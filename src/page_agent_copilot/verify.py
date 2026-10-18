"""Verification in code: the plan is the contract, the re-read is
the evidence.

After execution — by either driver — the shell verifies. It does
not ask the executor what happened, and it does not re-read through
the path the work went in by: every record is re-read through the
console read surface (`reads.py`) — the same read functions the
API serves to a human's browser — and diffed against the approved
plan. The store underneath is one single source of truth; the
independence is in the path, and it is the honest boundary: a bug
in the mutation path, or a bridge report that claims success for
work never done, cannot certify itself, because neither is
consulted. The agent's own report is never evidence.

Two kinds of checks, one verdict:

- **Field checks** — every planned field, per step in plan order:
  the value the plan named vs the value the re-read found.
- **Linkage checks** — whether the records hang together the way
  the console's own rules say they should: a created credit memo
  names this task as its creator, names the customer of the
  invoice it was planned against, and is listed back on that
  invoice's memo list; the invoice's customer exists; a touched
  order or invoice still names a customer that exists; a customer
  a cross-page plan updates is the customer of the invoice its
  credit memo was planned against; and the execution record in
  the audit trail — who ran each step, when — agrees with the
  state the re-read found.

An exact match across every check is the only path to VERIFIED.
Every failure is a named FieldDiff — field checks first, in plan
order, then linkage failures under their field names — and the
task lands in DISCREPANCY: a plan whose second step failed is a
discrepancy, never a partial success.

The result is a `VerificationReport`, stored by the service as the
record of the act. Verification happens once, at the end of
execution; the service refuses to re-run it (see
`service.verify_task`).
"""

from __future__ import annotations

from typing import Optional

from .models import (
    FieldCheck,
    FieldDiff,
    LinkageCheck,
    Plan,
    PlanStep,
    StepAction,
    VerificationReport,
)
from .reads import ConsoleReads

# The read path every verdict in this module comes from, named on
# the report so a reader can see the evidence's provenance.
READ_PATH = "console_read_surface"


def _execution_entry(audit: list[dict], seq: int) -> Optional[dict]:
    for entry in audit:
        if (entry["event"] in ("step_executed", "step_reported")
                and entry["detail"].get("seq") == seq):
            return entry
    return None


def _execution_check(step: PlanStep, audit: list[dict], *,
                     record_kind: str, record_id: str, field: str,
                     compare_key: str,
                     current: Optional[str]) -> LinkageCheck:
    """The execution-record check for one step: the audit trail's
    record of who ran the step, and when, must cohere with what
    the re-read found. For the offline driver the recorded outcome
    is compared value for value; a live bridge's report is advisory
    — its presence is recorded, and the field checks carry the
    verdict, never the report."""
    base = {"seq": step.seq, "record_kind": record_kind,
            "record_id": record_id, "check": "execution_record",
            "field": field}
    entry = _execution_entry(audit, step.seq)
    if entry is None:
        return LinkageCheck(
            **base, expected="an execution record for this step",
            actual=None, verdict="mismatch",
            detail="the audit trail holds no execution record "
                   "for this step")
    who_when = (f"{entry['event']} by {entry['actor']} "
                f"at {entry['ts']}")
    if entry["event"] == "step_reported":
        return LinkageCheck(
            **base, expected="reported", actual="reported",
            verdict="match",
            detail=f"{who_when}; a bridge report is recorded, "
                   f"never consulted as evidence")
    recorded = (entry["detail"].get("after") or {}).get(compare_key)
    recorded = str(recorded) if recorded is not None else None
    ok = recorded == current
    return LinkageCheck(
        **base, expected=recorded, actual=current,
        verdict="match" if ok else "mismatch",
        detail=f"{who_when}; the recorded outcome "
               f"{'agrees with' if ok else 'disagrees with'} "
               f"the re-read state")


