"""Verification depth: the independent re-read, the linkage
checks, the stored report, and the two discrepancy exits.

The R1–R3 verifier diffed the planned fields against the raw
store and left its verdict in the audit trail only. This round:
verification re-reads through the console read surface, checks
that created and touched records link the way the console's rules
say, stores a VerificationReport per task, refuses to run twice —
and a DISCREPANCY gains exactly two recorded exits.
"""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.models import StepReportIn, TaskStatus
from page_agent_copilot.reads import ConsoleReads
from page_agent_copilot.records import apply_field_change, create_credit_memo
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore
from page_agent_copilot.verify import run_verification

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")


def plan_and_confirm(service, text=CREDIT, planner="j.okafor"):
    task = service.intake(text, planner)
    assert task.status == TaskStatus.PLANNED
    return service.confirm(task.task_id, "s.hale")


def discrepancy(service, text="mark order 1032 as shipped",
                tamper=None):
    task = plan_and_confirm(service, text)
    task = service.execute(task.task_id, "offline",
                           tamper=tamper or {1: "placed"})
    assert task.status == TaskStatus.DISCREPANCY
    return task


# ------------------------------------------------------- the report

def test_report_is_stored_with_every_check(service):
    task = plan_and_confirm(service)
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    report = service.get_verification(task.task_id)
    assert report is not None
    assert report.plan_version == 1
    assert report.driver == "offline"
    assert report.read_path == "console_read_surface"
    assert report.verified_at
    assert report.ok is True
    assert report.checked == 4  # the memo's four planned fields
    assert {c.field for c in report.field_checks} == {
        "invoice_id", "amount", "reason", "status"}
    assert all(c.verdict == "match" for c in report.field_checks)
    checks = {c.check: c for c in report.linkage_checks}
    assert set(checks) == {"created_for_task", "memo_customer",
                           "invoice_customer", "invoice_reflects_memo",
                           "execution_record"}
    assert all(c.verdict == "match" for c in checks.values())
    assert checks["memo_customer"].expected == "2041"
    assert checks["created_for_task"].expected == task.task_id
    assert report.diffs == []
    assert report.corrections == []


def test_report_for_a_set_step_carries_its_linkage(service):
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    service.execute(task.task_id, "offline")
    report = service.get_verification(task.task_id)
    assert report.checked == 1
    checks = {c.check: c for c in report.linkage_checks}
    assert checks["customer_link"].expected == "2041"
    assert checks["customer_link"].verdict == "match"
    # Who ran the step, and when, come from the execution record.
    assert "offline" in checks["execution_record"].detail


def test_no_report_before_execution(service):
    task = service.intake(CREDIT, "j.okafor")
    assert service.get_verification(task.task_id) is None


# ------------------------------------------- the independent re-read

def test_a_later_write_is_caught_by_the_reread(service):
    """The live path: the work is done through the console's
    mutation path, then the record is written again behind the
    shell's back. Verification re-reads the current state through
    the console read surface — what it diffs is what stands, not
    what was reported or first written."""
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    service.execute(task.task_id, "page-agent-live")
    apply_field_change(service.store, "orders", "1032", "status",
                       "shipped")
    record = service.store.get_record("orders", "1032")
    record["status"] = "delivered"
    service.store.upsert_record("orders", record)
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.DISCREPANCY
    report = service.get_verification(task.task_id)
    check = report.field_checks[0]
    assert check.expected == "shipped"
    assert check.actual == "delivered"
    assert check.verdict == "mismatch"
    assert report.diffs[0].field == "status"


def test_a_memo_on_the_wrong_invoice_is_named(service):
    """A faulty bridge issues the memo against the wrong invoice.
    The field diff names invoice_id; the linkage checks name the
    consequence: the memo names the wrong invoice's customer, and
    the planned invoice does not list it."""
    task = plan_and_confirm(service)
    service.execute(task.task_id, "page-agent-live")
    create_credit_memo(
        service.store,
        {"invoice_id": "4472", "amount": "120.00",
         "reason": "damaged goods", "status": "issued"},
        task_id=task.task_id)
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.DISCREPANCY
    report = service.get_verification(task.task_id)
    diff_fields = {d.field for d in report.diffs}
    assert "invoice_id" in diff_fields
    assert "customer_id" in diff_fields
    assert "credit_memos" in diff_fields
    checks = {c.check: c for c in report.linkage_checks}
    assert checks["memo_customer"].verdict == "mismatch"
    assert checks["memo_customer"].expected == "2041"
    assert checks["memo_customer"].actual == "2042"
    assert checks["invoice_reflects_memo"].verdict == "mismatch"
    # And what the memo got right is still recorded as right.
    assert checks["created_for_task"].verdict == "match"


