"""Budgets: per-task ceilings, enforced in the shell.

The ceilings live in Policy and bind at three moments: a plan
longer than `max_plan_steps` refuses at plan time with the counts
named; an attempt that would run past `max_execution_steps` steps
or `max_execution_seconds` of wall clock is stopped by the shell
and lands as a failed execution naming the budget — the partial
state standing, the failure exits (resume, restart, abandon)
unchanged. The attempt is the budget unit: its record snapshots
the ceilings it ran under, and a resume spends a fresh budget.
PageAgent's `maxSteps`, served at /api/agent-config, is the
Policy ceiling under the library's name — one number, one home.
"""

from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.batches import BatchMemberIn
from page_agent_copilot.config import PageAgentConfig, Policy, Settings
from page_agent_copilot.models import StepReportIn, TaskStatus
from page_agent_copilot.service import CopilotService
from page_agent_copilot.store import InMemoryStore, SqlStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
SHIP = ("mark order 1032 as shipped, ship date 2026-10-20, "
        "carrier DHL")
COMPOUND3 = ("mark order 1032 as shipped and mark order 1036 as "
             "packed and mark invoice 4471 as paid")


def budgeted(**policy_kwargs) -> CopilotService:
    settings = Settings(store_kind="memory", store_target="",
                        policy=Policy(**policy_kwargs),
                        page_agent=PageAgentConfig())
    svc = CopilotService(settings, InMemoryStore())
    svc.seed()
    return svc


# -- Policy ---------------------------------------------------------------

def test_policy_budget_defaults():
    policy = Policy()
    assert policy.max_plan_steps == 25
    assert policy.max_execution_steps == 40
    assert policy.max_execution_seconds == 1800


def test_policy_budgets_from_env(monkeypatch):
    monkeypatch.setenv("COPILOT_MAX_PLAN_STEPS", "7")
    monkeypatch.setenv("COPILOT_MAX_EXECUTION_STEPS", "9")
    monkeypatch.setenv("COPILOT_MAX_EXECUTION_SECONDS", "60")
    policy = Policy.from_env()
    assert policy.max_plan_steps == 7
    assert policy.max_execution_steps == 9
    assert policy.max_execution_seconds == 60


# -- the plan-time ceiling --------------------------------------------------

def test_plan_over_the_ceiling_refuses_with_counts():
    svc = budgeted(max_plan_steps=2)
    task = svc.intake(COMPOUND3, "op")
    assert task.status == TaskStatus.REFUSED
    assert "plan_too_large" in (task.refusal_reason or "")
    assert "3 steps" in task.refusal_reason
    assert "of 2 plan steps" in task.refusal_reason
    # Nothing was stored to confirm: a refused plan has no version.
    assert task.plan_version is None
    assert svc.store.list_records("credit_memos") == []


def test_plan_at_the_ceiling_still_plans():
    svc = budgeted(max_plan_steps=3)
    task = svc.intake(COMPOUND3, "op")
    assert task.status == TaskStatus.PLANNED
    assert len(svc.get_plan(task.task_id).steps) == 3


def test_plan_ceiling_binds_replan_and_templates_alike():
    # The check sits in the planner's one entry point, so every
    # door that derives a plan faces it — here, a batch member
    # (which states through intake) refuses into the batch.
    svc = budgeted(max_plan_steps=2)
    batch = svc.create_batch("op", [BatchMemberIn(text=COMPOUND3)])
    member = svc.get_task(batch.members[0].task_id)
    assert member.status == TaskStatus.REFUSED
    assert "plan_too_large" in (member.refusal_reason or "")


# -- the execution ceilings -------------------------------------------------

