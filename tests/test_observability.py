"""Round 15 — observability: the execution trace, the failure
taxonomy, and the daily digest.

Each read is a fold over the records the shell already keeps.
The trace test folds a hand-built multi-attempt task planted
directly in the store and matches the trace exactly; the
taxonomy pins drive one real flow per ending class; the digest
tests grade the stored snapshot against an independent
recount from the task records.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import PageAgentConfig, Policy, Settings
from page_agent_copilot.models import (
    ExecutionAttempt,
    PageNavigation,
    Plan,
    PlanStep,
    StepAction,
    StepOutcome,
    Task,
    TaskStatus,
    VerificationReport,
)
from page_agent_copilot.records import apply_field_change
from page_agent_copilot.service import CopilotService, GateError
from page_agent_copilot.store import InMemoryStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
SHIP3 = ("mark order 1032 as shipped, ship date 2026-10-20, "
         "carrier DHL")
DESCRIPTOR = ("raise a credit memo for the open invoice, reason "
              "damaged goods, $120")


def fresh_with(policy) -> CopilotService:
    svc = CopilotService(
        Settings(store_kind="memory", store_target="",
                 policy=policy, page_agent=PageAgentConfig()),
        InMemoryStore())
    svc.seed()
    return svc


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


# ------------------------------------------------------------- the trace

def test_trace_folds_a_hand_built_multi_attempt_task_exactly(service):
    """Plant the records of a two-attempt execution directly in
    the store — a failed first attempt that crossed pages, a
    resumed second attempt, a passing verification — and match
    the folded trace field for field. The fold is the unit
    under test: no service act runs between planting and
    reading."""
    store = service.store
    task = Task(
        task_id="TASK-TRACE1", text="ship it", operator="op",
        planned_by="op", status=TaskStatus.VERIFIED, plan_version=1,
        confirmed_by="s.hale", confirmed_version=1,
        created_at="2026-10-18T09:59:00+00:00",
        updated_at="2026-10-18T10:05:03+00:00")
    store.put_task(task.model_dump(mode="json"))
    plan = Plan(
        task_id="TASK-TRACE1", version=1, task_type="compound",
        summary="three steps, two pages",
        steps=[
            PlanStep(seq=1, action=StepAction.SET, page="Orders",
                     record_kind="orders", record_id="1032",
                     field="status", value="shipped",
                     instruction="set the status"),
            PlanStep(seq=2, action=StepAction.CREATE,
                     page="Invoices", record_kind="credit_memos",
                     fields={"invoice_id": "4471",
                             "amount": "120.00"},
                     instruction="issue the memo"),
            PlanStep(seq=3, action=StepAction.SET, page="Invoices",
                     record_kind="invoices", record_id="4471",
                     field="status", value="paid",
                     instruction="settle the invoice"),
        ],
        created_at="2026-10-18T09:59:30+00:00")
    store.put_plan(plan.model_dump(mode="json"))
    first = ExecutionAttempt(
        task_id="TASK-TRACE1", number=1, driver="offline",
        executor="op", plan_version=1, mode="execute",
        outcome="failed",
        started_at="2026-10-18T10:00:00+00:00",
        finished_at="2026-10-18T10:00:07+00:00",
        steps=[
            StepOutcome(seq=1, driver="offline", ok=True,
                        before={"status": "packed"},
                        after={"status": "shipped"},
                        detail="set orders 1032.status"),
            StepOutcome(seq=2, driver="offline", ok=False,
                        detail="element not found: memo form"),
        ],
        navigations=[PageNavigation(
            seq=2, from_page="Orders", to_page="Invoices",
            driver="offline")],
        failed_seq=2, error="element not found: memo form",
        state_lines=["step 1: orders 1032.status = shipped"],
        step_ceiling=40, seconds_ceiling=1800)
    store.upsert_record("executions", {
        "id": "TASK-TRACE1:1", **first.model_dump(mode="json")})
    second = ExecutionAttempt(
        task_id="TASK-TRACE1", number=2, driver="offline",
        executor="op", plan_version=1, mode="resume",
        outcome="completed", base_attempt=1,
        started_at="2026-10-18T10:05:00+00:00",
        finished_at="2026-10-18T10:05:03+00:00",
        steps=[
            StepOutcome(seq=2, driver="offline", ok=True,
                        after={"id": "CM-1", "amount": "120.00"},
                        detail="created credit memo CM-1"),
            StepOutcome(seq=3, driver="offline", ok=True,
                        before={"status": "open"},
                        after={"status": "paid"},
                        detail="set invoices 4471.status"),
        ],
        state_lines=["step 2: credit memo CM-1 created"],
        step_ceiling=40, seconds_ceiling=1800)
    store.upsert_record("executions", {
        "id": "TASK-TRACE1:2", **second.model_dump(mode="json")})
    report = VerificationReport(
        task_id="TASK-TRACE1", plan_version=1, driver="offline",
        read_path="console_read_surface",
        verified_at="2026-10-18T10:05:03+00:00", ok=True, checked=5)
    store.upsert_record("verifications", {
        "id": "TASK-TRACE1", **report.model_dump(mode="json")})

    trace = service.task_trace("TASK-TRACE1")
    assert trace == {
        "task_id": "TASK-TRACE1",
        "status": "VERIFIED",
        "plan_version": 1,
        "ending": {"label": "verified", "detail": {}},
        "attempts": [
            {"number": 1, "mode": "execute", "driver": "offline",
             "executor": "op", "plan_version": 1,
             "outcome": "failed",
             "started_at": "2026-10-18T10:00:00+00:00",
             "finished_at": "2026-10-18T10:00:07+00:00",
             "duration_seconds": 7.0,
             "budget": {"step_ceiling": 40,
                        "seconds_ceiling": 1800,
                        "steps_consumed": 2,
                        "budget_exhausted": False,
                        "exhaustion_reason": None},
             "events": [
                 {"type": "step", "seq": 1, "ok": True,
                  "driver": "offline",
                  "before": {"status": "packed"},
                  "after": {"status": "shipped"},
                  "detail": "set orders 1032.status"},
                 {"type": "navigation", "seq": 2,
                  "from_page": "Orders", "to_page": "Invoices",
                  "driver": "offline"},
                 {"type": "step", "seq": 2, "ok": False,
                  "driver": "offline", "before": {}, "after": {},
                  "detail": "element not found: memo form"},
             ],
             "failed_seq": 2,
             "error": "element not found: memo form",
             "state_lines": ["step 1: orders 1032.status = shipped"]},
            {"number": 2, "mode": "resume", "driver": "offline",
             "executor": "op", "plan_version": 1,
             "outcome": "completed",
             "started_at": "2026-10-18T10:05:00+00:00",
             "finished_at": "2026-10-18T10:05:03+00:00",
             "duration_seconds": 3.0,
             "budget": {"step_ceiling": 40,
                        "seconds_ceiling": 1800,
                        "steps_consumed": 2,
                        "budget_exhausted": False,
                        "exhaustion_reason": None},
             "events": [
                 {"type": "step", "seq": 2, "ok": True,
                  "driver": "offline", "before": {},
                  "after": {"id": "CM-1", "amount": "120.00"},
                  "detail": "created credit memo CM-1"},
                 {"type": "step", "seq": 3, "ok": True,
                  "driver": "offline",
                  "before": {"status": "open"},
                  "after": {"status": "paid"},
                  "detail": "set invoices 4471.status"},
             ],
             "failed_seq": None, "error": None,
             "state_lines": ["step 2: credit memo CM-1 created"]},
        ],
        "verification": {
            "ok": True, "checked": 5, "plan_version": 1,
            "driver": "offline",
            "read_path": "console_read_surface",
            "verified_at": "2026-10-18T10:05:03+00:00",
            "diffs": []},
    }


def test_trace_of_an_unexecuted_task_is_empty_but_labelled(service):
    task = service.intake(CREDIT, "op")
    trace = service.task_trace(task.task_id)
    assert trace["attempts"] == []
    assert trace["verification"] is None
    assert trace["ending"] == {
        "label": "in_progress", "detail": {"status": "PLANNED"}}


# ------------------------------------------------------------ the taxonomy

def test_taxonomy_verified_and_in_progress(service):
    task = service.intake(CREDIT, "op")
    assert service.task_ending(task)["label"] == "in_progress"
    service.confirm(task.task_id, "s.hale")
    service.execute(task.task_id, "offline")
    task = service.get_task(task.task_id)
    assert service.task_ending(task) == {
        "label": "verified", "detail": {}}


def test_taxonomy_refused_at_plan_carries_the_code(service):
    task = service.intake(
        "raise a credit memo for invoice 4471, reason damaged "
        "goods, $900", "op")
    assert task.status == TaskStatus.REFUSED
    ending = service.task_ending(task)
    assert ending["label"] == "refused_at_plan"
    assert ending["detail"]["code"] == "credit_above_cap"


def test_taxonomy_clarification_exhausted_is_its_own_class(service):
    setup = service.intake(CREDIT, "op")
    service.confirm(setup.task_id, "s.hale")
    service.execute(setup.task_id, "offline")
    task = service.intake(
        "raise a credit memo for the open invoice, reason "
        "damaged goods and mark invoice 4472 as o", "op")
    task = service.answer_clarification(task.task_id, "op", "4471")
    task = service.answer_clarification(task.task_id, "op",
                                        "120.00")
    assert task.status == TaskStatus.REFUSED
    ending = service.task_ending(task)
    assert ending["label"] == "clarification_exhausted"
    assert ending["detail"]["questions"] == 2


def test_taxonomy_stale_names_the_moved_records(service):
    task = service.intake(SHIP3, "op")
    service.confirm(task.task_id, "s.hale")
    apply_field_change(service.store, "orders", "1032", "carrier",
                       "UPS")
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline")
    task = service.get_task(task.task_id)
    assert task.status == TaskStatus.STALE
    ending = service.task_ending(task)
    assert ending["label"] == "stale"
    assert ending["detail"]["records"] == ["orders 1032"]


def test_taxonomy_execution_failed_driver_error(service):
    task = service.intake(SHIP3, "op")
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline",
                           fail={2: "form rejected by the console"})
    assert task.status == TaskStatus.EXECUTION_FAILED
    ending = service.task_ending(task)
    assert ending["label"] == "execution_failed"
    assert ending["detail"]["subreason"] == "driver_error"
    assert ending["detail"]["failed_step"] == 2
    assert ending["detail"]["error"] == (
        "form rejected by the console")


def test_taxonomy_execution_failed_budget_exhausted():
    svc = fresh_with(Policy(max_execution_steps=2))
    task = svc.intake(SHIP3, "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    assert task.status == TaskStatus.EXECUTION_FAILED
    ending = svc.task_ending(task)
    assert ending["label"] == "execution_failed"
    assert ending["detail"]["subreason"] == "budget_exhausted"


def test_taxonomy_discrepancy_names_its_differences(service):
    task = service.intake("mark order 1036 as packed", "op")
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline",
                           tamper={1: "placed"})
    assert task.status == TaskStatus.DISCREPANCY
    ending = service.task_ending(task)
    assert ending["label"] == "discrepancy"
    assert ending["detail"]["diffs"][0]["field"] == "status"
    assert ending["detail"]["diffs"][0]["planned"] == "packed"
    assert ending["detail"]["diffs"][0]["actual"] == "placed"


def test_taxonomy_discrepancy_accepted_keeps_the_diffs(service):
    task = service.intake("mark order 1036 as packed", "op")
    service.confirm(task.task_id, "s.hale")
    service.execute(task.task_id, "offline", tamper={1: "placed"})
    task = service.accept_deviation(
        task.task_id, "s.hale", "the order was re-packed by hand")
    assert task.status == TaskStatus.DISCREPANCY_ACCEPTED
    ending = service.task_ending(task)
    assert ending["label"] == "discrepancy_accepted"
    assert ending["detail"]["accepted_by"] == "s.hale"
    assert ending["detail"]["diffs"][0]["field"] == "status"


def test_taxonomy_abandoned_carries_the_failure_and_the_reason(
        service):
    task = service.intake(SHIP3, "op")
    service.confirm(task.task_id, "s.hale")
    service.execute(task.task_id, "offline", fail={2: "timeout"})
    task = service.abandon(task.task_id, "op",
                           "the desk re-keyed the shipment by hand")
    assert task.status == TaskStatus.ABANDONED
    ending = service.task_ending(task)
    assert ending["label"] == "abandoned"
    assert ending["detail"]["subreason"] == "driver_error"
    assert ending["detail"]["abandoned_by"] == "op"
    assert ending["detail"]["reason"] == (
        "the desk re-keyed the shipment by hand")


def test_taxonomy_rejected(service):
    task = service.intake(CREDIT, "op")
    task = service.reject(task.task_id, "s.hale")
    assert task.status == TaskStatus.REJECTED
    assert service.task_ending(task)["label"] == "rejected"


# -------------------------------------------------------------- the digest

def _scripted_day(svc) -> dict:
    """The day's mix: a verified credit, a tampered discrepancy,
    an over-cap refusal, and a budget exhaustion resumed to
    verified. Returns the four tasks by role."""
    credited = svc.intake(CREDIT, "op")
    svc.confirm(credited.task_id, "s.hale")
    svc.execute(credited.task_id, "offline")
    discrepancy = svc.intake("mark order 1036 as packed", "op")
    svc.confirm(discrepancy.task_id, "s.hale")
    svc.execute(discrepancy.task_id, "offline",
                tamper={1: "placed"})
    refused = svc.intake(
        "raise a credit memo for invoice 4471, reason damaged "
        "goods, $900", "op")
    exhausted = svc.intake(SHIP3, "op")
    svc.confirm(exhausted.task_id, "s.hale")
    svc.execute(exhausted.task_id, "offline")
    svc.resume_execution(exhausted.task_id)
    return {"credited": credited, "discrepancy": discrepancy,
            "refused": refused, "exhausted": exhausted}


def test_digest_grades_against_an_independent_recount():
    svc = fresh_with(Policy(max_execution_steps=2))
    tasks = _scripted_day(svc)
    digest = svc.generate_digest(today(), "s.hale")

    # The expected fold, recounted here from the task records
    # directly — statuses off the tasks, credits off the stored
    # verification reports read back through the public read,
    # attempts off the attempt records.
    mine = svc.list_tasks()
    assert digest["tasks_total"] == len(mine) == 4
    by_status = {}
    for t in mine:
        by_status[t.status.value] = by_status.get(
            t.status.value, 0) + 1
    assert digest["counts_by_status"] == by_status == {
        "VERIFIED": 2, "DISCREPANCY": 1, "REFUSED": 1}
    assert digest["counts_by_taxonomy"] == {
        "verified": 2, "discrepancy": 1, "refused_at_plan": 1}
    assert digest["refusals_by_code"] == {"credit_above_cap": 1}
    expected_credit = 0.0
    for t in mine:
        if t.status != TaskStatus.VERIFIED:
            continue
        report = svc.get_verification(t.task_id)
        for check in report.field_checks:
            if (check.record_kind == "credit_memos"
                    and check.field == "amount"
                    and check.verdict == "match"):
                expected_credit += float(check.actual)
    assert digest["total_credited"] == f"{expected_credit:.2f}" \
        == "120.00"
    assert digest["credited"] == [{
        "task_id": tasks["credited"].task_id,
        "credited": "120.00"}]
    assert digest["budget_exhaustions"] == {
        "count": 1, "task_ids": [tasks["exhausted"].task_id]}
    assert digest["discrepancies"] == {
        "open": [tasks["discrepancy"].task_id], "accepted": []}
    attempts = [a for t in mine
                for a in svc.get_attempts(t.task_id)]
    assert digest["executions"]["attempts_run"] == len(attempts)
    assert digest["executions"]["steps_consumed"] == sum(
        len(a.steps) for a in attempts)
    assert digest["clarifications"] == {
        "asked": 0, "answered": 0, "awaiting": 0}
    assert digest["version"] == 1
    assert digest["generated_by"] == "s.hale"


def test_digest_regeneration_replaces_and_bumps_the_version():
    svc = fresh_with(Policy(max_execution_steps=2))
    _scripted_day(svc)
    first = svc.generate_digest(today(), "s.hale")
    second = svc.generate_digest(today(), "op")
    assert first["version"] == 1 and second["version"] == 2
    assert second["counts_by_status"] == first["counts_by_status"]
    assert second["total_credited"] == first["total_credited"]
    assert svc.get_digest(today())["version"] == 2
    assert [d["date"] for d in svc.list_digests()] == [today()]


def test_digest_counts_clarifications_asked_answered_awaiting(
        service):
    asked = service.intake(DESCRIPTOR, "op")
    service.answer_clarification(asked.task_id, "op", "4471")
    service.intake(DESCRIPTOR, "op")  # a second, still waiting
    digest = service.generate_digest(today(), "s.hale")
    assert digest["clarifications"] == {
        "asked": 2, "answered": 1, "awaiting": 1}
    assert digest["counts_by_status"] == {
        "PLANNED": 1, "AWAITING_CLARIFICATION": 1}


def test_digest_of_an_empty_day_and_an_ungenerated_day():
    svc = fresh_with(Policy())
    assert svc.get_digest("2020-01-01") is None
    digest = svc.generate_digest("2020-01-01", "s.hale")
    assert digest["tasks_total"] == 0
    assert digest["total_credited"] == "0.00"
    assert digest["counts_by_status"] == {}
    assert svc.get_digest("2020-01-01")["version"] == 1


def test_digest_refuses_a_date_that_is_not_a_date(service):
    with pytest.raises(GateError):
        service.generate_digest("18/10/2026", "s.hale")
    with pytest.raises(GateError):
        service.generate_digest("2026-13-01", "s.hale")
    with pytest.raises(GateError):
        service.generate_digest("2026-1-5", "s.hale")


def test_digest_folds_on_the_sqlite_profile_too(sqlite_service):
    task = sqlite_service.intake(CREDIT, "op")
    sqlite_service.confirm(task.task_id, "s.hale")
    sqlite_service.execute(task.task_id, "offline")
    digest = sqlite_service.generate_digest(today(), "s.hale")
    assert digest["total_credited"] == "120.00"
    assert sqlite_service.get_digest(today())["version"] == 1
    trace = sqlite_service.task_trace(task.task_id)
    assert trace["ending"]["label"] == "verified"


# ------------------------------------------------------------------- API

def test_api_trace_and_ending_reads(client):
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    client.post(f"/api/tasks/{tid}/confirm",
                json={"operator": "s.hale"})
    client.post(f"/api/tasks/{tid}/execute",
                json={"driver": "offline"})
    trace = client.get(f"/api/tasks/{tid}/trace")
    assert trace.status_code == 200
    body = trace.json()
    assert body["ending"] == {"label": "verified", "detail": {}}
    assert [e["seq"] for a in body["attempts"] for e in a["events"]
            if e["type"] == "step"] == [1]
    assert body["verification"]["ok"] is True
    rows = client.get("/api/tasks").json()
    mine = next(r for r in rows if r["task_id"] == tid)
    assert mine["ending"]["label"] == "verified"
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["ending"]["label"] == "verified"
    assert client.get("/api/tasks/TASK-NOPE/trace").status_code \
        == 404


def test_api_digest_round_trip_and_identity_gate(client):
    day = today()
    assert client.get(f"/api/digests/{day}").status_code == 404
    refused = client.post(f"/api/digests/{day}",
                          json={"operator": "nobody"})
    assert refused.status_code == 409
    made = client.post(f"/api/digests/{day}",
                       json={"operator": "s.hale"})
    assert made.status_code == 200
    assert made.json()["version"] == 1
    assert made.json()["date"] == day
    assert client.get(f"/api/digests/{day}").json()["version"] == 1
    again = client.post(f"/api/digests/{day}",
                        json={"operator": "s.hale"})
    assert again.json()["version"] == 2
    listed = client.get("/api/digests").json()
    assert [d["date"] for d in listed] == [day]
