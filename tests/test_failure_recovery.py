"""Failure recovery: a step that fails mid-plan stops the
execution, the failure is recorded as an attempt, and the way
forward — resume, restart, abandon — is decided in code."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot import drivers as drivers_module
from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.models import StepReportIn, TaskStatus
from page_agent_copilot.records import apply_field_change, create_credit_memo
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
CREDIT_AND_SETTLE = CREDIT + " and mark invoice 4471 as paid"
SHIP = ("mark order 1032 as shipped, ship date 2026-10-20, "
        "carrier DHL")
NOT_FOUND = ("element not found: no field labelled 'Ship date' "
             "on the Orders form")
TIMEOUT = "bridge timeout: no step result after 30s"


def plan_and_confirm(service, text=SHIP):
    task = service.intake(text, "j.okafor")
    assert task.status == TaskStatus.PLANNED
    return service.confirm(task.task_id, "s.hale")


def failed_ship(service, fail_at=2, error=NOT_FOUND):
    """A confirmed ship plan whose execution fails at `fail_at`."""
    task = plan_and_confirm(service)
    return service.execute(task.task_id, "offline",
                           fail={fail_at: error})


# ------------------------------------------------------- the failure

def test_mid_plan_failure_stops_and_is_accounted_exactly(service):
    task = failed_ship(service)
    assert task.status == TaskStatus.EXECUTION_FAILED
    assert task.failed_step == 2
    assert task.failure_error == NOT_FOUND  # the driver's error, verbatim
    # Step 1 landed and stands; steps 2 and 3 never ran.
    order = service.store.get_record("orders", "1032")
    assert order["status"] == "shipped"
    assert order["ship_date"] == "" and order["carrier"] == ""
    # The attempt record: one completed step with before/after,
    # the failed step last, the partial state named.
    attempts = service.get_attempts(task.task_id)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.number == 1 and attempt.mode == "execute"
    assert attempt.outcome == "failed"
    assert attempt.failed_seq == 2 and attempt.error == NOT_FOUND
    assert [o.seq for o in attempt.steps] == [1, 2]
    assert attempt.steps[0].ok and attempt.steps[0].after == {
        "status": "shipped"}
    assert not attempt.steps[1].ok
    assert attempt.state_lines == [
        "step 1: orders 1032.status is now 'shipped'"]
    # The audit names the failure and the completed steps.
    failed = [e for e in service.audit(task.task_id)
              if e["event"] == "execution_failed"]
    assert len(failed) == 1
    assert failed[0]["detail"]["failed_seq"] == 2
    assert failed[0]["detail"]["completed_steps"] == [1]
    assert failed[0]["detail"]["partial_state"] == attempt.state_lines
    # No verification ran: a failure is not a verdict.
    assert service.get_verification(task.task_id) is None


def test_failure_at_the_first_step_completes_nothing(service):
    task = failed_ship(service, fail_at=1)
    assert task.status == TaskStatus.EXECUTION_FAILED
    attempt = service.get_attempts(task.task_id)[0]
    assert [o.seq for o in attempt.steps if o.ok] == []
    assert attempt.state_lines == []
    order = service.store.get_record("orders", "1032")
    assert order["status"] == "packed"


def test_a_driver_error_that_raises_becomes_the_recorded_failure(
        service, monkeypatch):
    def raiser(store, kind, record_id, field, value):
        raise RuntimeError(
            "form rejected: order 1032 is locked by another session")

    monkeypatch.setattr(drivers_module, "apply_field_change", raiser)
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    task = service.execute(task.task_id, "offline")
    # The error did not escape as an exception stranding the task:
    # it is the failure, recorded, with the task in a state that
    # has exits.
    assert task.status == TaskStatus.EXECUTION_FAILED
    assert task.failure_error == (
        "form rejected: order 1032 is locked by another session")
    assert service.get_task(task.task_id).driver == "offline"


# ------------------------------------------------------------ resume

def test_resume_runs_only_the_remaining_steps_and_verifies(service):
    task = failed_ship(service)
    task = service.resume_execution(task.task_id)
    assert task.status == TaskStatus.VERIFIED
    order = service.store.get_record("orders", "1032")
    assert order["status"] == "shipped"
    assert order["ship_date"] == "2026-10-20"
    assert order["carrier"] == "DHL"
    attempts = service.get_attempts(task.task_id)
    assert len(attempts) == 2
    assert attempts[1].mode == "resume"
    assert attempts[1].outcome == "completed"
    assert [o.seq for o in attempts[1].steps] == [2, 3]
    # Step 1 was executed exactly once, in the first attempt.
    executed = [e["detail"]["seq"] for e in service.audit(task.task_id)
                if e["event"] == "step_executed"]
    assert executed == [1, 2, 3]


def test_resume_after_a_create_does_not_duplicate_it(service):
    task = plan_and_confirm(service, CREDIT_AND_SETTLE)
    task = service.execute(task.task_id, "offline", fail={2: TIMEOUT})
    assert task.status == TaskStatus.EXECUTION_FAILED
    assert len(service.store.list_records("credit_memos")) == 1
    task = service.resume_execution(task.task_id)
    assert task.status == TaskStatus.VERIFIED
    assert len(service.store.list_records("credit_memos")) == 1
    assert service.store.get_record("invoices", "4471")["status"] == "paid"
    report = service.get_verification(task.task_id)
    assert report is not None and report.ok


def test_resume_refused_when_a_completed_step_was_changed(service):
    task = failed_ship(service, fail_at=3)
    # Another hand rewrites what step 1 left.
    apply_field_change(service.store, "orders", "1032", "status",
                       "delivered")
    with pytest.raises(GateError) as exc:
        service.resume_execution(task.task_id)
    assert "step 1" in str(exc.value) and "delivered" in str(exc.value)
    task = service.get_task(task.task_id)
    assert task.status == TaskStatus.EXECUTION_FAILED
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "resume_refused"]
    assert len(refused) == 1
    assert refused[0]["detail"]["code"] == "basis_moved"


def test_resume_refused_when_a_remaining_steps_basis_moved(service):
    task = plan_and_confirm(service, CREDIT_AND_SETTLE)
    task = service.execute(task.task_id, "offline", fail={2: TIMEOUT})
    # The invoice the remaining step rests on changes underneath.
    apply_field_change(service.store, "invoices", "4471", "memo",
                       "paid in full by phone")
    with pytest.raises(GateError) as exc:
        service.resume_execution(task.task_id)
    assert "invoices 4471" in str(exc.value)
    assert service.get_task(task.task_id).status == \
        TaskStatus.EXECUTION_FAILED


def test_resume_rechecks_roles(service):
    task = failed_ship(service)
    with pytest.raises(GateError):
        service.resume_execution(task.task_id, operator="v.ward")
    with pytest.raises(GateError):
        service.resume_execution(task.task_id, operator="ghost")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "execution_refused"]
    assert {e["detail"]["code"] for e in refused} == {
        "role_cannot_execute", "unknown_operator"}


def test_resume_blocked_by_a_role_narrowed_after_the_failure(service):
    task = failed_ship(service)
    raw = service.get_role("operator").model_dump(mode="json")
    raw["fields"]["orders"] = ["carrier", "status"]
    service.update_role("operator", raw, "s.hale")
    with pytest.raises(GateError) as exc:
        service.resume_execution(task.task_id)
    assert "ship_date" in str(exc.value)
    assert service.get_task(task.task_id).status == \
        TaskStatus.EXECUTION_FAILED
    raw["fields"]["orders"] = ["carrier", "ship_date", "status"]
    service.update_role("operator", raw, "s.hale")
    task = service.resume_execution(task.task_id)
    assert task.status == TaskStatus.VERIFIED


def test_resume_and_restart_require_a_failed_execution(service):
    task = plan_and_confirm(service)
    with pytest.raises(GateError):
        service.resume_execution(task.task_id)
    with pytest.raises(GateError):
        service.restart_execution(task.task_id)
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    with pytest.raises(GateError):
        service.resume_execution(task.task_id)


def test_a_resume_that_fails_again_records_a_second_failure(service):
    task = failed_ship(service)
    task = service.resume_execution(task.task_id, fail={3: TIMEOUT})
    assert task.status == TaskStatus.EXECUTION_FAILED
    assert task.failed_step == 3 and task.failure_error == TIMEOUT
    attempts = service.get_attempts(task.task_id)
    assert [a.outcome for a in attempts] == ["failed", "failed"]
    assert [o.seq for o in attempts[1].steps] == [2, 3]
    task = service.resume_execution(task.task_id)
    assert task.status == TaskStatus.VERIFIED


# ----------------------------------------------------------- restart

def test_restart_refused_once_a_create_has_landed(service):
    task = plan_and_confirm(service, CREDIT_AND_SETTLE)
    task = service.execute(task.task_id, "offline", fail={2: TIMEOUT})
    with pytest.raises(GateError) as exc:
        service.restart_execution(task.task_id)
    assert "step 1" in str(exc.value)
    assert "never run twice" in str(exc.value)
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "restart_refused"]
    assert refused[0]["detail"]["code"] == "create_already_applied"
    assert service.get_task(task.task_id).status == \
        TaskStatus.EXECUTION_FAILED
    assert len(service.store.list_records("credit_memos")) == 1


def test_restart_reruns_the_plan_from_step_one(service):
    task = failed_ship(service)
    task = service.restart_execution(task.task_id)
    assert task.status == TaskStatus.VERIFIED
    attempts = service.get_attempts(task.task_id)
    assert len(attempts) == 2
    assert attempts[0].outcome == "failed"  # the record stands
    assert attempts[1].mode == "restart"
    assert [o.seq for o in attempts[1].steps] == [1, 2, 3]
    order = service.store.get_record("orders", "1032")
    assert (order["status"], order["ship_date"], order["carrier"]) == \
        ("shipped", "2026-10-20", "DHL")


def test_restart_refused_when_the_basis_moved(service):
    task = failed_ship(service, fail_at=3)
    apply_field_change(service.store, "orders", "1032", "status",
                       "delivered")
    with pytest.raises(GateError) as exc:
        service.restart_execution(task.task_id)
    assert "step 1" in str(exc.value)
    assert service.get_task(task.task_id).status == \
        TaskStatus.EXECUTION_FAILED


# ----------------------------------------------------------- abandon

def test_abandon_closes_the_task_with_the_partial_state_named(service):
    task = failed_ship(service)
    with pytest.raises(GateError):
        service.abandon(task.task_id, "j.okafor", "   ")
    with pytest.raises(GateError):
        service.abandon(task.task_id, "v.ward", "a real reason")
    task = service.abandon(
        task.task_id, "j.okafor",
        "the shipment is being rebooked by hand on the carrier portal")
    assert task.status == TaskStatus.ABANDONED
    assert task.abandoned_by == "j.okafor"
    assert "rebooked by hand" in task.abandon_reason
    # The partial write stands, named — never cleaned up.
    order = service.store.get_record("orders", "1032")
    assert order["status"] == "shipped" and order["carrier"] == ""
    entry = [e for e in service.audit(task.task_id)
             if e["event"] == "task_abandoned"]
    assert len(entry) == 1
    assert entry[0]["detail"]["partial_state"] == [
        "step 1: orders 1032.status is now 'shipped'"]
    assert entry[0]["detail"]["failed_seq"] == 2
    # ABANDONED is terminal: every execution act now refuses.
    for act in (lambda: service.execute(task.task_id, "offline"),
                lambda: service.resume_execution(task.task_id),
                lambda: service.abandon(task.task_id, "j.okafor",
                                        "again")):
        with pytest.raises(GateError):
            act()


def test_abandon_requires_a_failed_execution(service):
    task = plan_and_confirm(service)
    with pytest.raises(GateError):
        service.abandon(task.task_id, "j.okafor", "no failure here")


# ------------------------------------------------- idempotency guard

def test_reexecuting_a_verified_task_is_refused(service):
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline")


def test_a_second_execute_while_one_is_in_flight_is_refused(service):
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    task = service.execute(task.task_id, "page-agent-live")
    assert task.status == TaskStatus.CONFIRMED  # in flight
    with pytest.raises(GateError) as exc:
        service.execute(task.task_id, "offline")
    assert "in flight" in str(exc.value)
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "execution_refused"]
    assert refused[-1]["detail"]["code"] == "attempt_in_flight"
    # The in-flight attempt is undisturbed and completes normally.
    apply_field_change(service.store, "orders", "1032", "status",
                       "shipped")
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.VERIFIED


def test_execute_on_a_failed_task_is_refused_use_the_exits(service):
    task = failed_ship(service)
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline")


# --------------------------------------------------------- live path

def test_live_failure_stops_the_execution_and_resume_rearms(service):
    task = plan_and_confirm(service, CREDIT_AND_SETTLE)
    service.execute(task.task_id, "page-agent-live")
    create_credit_memo(
        service.store,
        {"invoice_id": "4471", "amount": "120.00",
         "reason": "damaged goods", "status": "issued"},
        task_id=task.task_id)
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.CONFIRMED
    task = service.report_step(
        task.task_id, 2,
        StepReportIn(ok=False, detail=NOT_FOUND))
    assert task.status == TaskStatus.EXECUTION_FAILED
    assert task.failed_step == 2 and task.failure_error == NOT_FOUND
    with pytest.raises(GateError):
        service.report_step(task.task_id, 2, StepReportIn(ok=True))
    # Resume re-arms the same driver for the remaining step only.
    task = service.resume_execution(task.task_id)
    assert task.status == TaskStatus.CONFIRMED
    attempts = service.get_attempts(task.task_id)
    assert len(attempts) == 2 and attempts[1].mode == "resume"
    with pytest.raises(GateError):
        service.report_step(task.task_id, 1, StepReportIn(ok=True))
    apply_field_change(service.store, "invoices", "4471", "status",
                       "paid")
    task = service.report_step(task.task_id, 2, StepReportIn(ok=True))
    assert task.status == TaskStatus.VERIFIED
    assert len(service.store.list_records("credit_memos")) == 1


def test_live_restart_rearms_the_whole_plan(service):
    task = plan_and_confirm(service)
    service.execute(task.task_id, "page-agent-live")
    apply_field_change(service.store, "orders", "1032", "status",
                       "shipped")
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    task = service.report_step(
        task.task_id, 2, StepReportIn(ok=False, detail=TIMEOUT))
    assert task.status == TaskStatus.EXECUTION_FAILED
    task = service.restart_execution(task.task_id)
    assert task.status == TaskStatus.CONFIRMED
    attempts = service.get_attempts(task.task_id)
    assert attempts[1].mode == "restart"
    for seq in (1, 2, 3):
        if seq == 2:
            apply_field_change(service.store, "orders", "1032",
                               "ship_date", "2026-10-20")
        if seq == 3:
            apply_field_change(service.store, "orders", "1032",
                               "carrier", "DHL")
        task = service.report_step(task.task_id, seq,
                                   StepReportIn(ok=True))
    assert task.status == TaskStatus.VERIFIED


# ------------------------------------------------------------ surfaces

@pytest.fixture()
def client_and_service():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app), app.state.service


def test_attempts_and_recovery_over_http(client_and_service):
    client, svc = client_and_service
    task = plan_and_confirm(svc)
    task = svc.execute(task.task_id, "offline", fail={2: NOT_FOUND})
    tid = task.task_id

    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["task"]["status"] == "EXECUTION_FAILED"
    assert detail["attempts"][0]["failed_seq"] == 2
    attempts = client.get(f"/api/tasks/{tid}/attempts").json()
    assert len(attempts) == 1 and attempts[0]["error"] == NOT_FOUND
    assert client.get("/api/tasks/TASK-NOPE/attempts").status_code == 404

    resumed = client.post(f"/api/tasks/{tid}/resume",
                          json={"operator": "j.okafor"})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "VERIFIED"

    blocked = client.post(f"/api/tasks/{tid}/resume",
                          json={"operator": "j.okafor"})
    assert blocked.status_code == 409
    blocked = client.post(f"/api/tasks/{tid}/abandon",
                          json={"operator": "j.okafor",
                                "reason": "nothing failed"})
    assert blocked.status_code == 409


def test_abandon_over_http(client_and_service):
    client, svc = client_and_service
    task = plan_and_confirm(svc, CREDIT_AND_SETTLE)
    task = svc.execute(task.task_id, "offline", fail={2: TIMEOUT})
    tid = task.task_id
    no_reason = client.post(f"/api/tasks/{tid}/abandon",
                            json={"operator": "j.okafor",
                                  "reason": ""})
    assert no_reason.status_code == 409
    done = client.post(f"/api/tasks/{tid}/abandon",
                       json={"operator": "j.okafor",
                             "reason": "the desk will settle 4471 "
                                       "by hand"})
    assert done.status_code == 200
    assert done.json()["status"] == "ABANDONED"
    restart = client.post(f"/api/tasks/{tid}/restart",
                          json={"operator": "j.okafor"})
    assert restart.status_code == 409


def test_sqlite_failure_and_resume_round_trip(sqlite_service):
    task = plan_and_confirm(sqlite_service)
    task = sqlite_service.execute(task.task_id, "offline",
                                  fail={2: NOT_FOUND})
    assert task.status == TaskStatus.EXECUTION_FAILED
    task = sqlite_service.resume_execution(task.task_id)
    assert task.status == TaskStatus.VERIFIED
    attempts = sqlite_service.get_attempts(task.task_id)
    assert [a.mode for a in attempts] == ["execute", "resume"]
    assert attempts[0].error == NOT_FOUND