def test_offline_attempt_stops_at_the_step_ceiling():
    svc = budgeted(max_execution_steps=2)
    task = svc.intake(SHIP, "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    assert task.status == TaskStatus.EXECUTION_FAILED
    assert task.failed_step == 3
    assert "budget exhausted" in (task.failure_error or "")
    assert "step 3 was not run" in task.failure_error
    attempt = svc.get_attempts(task.task_id)[0]
    assert attempt.outcome == "failed"
    assert attempt.budget_exhausted is True
    assert attempt.exhaustion_reason == task.failure_error
    assert attempt.step_ceiling == 2
    assert [o.seq for o in attempt.steps] == [1, 2, 3]
    assert [o.ok for o in attempt.steps] == [True, True, False]
    # The steps that landed stand; the forbidden step never ran.
    order = svc.store.get_record("orders", "1032")
    assert order["status"] == "shipped"
    assert order["ship_date"] == "2026-10-20"
    assert order["carrier"] == ""
    assert attempt.state_lines == [
        "step 1: orders 1032.status is now 'shipped'",
        "step 2: orders 1032.ship_date is now '2026-10-20'"]
    # The exhaustion is on the trail, with its counts.
    events = [e for e in svc.audit(task.task_id)
              if e["event"] == "budget_exhausted"]
    assert len(events) == 1
    assert events[0]["detail"]["steps_consumed"] == 2
    assert events[0]["detail"]["step_ceiling"] == 2
    failed = [e for e in svc.audit(task.task_id)
              if e["event"] == "execution_failed"][0]
    assert failed["detail"]["budget_exhausted"] is True
    assert svc.get_verification(task.task_id) is None


def test_live_reports_stop_at_the_step_ceiling():
    svc = budgeted(max_execution_steps=1)
    task = svc.intake(CREDIT + " and mark invoice 4471 as paid",
                      "op")
    svc.confirm(task.task_id, "s.hale")
    svc.execute(task.task_id, "page-agent-live")
    task = svc.report_step(task.task_id, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.CONFIRMED  # accepted, in flight
    task = svc.report_step(task.task_id, 2, StepReportIn(ok=True))
    assert task.status == TaskStatus.EXECUTION_FAILED
    assert task.failed_step == 2
    assert "budget exhausted" in (task.failure_error or "")
    attempt = svc.get_attempts(task.task_id)[0]
    assert attempt.budget_exhausted is True
    # The refused report is still on the trail as data.
    reported = [e for e in svc.audit(task.task_id)
                if e["event"] == "step_reported"]
    assert [e["detail"]["seq"] for e in reported] == [1, 2]


def test_wall_clock_ceiling_stops_the_first_step():
    svc = budgeted(max_execution_seconds=0)
    task = svc.intake(SHIP, "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    assert task.status == TaskStatus.EXECUTION_FAILED
    assert task.failed_step == 1
    assert "wall-clock ceiling of 0s" in (task.failure_error or "")
    attempt = svc.get_attempts(task.task_id)[0]
    assert attempt.budget_exhausted is True
    assert attempt.seconds_ceiling == 0
    assert len(attempt.steps) == 1  # only the un-run step's record
    assert attempt.state_lines == []
    order = svc.store.get_record("orders", "1032")
    assert order["status"] == "packed"  # nothing ran at all


def test_attempts_snapshot_the_ceilings_they_ran_under(service):
    task = service.intake(CREDIT, "op")
    service.confirm(task.task_id, "s.hale")
    service.execute(task.task_id, "offline")
    attempt = service.get_attempts(task.task_id)[0]
    assert attempt.step_ceiling == Policy().max_execution_steps
    assert attempt.seconds_ceiling == Policy().max_execution_seconds
    assert attempt.budget_exhausted is False
    assert attempt.exhaustion_reason is None


def test_resume_after_exhaustion_spends_a_fresh_budget():
    svc = budgeted(max_execution_steps=2)
    task = svc.intake(SHIP, "op")
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    assert task.status == TaskStatus.EXECUTION_FAILED
    task = svc.resume_execution(task.task_id)
    assert task.status == TaskStatus.VERIFIED
    attempts = svc.get_attempts(task.task_id)
    assert len(attempts) == 2
    # Across the task, four step outcomes were consumed against a
    # ceiling of two — proof the budget is per attempt, not per
    # task: attempt 2 ran its step under its own fresh ceiling.
    assert sum(len(a.steps) for a in attempts) == 4
    assert attempts[1].step_ceiling == 2
    assert attempts[1].budget_exhausted is False
    order = svc.store.get_record("orders", "1032")
    assert (order["status"], order["ship_date"], order["carrier"]) \
        == ("shipped", "2026-10-20", "DHL")


def test_exhaustion_round_trips_on_sqlite(tmp_path):
    target = str(tmp_path / "copilot.db")
    settings = Settings(store_kind="sqlite", store_target=target,
                        policy=Policy(max_execution_steps=2),
                        page_agent=PageAgentConfig())
    svc = CopilotService(settings, SqlStore("sqlite", target))
    svc.seed()
    task = svc.intake(SHIP, "op")
    svc.confirm(task.task_id, "s.hale")
    svc.execute(task.task_id, "offline")
    # A fresh service over the same store reads the same record.
    svc2 = CopilotService(settings, SqlStore("sqlite", target))
    attempt = svc2.get_attempts(task.task_id)[0]
    assert attempt.budget_exhausted is True
    assert attempt.step_ceiling == 2
    assert "budget exhausted" in (attempt.exhaustion_reason or "")


# -- the surfaces -----------------------------------------------------------

def test_agent_config_serves_the_policy_ceiling():
    settings = Settings(store_kind="memory", store_target="",
                        policy=Policy(max_execution_steps=7),
                        page_agent=PageAgentConfig())
    client = TestClient(create_app(settings, InMemoryStore(),
                                   seed=True))
    cfg = client.get("/api/agent-config").json()
    assert cfg["maxSteps"] == 7


def test_budget_report_folds_from_the_attempts():
    svc = budgeted(max_execution_steps=2)
    ship = svc.intake(SHIP, "op")
    svc.confirm(ship.task_id, "s.hale")
    svc.execute(ship.task_id, "offline")  # exhausts at step 3
    credit = svc.intake(CREDIT, "op")
    svc.confirm(credit.task_id, "s.hale")
    svc.execute(credit.task_id, "offline", operator="s.hale")
    rows = {r["operator"]: r for r in svc.budget_report()}
    # op executed the ship task: one attempt, three outcomes
    # consumed (two landed, the forbidden step's record), one
    # exhaustion. s.hale executed the credit: one clean step.
    assert rows["op"]["attempts_run"] == 1
    assert rows["op"]["tasks_executed"] == 1
    assert rows["op"]["steps_consumed"] == 3
    assert rows["op"]["budget_exhaustions"] == 1
    assert rows["s.hale"]["attempts_run"] == 1
    assert rows["s.hale"]["steps_consumed"] == 1
    assert rows["s.hale"]["budget_exhaustions"] == 0
    assert rows["v.ward"]["attempts_run"] == 0


def test_budget_report_over_http():
    settings = Settings.from_env()
    client = TestClient(create_app(settings, InMemoryStore(),
                                   seed=True))
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    client.post(f"/api/tasks/{tid}/confirm",
                json={"operator": "s.hale"})
    client.post(f"/api/tasks/{tid}/execute",
                json={"driver": "offline"})
    rows = client.get("/api/operators/budget").json()
    mine = [r for r in rows if r["operator"] == "j.okafor"]
    assert len(mine) == 1
    assert mine[0]["tasks_executed"] == 1
    assert mine[0]["steps_consumed"] == 1
    assert mine[0]["budget_exhaustions"] == 0