def test_execution_record_must_agree_with_the_reread(service):
    """Unit-level: an execution record that claims a different
    write than the state the re-read finds is itself a named
    linkage failure — the audit trail is checked, not trusted."""
    task = service.intake("mark order 1032 as shipped", "j.okafor")
    plan = service.get_plan(task.task_id)
    reads = ConsoleReads(service.store)
    lying = [{"event": "step_executed", "actor": "offline",
              "ts": "2026-10-18T00:00:00+00:00",
              "detail": {"seq": 1, "after": {"status": "delivered"}}}]
    report = run_verification(
        reads, plan, {}, task_id=task.task_id, driver="offline",
        audit=lying, verified_at="2026-10-18T00:01:00+00:00")
    checks = {c.check: c for c in report.linkage_checks}
    assert checks["execution_record"].verdict == "mismatch"
    assert checks["execution_record"].expected == "delivered"
    assert checks["execution_record"].actual == "packed"
    missing = run_verification(
        reads, plan, {}, task_id=task.task_id, driver="offline",
        audit=[], verified_at="2026-10-18T00:01:00+00:00")
    checks = {c.check: c for c in missing.linkage_checks}
    assert checks["execution_record"].verdict == "mismatch"
    assert checks["execution_record"].actual is None


# ------------------------------------------- re-verification refused

def test_reverification_is_refused_and_the_record_stands(service):
    task = plan_and_confirm(service)
    task = service.execute(task.task_id, "offline")
    before = service.get_verification(task.task_id)
    with pytest.raises(GateError):
        service.verify_task(task.task_id, "j.okafor")
    after = service.get_verification(task.task_id)
    assert after.verified_at == before.verified_at
    assert after.ok is True
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "reverification_refused"]
    assert len(refused) == 1
    assert refused[0]["detail"]["code"] == "already_verified"


def test_verify_before_execution_is_refused(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.verify_task(task.task_id, "j.okafor")


# ------------------------------------------------- corrective tasks

def test_corrective_task_closes_the_diffs(service):
    task = discrepancy(service)
    child = service.spawn_corrective(task.task_id, "j.okafor")
    # Linked both ways, planned, and carrying only the diff step.
    assert child.status == TaskStatus.PLANNED
    assert child.parent_task_id == task.task_id
    parent = service.get_task(task.task_id)
    assert parent.corrective_task_ids == [child.task_id]
    child_plan = service.get_plan(child.task_id)
    assert child_plan.task_type == "corrective"
    assert len(child_plan.steps) == 1
    step = child_plan.steps[0]
    assert (step.record_kind, step.record_id, step.field, step.value) == \
        ("orders", "1032", "status", "shipped")
    # The full cycle, on its own: second-person confirmation,
    # execution, its own verification.
    service.confirm(child.task_id, "s.hale")
    child = service.execute(child.task_id, "offline")
    assert child.status == TaskStatus.VERIFIED
    assert service.store.get_record("orders", "1032")["status"] == "shipped"
    # The parent is not rewritten: still a discrepancy, with the
    # chain composed onto its report at read time.
    parent = service.get_task(task.task_id)
    assert parent.status == TaskStatus.DISCREPANCY
    report = service.get_verification(task.task_id)
    assert report.ok is False
    assert len(report.corrections) == 1
    entry = report.corrections[0]
    assert entry.task_id == child.task_id
    assert entry.status == "VERIFIED"
    assert entry.verification_ok is True
    spawned = [e for e in service.audit(task.task_id)
               if e["event"] == "corrective_spawned"]
    assert spawned[0]["detail"]["corrective_task_id"] == child.task_id


def test_corrective_carries_only_the_failed_steps(service):
    task = plan_and_confirm(
        service, CREDIT + " and mark invoice 4471 as paid")
    task = service.execute(task.task_id, "offline", tamper={2: "open"})
    assert task.status == TaskStatus.DISCREPANCY
    child = service.spawn_corrective(task.task_id, "j.okafor")
    steps = service.get_plan(child.task_id).steps
    assert len(steps) == 1
    assert (steps[0].record_kind, steps[0].field, steps[0].value) == \
        ("invoices", "status", "paid")
    # No second memo is issued by the correction.
    assert len(service.store.list_records("credit_memos")) == 1


def test_corrective_reissues_a_memo_that_never_landed(service):
    task = plan_and_confirm(service)
    service.execute(task.task_id, "page-agent-live")
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.DISCREPANCY  # nothing created
    child = service.spawn_corrective(task.task_id, "j.okafor")
    service.confirm(child.task_id, "s.hale")
    child = service.execute(child.task_id, "offline")
    assert child.status == TaskStatus.VERIFIED
    memos = service.store.list_records("credit_memos")
    assert len(memos) == 1
    assert memos[0]["invoice_id"] == "4471"
    assert memos[0]["created_for_task"] == child.task_id


def test_corrective_cannot_close_a_wrongly_created_memo(service):
    """A memo created with the wrong amount is a fact in the
    world: no plan edits a created memo, so the corrective spawn
    refuses, recorded — the deviation exit remains."""
    task = plan_and_confirm(service)
    service.execute(task.task_id, "page-agent-live")
    create_credit_memo(
        service.store,
        {"invoice_id": "4471", "amount": "999.00",
         "reason": "damaged goods", "status": "issued"},
        task_id=task.task_id)
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.DISCREPANCY
    with pytest.raises(GateError):
        service.spawn_corrective(task.task_id, "j.okafor")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "corrective_refused"]
    assert refused[-1]["detail"]["code"] == "corrective_cannot_close"
    assert service.get_task(task.task_id).corrective_task_ids == []