def run_verification(reads: ConsoleReads, plan: Plan,
                     created_ids: dict[int, str], *,
                     task_id: str, driver: str, audit: list[dict],
                     verified_at: str) -> VerificationReport:
    field_checks: list[FieldCheck] = []
    linkage_checks: list[LinkageCheck] = []

    for step in plan.steps:
        if step.action == StepAction.SET:
            record = reads.get(step.record_kind, step.record_id or "")
            actual: Optional[str] = None
            if record is not None and step.field is not None:
                actual = str(record.get(step.field, ""))
            field_checks.append(FieldCheck(
                seq=step.seq, record_kind=step.record_kind,
                record_id=step.record_id or "",
                field=step.field or "", expected=step.value,
                actual=actual,
                verdict="match" if actual == step.value
                else "mismatch"))
            if record is not None and record.get("customer_id"):
                cid = str(record["customer_id"])
                customer = reads.get("customers", cid)
                linkage_checks.append(LinkageCheck(
                    seq=step.seq, record_kind=step.record_kind,
                    record_id=step.record_id or "",
                    check="customer_link", field="customer_id",
                    expected=cid,
                    actual=cid if customer is not None else None,
                    verdict="match" if customer is not None
                    else "mismatch",
                    detail=f"{step.record_kind[:-1]} "
                           f"{step.record_id} names customer {cid}"
                           + ("" if customer is not None
                              else ", who does not exist")))
            if step.record_kind == "customers":
                # The cross-page tie (round 12): when this plan also
                # creates a credit memo, the customer it updates
                # must be the customer of the invoice the memo was
                # planned against — the two halves of the flow hang
                # on the same customer, or the plan did not do what
                # it said across the page boundary.
                for other in plan.steps:
                    if other.action != StepAction.CREATE:
                        continue
                    inv_id = other.fields.get("invoice_id", "")
                    inv = reads.get("invoices", inv_id)
                    expected = (str(inv["customer_id"])
                                if inv is not None else None)
                    hit = (expected is not None
                           and step.record_id == expected)
                    linkage_checks.append(LinkageCheck(
                        seq=step.seq, record_kind="customers",
                        record_id=step.record_id or "",
                        check="invoice_customer_link",
                        field="customer_id", expected=expected,
                        actual=step.record_id,
                        verdict="match" if hit else "mismatch",
                        detail=f"this plan's credit memo is against "
                               f"invoice {inv_id}, whose customer is "
                               f"{expected}; the step updates "
                               f"customer {step.record_id}"))
            linkage_checks.append(_execution_check(
                step, audit, record_kind=step.record_kind,
                record_id=step.record_id or "",
                field=step.field or "", compare_key=step.field or "",
                current=actual))
            continue

        # CREATE — a credit memo, issued whole.
        created_id = created_ids.get(step.seq)
        memo = (reads.get(step.record_kind, created_id)
                if created_id else None)
        shown_id = created_id or "(not created)"
        for field, planned in sorted(step.fields.items()):
            actual = str(memo.get(field, "")) if memo else None
            field_checks.append(FieldCheck(
                seq=step.seq, record_kind=step.record_kind,
                record_id=shown_id, field=field, expected=planned,
                actual=actual,
                verdict="match" if actual == planned else "mismatch"))
        invoice_id = step.fields.get("invoice_id", "")
        invoice = reads.get("invoices", invoice_id)

        stamped = (str(memo.get("created_for_task", ""))
                   if memo else None)
        linkage_checks.append(LinkageCheck(
            seq=step.seq, record_kind=step.record_kind,
            record_id=shown_id, check="created_for_task",
            field="created_for_task", expected=task_id,
            actual=stamped,
            verdict="match" if stamped == task_id else "mismatch",
            detail="the memo names the task that created it"
                   if stamped == task_id else
                   "the memo does not name this task as its creator"))

        expected_customer = (str(invoice["customer_id"])
                             if invoice else None)
        actual_customer = (str(memo.get("customer_id", ""))
                           if memo else None)
        customer_ok = (expected_customer is not None
                       and actual_customer == expected_customer)
        linkage_checks.append(LinkageCheck(
            seq=step.seq, record_kind=step.record_kind,
            record_id=shown_id, check="memo_customer",
            field="customer_id", expected=expected_customer,
            actual=actual_customer,
            verdict="match" if customer_ok else "mismatch",
            detail=f"invoice {invoice_id} belongs to customer "
                   f"{expected_customer}; the memo names "
                   f"{actual_customer}"))

        customer = (reads.get("customers", expected_customer)
                    if expected_customer else None)
        linkage_checks.append(LinkageCheck(
            seq=step.seq, record_kind="invoices",
            record_id=invoice_id, check="invoice_customer",
            field="customer_id", expected=expected_customer,
            actual=(expected_customer if customer is not None
                    else None),
            verdict="match" if customer is not None else "mismatch",
            detail=f"invoice {invoice_id} names customer "
                   f"{expected_customer}"
                   + ("" if customer is not None
                      else ", who does not exist")))

        listed = {m["id"] for m in reads.memos_for_invoice(invoice_id)}
        reflected = (created_id if created_id and created_id in listed
                     else None)
        linkage_checks.append(LinkageCheck(
            seq=step.seq, record_kind="invoices",
            record_id=invoice_id, check="invoice_reflects_memo",
            field="credit_memos", expected=created_id,
            actual=reflected,
            verdict="match" if reflected is not None else "mismatch",
            detail=f"invoice {invoice_id}'s memo list "
                   f"{'shows' if reflected else 'does not show'} "
                   f"the created memo"))

        linkage_checks.append(_execution_check(
            step, audit, record_kind=step.record_kind,
            record_id=shown_id, field="id", compare_key="id",
            current=created_id))

    diffs = [
        FieldDiff(seq=c.seq, record_kind=c.record_kind,
                  record_id=c.record_id, field=c.field,
                  planned=c.expected, actual=c.actual)
        for c in field_checks if c.verdict != "match"
    ] + [
        FieldDiff(seq=c.seq, record_kind=c.record_kind,
                  record_id=c.record_id, field=c.field,
                  planned=c.expected, actual=c.actual)
        for c in linkage_checks if c.verdict != "match"
    ]
    return VerificationReport(
        task_id=task_id, plan_version=plan.version, driver=driver,
        read_path=READ_PATH, verified_at=verified_at,
        ok=not diffs, checked=len(field_checks),
        field_checks=field_checks, linkage_checks=linkage_checks,
        diffs=diffs)
