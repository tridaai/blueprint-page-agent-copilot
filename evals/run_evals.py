"""Eval gate: scripted tasks through intake -> plan -> confirm ->
execute -> verify, graded on the exact record diff and the gate
behaviour. Gold outcomes hold by construction: each case names the
record state it must produce, and the grader diffs the store, never
the agent's word.

    uv run python evals/run_evals.py

Exit 0 = PASS. Every case runs on a fresh in-memory store.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from page_agent_copilot.config import Settings  # noqa: E402
from page_agent_copilot.models import (  # noqa: E402
    AmendChangeIn,
    StepReportIn,
    TaskStatus,
)
from page_agent_copilot.policy import PlanRefusal  # noqa: E402
from page_agent_copilot.records import (  # noqa: E402
    apply_field_change,
    create_credit_memo,
)
from page_agent_copilot.service import CopilotService, GateError  # noqa: E402
from page_agent_copilot.store import InMemoryStore  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def case(name):
    def wrap(fn):
        RESULTS.append((name, *run(fn)))
        return fn
    return wrap


def run(fn):
    try:
        fn()
        return True, ""
    except AssertionError as exc:
        return False, str(exc)
    except Exception as exc:  # a case that errors is a failed case
        return False, f"{type(exc).__name__}: {exc}"


def fresh() -> CopilotService:
    svc = CopilotService(Settings.from_env(), InMemoryStore())
    svc.seed()
    return svc


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")


@case("credit memo: happy path verifies, memo fields exact")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    expect(task.status == TaskStatus.PLANNED, f"status {task.status}")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    memos = svc.store.list_records("credit_memos")
    expect(len(memos) == 1, f"{len(memos)} memos")
    memo = memos[0]
    expect(memo["invoice_id"] == "4471" and memo["amount"] == "120.00"
           and memo["reason"] == "damaged goods"
           and memo["status"] == "issued", f"memo {memo}")


@case("order status: exact diff on one field only")
def _():
    svc = fresh()
    before = svc.store.get_record("orders", "1032")
    task = svc.intake("mark order 1032 as shipped", "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    after = svc.store.get_record("orders", "1032")
    changed = {k for k in after if after[k] != before[k]}
    expect(changed == {"status"}, f"changed fields {changed}")
    expect(after["status"] == "shipped", "status not shipped")


@case("customer update: phone changes, nothing else does")
def _():
    svc = fresh()
    task = svc.intake("update customer 2041 phone to 555-0134", "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    cust = svc.store.get_record("customers", "2041")
    expect(cust["phone"] == "555-0134", f"phone {cust['phone']}")
    expect(cust["bank_account"].startswith("GB29"),
           "payment field moved — must never happen")


@case("invoice status: overdue invoice marked paid")
def _():
    svc = fresh()
    task = svc.intake("mark invoice 4474 as paid", "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    expect(svc.store.get_record("invoices", "4474")["status"] == "paid",
           "invoice not paid")


@case("gate: execution before confirmation is refused")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    try:
        svc.execute(task.task_id, "offline")
    except GateError:
        pass
    else:
        raise AssertionError("unconfirmed execution was allowed")
    expect(svc.store.list_records("credit_memos") == [],
           "a memo was created without confirmation")


@case("gate: over-cap credit refused at plan time")
def _():
    svc = fresh()
    task = svc.intake("raise a credit memo for invoice 4471, reason "
                      "goodwill, $900", "op")
    expect(task.status == TaskStatus.REFUSED, f"status {task.status}")
    expect("credit_above_cap" in (task.refusal_reason or ""),
           f"reason {task.refusal_reason}")


@case("gate: payment-field plan refused at plan time")
def _():
    svc = fresh()
    task = svc.intake("update customer 2041 bank account to GB00 XXXX",
                      "op")
    expect(task.status == TaskStatus.REFUSED, f"status {task.status}")
    expect("payment_field" in (task.refusal_reason or ""),
           f"reason {task.refusal_reason}")


@case("gate: delete refused at plan time")
def _():
    svc = fresh()
    task = svc.intake("delete order 1036", "op")
    expect(task.status == TaskStatus.REFUSED, f"status {task.status}")
    expect("delete_refused" in (task.refusal_reason or ""),
           f"reason {task.refusal_reason}")
    expect(svc.store.get_record("orders", "1036") is not None,
           "the record was deleted")


@case("verify: a wrong write is a DISCREPANCY, never a success")
def _():
    svc = fresh()
    task = svc.intake("mark order 1032 as shipped", "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline", tamper={1: "placed"})
    expect(task.status == TaskStatus.DISCREPANCY, f"status {task.status}")
    failed = [e for e in svc.audit(task.task_id)
              if e["event"] == "verification_failed"]
    expect(len(failed) == 1, "no verification_failed audit event")
    diff = failed[0]["detail"]["diffs"][0]
    expect(diff["field"] == "status" and diff["planned"] == "shipped"
           and diff["actual"] == "placed", f"diff {diff}")


@case("live driver: bridge reports, shell verifies from the store")
def _():
    svc = fresh()
    task = svc.intake("mark order 1032 as shipped", "op")
    svc.confirm(task.task_id, "s.hale")
    svc.execute(task.task_id, "page-agent-live")
    apply_field_change(svc.store, "orders", "1032", "status", "shipped")
    task = svc.report_step(task.task_id, 1, StepReportIn(ok=True))
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")


@case("live driver: a success report with no work done still fails")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    svc.confirm(task.task_id, "s.hale")
    svc.execute(task.task_id, "page-agent-live")
    task = svc.report_step(task.task_id, 1, StepReportIn(ok=True))
    expect(task.status == TaskStatus.DISCREPANCY, f"status {task.status}")
    expect(svc.store.list_records("credit_memos") == [],
           "a memo exists that nobody created")


@case("page text is data: the note's '$5,000 refund' is never planned")
def _():
    svc = fresh()
    task = svc.intake("update customer 2044 phone to 555-0199", "op")
    plan = svc.get_plan(task.task_id)
    expect(task.status == TaskStatus.PLANNED, f"status {task.status}")
    expect(len(plan.steps) == 1 and plan.steps[0].field == "phone",
           f"plan steps {plan.steps}")
    expect(svc.store.list_records("credit_memos") == [],
           "the note produced a credit memo")


@case("multi-record: credit memo + invoice settled, both diffed exactly")
def _():
    svc = fresh()
    task = svc.intake(CREDIT + " and mark invoice 4471 as paid", "op")
    plan = svc.get_plan(task.task_id)
    expect(len(plan.steps) == 2, f"steps {len(plan.steps)}")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    memo = svc.store.list_records("credit_memos")[0]
    expect(memo["amount"] == "120.00" and memo["invoice_id"] == "4471",
           f"memo {memo}")
    invoice = svc.store.get_record("invoices", "4471")
    expect(invoice["status"] == "paid", f"invoice {invoice['status']}")
    expect(invoice["amount"] == "1840.00",
           "an unplanned invoice field changed")


@case("multi-record: shipped with date and carrier, three steps exact")
def _():
    svc = fresh()
    before = svc.store.get_record("orders", "1032")
    task = svc.intake("mark order 1032 as shipped, ship date "
                      "2026-10-20, carrier DHL", "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    after = svc.store.get_record("orders", "1032")
    changed = {k for k in after if after[k] != before[k]}
    expect(changed == {"status", "ship_date", "carrier"},
           f"changed fields {changed}")


@case("multi-record: a failed step is named, the plan is no success")
def _():
    svc = fresh()
    task = svc.intake(CREDIT + " and mark invoice 4471 as paid", "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline", tamper={2: "open"})
    expect(task.status == TaskStatus.DISCREPANCY, f"status {task.status}")
    failed = [e for e in svc.audit(task.task_id)
              if e["event"] == "verification_failed"]
    diffs = failed[0]["detail"]["diffs"]
    expect(len(diffs) == 1 and diffs[0]["seq"] == 2,
           f"diffs {diffs}")


@case("amendment: v2 confirmed binds v2, executed values are v2's")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    svc.amend(task.task_id, "op",
              [AmendChangeIn(seq=1, fields={"amount": "150.00"})])
    task = svc.confirm(task.task_id, "s.hale")
    expect(task.confirmed_version == 2,
           f"confirmed_version {task.confirmed_version}")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    memo = svc.store.list_records("credit_memos")[0]
    expect(memo["amount"] == "150.00", f"memo amount {memo['amount']}")
    diff = svc.plan_diff(task.task_id, 1, 2)
    expect(diff.step_diffs[0].changes[0].field == "amount",
           f"diff {diff}")


@case("amendment: a confirmed v1 can never be edited into a v2")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    svc.confirm(task.task_id, "s.hale")
    try:
        svc.amend(task.task_id, "op",
                  [AmendChangeIn(seq=1, fields={"amount": "150.00"})])
    except GateError:
        pass
    else:
        raise AssertionError("a confirmed plan was amended")
    task = svc.execute(task.task_id, "offline")
    memo = svc.store.list_records("credit_memos")[0]
    expect(memo["amount"] == "120.00",
           f"executed amount {memo['amount']} — not the confirmed v1")


@case("amendment: a policy-breaking value is refused, plan untouched")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    try:
        svc.amend(task.task_id, "op",
                  [AmendChangeIn(seq=1, fields={"amount": "900.00"})])
    except PlanRefusal as refusal:
        expect(refusal.code == "credit_above_cap",
               f"code {refusal.code}")
    else:
        raise AssertionError("an over-cap amendment was accepted")
    task = svc.get_task(task.task_id)
    expect(task.status == TaskStatus.PLANNED and task.plan_version == 1,
           f"task {task.status} v{task.plan_version}")


@case("staleness: a record changed under the plan refuses execution")
def _():
    svc = fresh()
    task = svc.intake("mark order 1032 as shipped", "op")
    svc.confirm(task.task_id, "s.hale")
    apply_field_change(svc.store, "orders", "1032", "carrier", "FedEx")
    try:
        svc.execute(task.task_id, "offline")
    except GateError:
        pass
    else:
        raise AssertionError("a stale plan executed")
    task = svc.get_task(task.task_id)
    expect(task.status == TaskStatus.STALE, f"status {task.status}")
    expect(task.stale_records == ["orders 1032"],
           f"stale {task.stale_records}")
    order = svc.store.get_record("orders", "1032")
    expect(order["status"] == "packed" and order["carrier"] == "FedEx",
           "the stale execution wrote something")
    # The way back: re-plan (v1 kept, superseded), confirm, execute.
    task = svc.replan(task.task_id, "op")
    expect(task.plan_version == 2, f"version {task.plan_version}")
    expect(svc.get_plan(task.task_id, 1).superseded is True,
           "the stale version was not marked superseded")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    expect(svc.store.get_record("orders", "1032")["status"] == "shipped",
           "the re-planned task did not land")


@case("permissions: a viewer cannot plan")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "v.ward")
    expect(task.status == TaskStatus.REFUSED, f"status {task.status}")
    expect("role_cannot_plan" in (task.refusal_reason or ""),
           f"reason {task.refusal_reason}")
    expect("viewer" in (task.refusal_reason or ""),
           "the refusal does not name the role")
    expect(svc.store.list_records("credit_memos") == [],
           "a viewer produced a credit memo")


@case("permissions: a supervisor-only field is refused for an operator")
def _():
    svc = fresh()
    task = svc.intake("update customer 2041 credit limit to 30000",
                      "op")
    expect(task.status == TaskStatus.REFUSED, f"status {task.status}")
    expect("field_not_permitted" in (task.refusal_reason or ""),
           f"reason {task.refusal_reason}")
    expect("credit_limit" in (task.refusal_reason or "")
           and "operator" in (task.refusal_reason or ""),
           "the refusal does not name the field and the role")
    # The same task is a supervisor's to plan, and it verifies.
    task = svc.intake("update customer 2041 credit limit to 30000",
                      "s.hale")
    expect(task.status == TaskStatus.PLANNED, f"status {task.status}")
    svc.confirm(task.task_id, "op")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    cust = svc.store.get_record("customers", "2041")
    expect(cust["credit_limit"] == "30000",
           f"credit_limit {cust['credit_limit']}")


@case("permissions: self-confirmation is refused, and recorded")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    try:
        svc.confirm(task.task_id, "op")
    except GateError:
        pass
    else:
        raise AssertionError("the planner confirmed their own plan")
    task = svc.get_task(task.task_id)
    expect(task.status == TaskStatus.PLANNED, f"status {task.status}")
    refused = [e for e in svc.audit(task.task_id)
               if e["event"] == "confirmation_refused"]
    expect(len(refused) == 1
           and refused[0]["detail"]["code"] == "self_confirmation",
           f"audit {refused}")


@case("permissions: supervisor confirmation succeeds end to end")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    task = svc.confirm(task.task_id, "s.hale")
    expect(task.status == TaskStatus.CONFIRMED, f"status {task.status}")
    expect(task.confirmed_by == "s.hale",
           f"confirmed_by {task.confirmed_by}")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")


@case("permissions: a role narrowed after confirmation blocks execution")
def _():
    svc = fresh()
    task = svc.intake("mark order 1032 as shipped", "op")
    svc.confirm(task.task_id, "s.hale")
    raw = svc.get_role("operator").model_dump(mode="json")
    raw["fields"]["orders"] = ["carrier", "ship_date"]
    svc.update_role("operator", raw, "s.hale")
    try:
        svc.execute(task.task_id, "offline")
    except GateError as exc:
        expect("status" in str(exc) and "operator" in str(exc),
               f"refusal does not name field and role: {exc}")
    else:
        raise AssertionError("a narrowed role still executed")
    task = svc.get_task(task.task_id)
    expect(task.status == TaskStatus.CONFIRMED, f"status {task.status}")
    expect(task.driver is None, "execution started under a refusal")
    order = svc.store.get_record("orders", "1032")
    expect(order["status"] == "packed", "the blocked run wrote")
    refused = [e for e in svc.audit(task.task_id)
               if e["event"] == "execution_refused"]
    expect(len(refused) == 1
           and refused[0]["detail"]["field"] == "status",
           f"audit {refused}")
    # Restoring the field lets the same confirmed plan execute.
    raw["fields"]["orders"] = ["carrier", "ship_date", "status"]
    svc.update_role("operator", raw, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")


@case("permissions: role edits are recorded acts, with their diff")
def _():
    svc = fresh()
    raw = svc.get_role("operator").model_dump(mode="json")
    raw["fields"]["orders"] = ["carrier", "ship_date"]
    svc.update_role("operator", raw, "s.hale")
    audit = svc.role_audit()
    expect(len(audit) == 1, f"role audit {audit}")
    entry = audit[0]
    expect(entry["event"] == "role_updated"
           and entry["actor"] == "s.hale" and entry["ts"],
           f"entry {entry}")
    changes = {c["field"]: c for c in entry["detail"]["changes"]}
    expect(changes["fields.orders"]["new"] == ["carrier", "ship_date"],
           f"changes {changes}")
    # An operator may not edit roles at all.
    try:
        svc.update_role("operator", raw, "op")
    except GateError:
        pass
    else:
        raise AssertionError("an operator edited a role definition")


@case("verify depth: a write after the work is caught by the re-read")
def _():
    svc = fresh()
    task = svc.intake("mark order 1032 as shipped", "op")
    svc.confirm(task.task_id, "s.hale")
    svc.execute(task.task_id, "page-agent-live")
    apply_field_change(svc.store, "orders", "1032", "status", "shipped")
    # The record is written again afterwards — another hand, a
    # buggy form handler. Verification re-reads through the console
    # read surface; what it diffs is the state that stands, not the
    # work it saw reported.
    record = svc.store.get_record("orders", "1032")
    record["status"] = "delivered"
    svc.store.upsert_record("orders", record)
    task = svc.report_step(task.task_id, 1, StepReportIn(ok=True))
    expect(task.status == TaskStatus.DISCREPANCY, f"status {task.status}")
    report = svc.get_verification(task.task_id)
    expect(report is not None
           and report.read_path == "console_read_surface",
           f"report {report}")
    check = report.field_checks[0]
    expect(check.expected == "shipped" and check.actual == "delivered"
           and check.verdict == "mismatch", f"check {check}")


@case("verify depth: a memo against the wrong invoice is caught and named")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    svc.confirm(task.task_id, "s.hale")
    svc.execute(task.task_id, "page-agent-live")
    # A faulty bridge issues the memo against the wrong invoice.
    create_credit_memo(svc.store, {
        "invoice_id": "4472", "amount": "120.00",
        "reason": "damaged goods", "status": "issued"},
        task_id=task.task_id)
    task = svc.report_step(task.task_id, 1, StepReportIn(ok=True))
    expect(task.status == TaskStatus.DISCREPANCY, f"status {task.status}")
    report = svc.get_verification(task.task_id)
    fields = {d.field for d in report.diffs}
    expect("invoice_id" in fields and "customer_id" in fields,
           f"diff fields {fields}")
    link = [c for c in report.linkage_checks
            if c.check == "memo_customer"][0]
    expect(link.verdict == "mismatch" and link.expected == "2041"
           and link.actual == "2042", f"linkage {link}")


@case("discrepancy: a corrective task closes the diffs, chain on the report")
def _():
    svc = fresh()
    task = svc.intake("mark order 1032 as shipped", "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline", tamper={1: "placed"})
    expect(task.status == TaskStatus.DISCREPANCY, f"status {task.status}")
    child = svc.spawn_corrective(task.task_id, "op")
    expect(child.parent_task_id == task.task_id
           and child.status == TaskStatus.PLANNED,
           f"child {child.status}")
    parent = svc.get_task(task.task_id)
    expect(parent.corrective_task_ids == [child.task_id],
           f"parent links {parent.corrective_task_ids}")
    svc.confirm(child.task_id, "s.hale")
    child = svc.execute(child.task_id, "offline")
    expect(child.status == TaskStatus.VERIFIED, f"child {child.status}")
    expect(svc.store.get_record("orders", "1032")["status"] == "shipped",
           "the corrective did not land")
    parent = svc.get_task(task.task_id)
    expect(parent.status == TaskStatus.DISCREPANCY,
           "the parent's verdict was rewritten")
    report = svc.get_verification(task.task_id)
    expect(len(report.corrections) == 1
           and report.corrections[0].task_id == child.task_id
           and report.corrections[0].verification_ok is True,
           f"corrections {report.corrections}")


@case("discrepancy: deviation accepted — supervisor, reason, never VERIFIED")
def _():
    svc = fresh()
    task = svc.intake("mark order 1032 as shipped", "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline", tamper={1: "placed"})
    expect(task.status == TaskStatus.DISCREPANCY, f"status {task.status}")
    for operator, reason in (("op", "the carrier collected it"),
                             ("s.hale", "")):
        try:
            svc.accept_deviation(task.task_id, operator, reason)
        except GateError:
            pass
        else:
            raise AssertionError(
                f"deviation accepted by {operator!r} reason {reason!r}")
    task = svc.accept_deviation(
        task.task_id, "s.hale",
        "the carrier's own scan shows collection; the record stands")
    expect(task.status == TaskStatus.DISCREPANCY_ACCEPTED,
           f"status {task.status}")
    expect(task.deviation_accepted_by == "s.hale"
           and task.deviation_reason, "the acceptance is not recorded")
    report = svc.get_verification(task.task_id)
    expect(report.ok is False, "the report was rewritten to a pass")


@case("verify: re-verification is refused and the record stands")
def _():
    svc = fresh()
    task = svc.intake(CREDIT, "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    expect(task.status == TaskStatus.VERIFIED, f"status {task.status}")
    before = svc.get_verification(task.task_id)
    expect(before is not None and before.ok, "no report recorded")
    try:
        svc.verify_task(task.task_id, "op")
    except GateError:
        pass
    else:
        raise AssertionError("a second verification ran")
    after = svc.get_verification(task.task_id)
    expect(after.verified_at == before.verified_at and after.ok,
           "the recorded report changed")
    refused = [e for e in svc.audit(task.task_id)
               if e["event"] == "reverification_refused"]
    expect(len(refused) == 1, f"audit {refused}")


def main() -> int:
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}"
              + (f"  -- {detail}" if detail and not ok else ""))
    print(f"\n{passed}/{len(RESULTS)} cases passed")
    print("EVALS: PASS" if passed == len(RESULTS) else "EVALS: FAIL")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