def test_spawn_corrective_needs_a_discrepancy(service):
    task = plan_and_confirm(service)
    with pytest.raises(GateError):
        service.spawn_corrective(task.task_id, "j.okafor")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    with pytest.raises(GateError):
        service.spawn_corrective(task.task_id, "j.okafor")


def test_second_corrective_after_a_verified_one_is_refused(service):
    task = discrepancy(service)
    child = service.spawn_corrective(task.task_id, "j.okafor")
    service.confirm(child.task_id, "s.hale")
    service.execute(child.task_id, "offline")
    with pytest.raises(GateError):
        service.spawn_corrective(task.task_id, "j.okafor")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "corrective_refused"]
    assert refused[-1]["detail"]["code"] == "already_corrected"


def test_corrective_spawn_follows_the_role_rules(service):
    task = discrepancy(service)
    with pytest.raises(GateError):
        service.spawn_corrective(task.task_id, "v.ward")  # viewer
    with pytest.raises(GateError):
        service.spawn_corrective(task.task_id, "nobody")  # no role


def test_corrective_for_a_supervisor_field_needs_a_supervisor(service):
    # Planned by the supervisor (credit_limit is held back from
    # operators), confirmed under dual control (round 18: the
    # field is high-stakes, so two distinct confirmers — a second
    # operator is enrolled for the second seat), tampered: the
    # diff is on a supervisor-only field, so the corrective plan
    # is a supervisor's to spawn — an operator's spawn is refused.
    service.enroll_operator("s.hale", "r.osei", "operator")
    task = service.intake("update customer 2041 credit limit to 30000",
                          "s.hale")
    service.confirm(task.task_id, "j.okafor")
    service.confirm(task.task_id, "r.osei")
    task = service.execute(task.task_id, "offline",
                           tamper={1: "25000.00"})
    assert task.status == TaskStatus.DISCREPANCY
    with pytest.raises(GateError):
        service.spawn_corrective(task.task_id, "j.okafor")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "corrective_refused"]
    assert refused[-1]["detail"]["code"] == "field_not_permitted"
    child = service.spawn_corrective(task.task_id, "s.hale")
    service.confirm(child.task_id, "j.okafor")
    service.confirm(child.task_id, "r.osei")
    child = service.execute(child.task_id, "offline")
    assert child.status == TaskStatus.VERIFIED
    customer = service.store.get_record("customers", "2041")
    assert customer["credit_limit"] == "30000"


# ------------------------------------------------- accept deviation

