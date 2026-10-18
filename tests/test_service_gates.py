"""The state machine: no execution without confirmation, and no
success without an exact post-state match."""

import pytest

from page_agent_copilot.models import StepReportIn, TaskStatus
from page_agent_copilot.records import apply_field_change, create_credit_memo
from page_agent_copilot.service import GateError

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")


def plan_and_confirm(service, text=CREDIT):
    task = service.intake(text, "j.okafor")
    assert task.status == TaskStatus.PLANNED
    return service.confirm(task.task_id, "j.okafor")


def test_unconfirmed_execution_is_refused(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline")
    assert service.get_task(task.task_id).status == TaskStatus.PLANNED


def test_confirm_requires_planned(service):
    task = service.intake(CREDIT, "j.okafor")
    service.confirm(task.task_id, "j.okafor")
    with pytest.raises(GateError):
        service.confirm(task.task_id, "j.okafor")


def test_happy_path_verifies_exactly(service):
    task = plan_and_confirm(service)
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    memos = service.store.list_records("credit_memos")
    assert len(memos) == 1
    assert memos[0]["invoice_id"] == "4471"
    assert memos[0]["amount"] == "120.00"
    assert memos[0]["reason"] == "damaged goods"
    assert memos[0]["created_for_task"] == task.task_id
    events = [e["event"] for e in service.audit(task.task_id)]
    assert events == [
        "task_created", "plan_created", "plan_confirmed",
        "step_executed", "execution_completed", "verification_passed"]


def test_step_audit_carries_before_and_after(service):
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    service.execute(task.task_id, "offline")
    step_events = [e for e in service.audit(task.task_id)
                   if e["event"] == "step_executed"]
    assert step_events[0]["detail"]["before"] == {"status": "packed"}
    assert step_events[0]["detail"]["after"] == {"status": "shipped"}
    assert step_events[0]["actor"] == "offline"


def test_tampered_write_is_a_discrepancy_never_a_success(service):
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    task = service.execute(task.task_id, "offline", tamper={1: "placed"})
    assert task.status == TaskStatus.DISCREPANCY
    failed = [e for e in service.audit(task.task_id)
              if e["event"] == "verification_failed"]
    assert len(failed) == 1
    diff = failed[0]["detail"]["diffs"][0]
    assert diff["field"] == "status"
    assert diff["planned"] == "shipped"
    assert diff["actual"] == "placed"


def test_double_execution_refused(service):
    task = plan_and_confirm(service)
    service.execute(task.task_id, "offline")
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline")


def test_live_flow_reports_then_verifies(service):
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    task = service.execute(task.task_id, "page-agent-live")
    assert task.status == TaskStatus.CONFIRMED  # armed, awaiting reports
    # The "browser" does the work through the console's own mutation
    # path, then reports. The report claims success either way;
    # verification reads the store.
    apply_field_change(service.store, "orders", "1032", "status", "shipped")
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.VERIFIED
    events = [e["event"] for e in service.audit(task.task_id)]
    assert "execution_started" in events and "step_reported" in events


def test_live_flow_lying_report_still_fails_verification(service):
    task = plan_and_confirm(service, "mark order 1032 as shipped")
    service.execute(task.task_id, "page-agent-live")
    # The browser changes nothing but reports success. The shell
    # diffs the store, not the report: DISCREPANCY.
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.DISCREPANCY


def test_live_credit_memo_flow(service):
    task = plan_and_confirm(service)
    service.execute(task.task_id, "page-agent-live")
    create_credit_memo(
        service.store,
        {"invoice_id": "4471", "amount": "120.00",
         "reason": "damaged goods", "status": "issued"},
        task_id=task.task_id)
    task = service.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.VERIFIED


def test_step_report_requires_an_armed_task(service):
    task = plan_and_confirm(service)
    with pytest.raises(GateError):
        service.report_step(task.task_id, 1, StepReportIn(ok=True))


def test_duplicate_step_report_refused(service):
    task = plan_and_confirm(
        service, "raise a credit memo for invoice 4471, reason damaged "
                 "goods, $120")
    service.execute(task.task_id, "page-agent-live")
    service.report_step(task.task_id, 1, StepReportIn(ok=True))
    # The task verified (and failed: nothing was created) — a second
    # report is refused because the task no longer awaits reports.
    with pytest.raises(GateError):
        service.report_step(task.task_id, 1, StepReportIn(ok=True))


def test_sqlite_backed_service_round_trip(sqlite_service):
    task = plan_and_confirm(sqlite_service)
    task = sqlite_service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