def test_deviation_acceptance_is_a_supervisor_act_with_a_reason(service):
    task = discrepancy(service)
    with pytest.raises(GateError):
        service.accept_deviation(task.task_id, "j.okafor",
                                 "the carrier collected it")
    with pytest.raises(GateError):
        service.accept_deviation(task.task_id, "s.hale", "   ")
    refused = [e["detail"]["code"] for e in service.audit(task.task_id)
               if e["event"] == "deviation_refused"]
    assert refused == ["not_a_supervisor", "reason_required"]
    task = service.accept_deviation(
        task.task_id, "s.hale",
        "the carrier's own scan shows collection; the record stands")
    assert task.status == TaskStatus.DISCREPANCY_ACCEPTED
    assert task.deviation_accepted_by == "s.hale"
    assert "carrier" in task.deviation_reason
    accepted = [e for e in service.audit(task.task_id)
                if e["event"] == "deviation_accepted"]
    assert len(accepted) == 1
    assert accepted[0]["detail"]["reason"] == task.deviation_reason
    assert accepted[0]["detail"]["diffs"][0]["field"] == "status"
    # Never rewritten: the report still names the difference, and
    # the task cannot be accepted (or verified) a second time.
    assert service.get_verification(task.task_id).ok is False
    with pytest.raises(GateError):
        service.accept_deviation(task.task_id, "s.hale", "again")


def test_deviation_needs_a_discrepancy(service):
    task = plan_and_confirm(service)
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    with pytest.raises(GateError):
        service.accept_deviation(task.task_id, "s.hale", "no diffs")


# ------------------------------------------------------------ the API

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def _http_discrepancy(client):
    created = client.post("/api/tasks", json={
        "text": "mark order 1032 as shipped", "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    client.post(f"/api/tasks/{tid}/confirm", json={"operator": "s.hale"})
    # Drive it over the live bridge with no work done: discrepancy.
    client.post(f"/api/tasks/{tid}/execute",
                json={"driver": "page-agent-live"})
    done = client.post(f"/api/tasks/{tid}/steps/1/report",
                       json={"ok": True})
    assert done.json()["status"] == "DISCREPANCY"
    return tid


def test_verification_over_http(client):
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    assert client.get(f"/api/tasks/{tid}/verification").status_code == 404
    client.post(f"/api/tasks/{tid}/confirm", json={"operator": "s.hale"})
    client.post(f"/api/tasks/{tid}/execute", json={"driver": "offline"})
    report = client.get(f"/api/tasks/{tid}/verification")
    assert report.status_code == 200
    body = report.json()
    assert body["ok"] is True
    assert body["read_path"] == "console_read_surface"
    assert body["corrections"] == []
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["verification"]["task_id"] == tid
    assert client.post(f"/api/tasks/{tid}/verify",
                       json={"operator": "j.okafor"}).status_code == 409


def test_discrepancy_exits_over_http(client):
    tid = _http_discrepancy(client)
    spawned = client.post(f"/api/tasks/{tid}/corrective",
                          json={"operator": "j.okafor"})
    assert spawned.status_code == 201
    child = spawned.json()["task"]
    assert child["parent_task_id"] == tid
    assert child["status"] == "PLANNED"
    report = client.get(f"/api/tasks/{tid}/verification").json()
    assert report["corrections"][0]["task_id"] == child["task_id"]
    # Accept-deviation: the operator is refused, the supervisor
    # without a reason is refused, with a reason it closes.
    assert client.post(f"/api/tasks/{tid}/accept-deviation", json={
        "operator": "j.okafor", "reason": "stands"}).status_code == 409
    assert client.post(f"/api/tasks/{tid}/accept-deviation", json={
        "operator": "s.hale", "reason": ""}).status_code == 409
    closed = client.post(f"/api/tasks/{tid}/accept-deviation", json={
        "operator": "s.hale",
        "reason": "warehouse confirms the order never left"})
    assert closed.status_code == 200
    assert closed.json()["status"] == "DISCREPANCY_ACCEPTED"


def test_verification_and_chain_survive_sqlite(sqlite_service):
    task = discrepancy(sqlite_service)
    report = sqlite_service.get_verification(task.task_id)
    assert report is not None and report.ok is False
    child = sqlite_service.spawn_corrective(task.task_id, "j.okafor")
    sqlite_service.confirm(child.task_id, "s.hale")
    child = sqlite_service.execute(child.task_id, "offline")
    assert child.status == TaskStatus.VERIFIED
    report = sqlite_service.get_verification(task.task_id)
    assert report.corrections[0].verification_ok is True
