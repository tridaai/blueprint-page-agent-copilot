"""Round 18 — dual control: the gate scales with the stakes.

A plan whose total credit reaches the governed threshold, or
that touches a high-stakes field, is not confirmed by one
person: two distinct confirm-capable operators, neither of them
the planner, must each confirm the same plan version. The first
confirmation is a SEAT, recorded on the task; the task stands
PLANNED with the seat filled until the second lands, and
execution from there refuses, naming the confirmer it is owed.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.batches import BatchMemberIn
from page_agent_copilot.config import PageAgentConfig, Policy, Settings
from page_agent_copilot.models import (
    AmendChangeIn,
    Plan,
    PlanStep,
    StepAction,
    TaskStatus,
)
from page_agent_copilot.policy import dual_control_assessment
from page_agent_copilot.service import CopilotService, GateError
from page_agent_copilot.store import InMemoryStore

AT_CAP = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $500")
BELOW = ("raise a credit memo for invoice 4471, reason damaged "
         "goods, $120")
CREDIT_LIMIT = "update customer 2041 credit limit to 30000"


@pytest.fixture()
def second_operator(service):
    """A second confirm-capable operator, enrolled by the
    supervisor as a recorded directory act — the seeded
    directory's two confirm-capable names (the planner and the
    supervisor) cannot fill two non-planner seats alone."""
    service.enroll_operator("s.hale", "r.osei", "operator")
    return "r.osei"


def dual_view(service, task):
    return service.dual_control_view(service.get_task(task.task_id))


# ------------------------------------------------------- the stakes rule

def _plan(steps):
    return Plan(task_id="T", version=1, task_type="test",
                summary="test", steps=steps)


def _create_memo(seq, amount):
    return PlanStep(seq=seq, action=StepAction.CREATE,
                    page="invoices", record_kind="credit_memos",
                    fields={"amount": amount},
                    instruction=f"issue a credit memo of {amount}")


def _set(seq, kind, field, value):
    return PlanStep(seq=seq, action=StepAction.SET, page="customers",
                    record_kind=kind, record_id="2041", field=field,
                    value=value, instruction=f"set {field}")


def test_assessment_amount_at_and_below_the_threshold():
    policy = Policy()
    at = dual_control_assessment(_plan([_create_memo(1, "500.00")]),
                                 policy)
    assert at["required"] is True and at["reasons"] == ["amount"]
    assert at["total_credited"] == "500.00"
    assert at["threshold"] == "500.00"
    below = dual_control_assessment(
        _plan([_create_memo(1, "499.99")]), policy)
    assert below["required"] is False and below["reasons"] == []


def test_assessment_sums_every_memo_the_plan_creates():
    two = _plan([_create_memo(1, "300.00"), _create_memo(2, "250.00")])
    judged = dual_control_assessment(two, Policy())
    assert judged["total_credited"] == "550.00"
    assert judged["required"] is True


def test_assessment_field_reason_and_both_reasons():
    field = dual_control_assessment(
        _plan([_set(1, "customers", "credit_limit", "30000")]),
        Policy())
    assert field["required"] is True and field["reasons"] == ["field"]
    assert field["high_stakes_fields"] == ["customers.credit_limit"]
    assert field["total_credited"] == "0.00"
    both = dual_control_assessment(
        _plan([_create_memo(1, "500.00"),
               _set(2, "customers", "credit_limit", "30000")]),
        Policy())
    assert both["reasons"] == ["amount", "field"]
    plain = dual_control_assessment(
        _plan([_set(1, "customers", "phone", "555-0134")]), Policy())
    assert plain["required"] is False


def test_threshold_defaults_to_the_credit_cap_in_force():
    assert Policy().dual_control_cap == Policy().credit_cap
    assert Policy(credit_cap=Decimal("300.00")).dual_control_cap \
        == Decimal("500.00")  # the field's own default stands alone
    env = Policy.from_env()
    assert env.dual_control_cap == env.credit_cap


# ------------------------------------------------------------ the two seats

def test_at_cap_credit_needs_two_distinct_confirmers(
        service, second_operator):
    task = service.intake(AT_CAP, "j.okafor")
    assert task.status == TaskStatus.PLANNED
    view = dual_view(service, task)
    assert view["required"] is True and view["reasons"] == ["amount"]
    assert view["seats_required"] == 2 and view["seats_filled"] == 0

    # Seat one: the gate does not complete; the task stands
    # PLANNED with the seat on the record.
    task = service.confirm(task.task_id, "s.hale")
    assert task.status == TaskStatus.PLANNED
    assert task.confirmed_by is None and task.confirmed_version is None
    assert [(c.operator, c.method, c.plan_version)
            for c in task.confirmations] == [
                ("s.hale", "confirm", 1)]
    view = dual_view(service, task)
    assert view["seats_filled"] == 1
    assert view["confirmers"] == ["s.hale"]
    assert view["awaiting_second_confirmer"] is True

    # Execution refuses, naming the second confirmer it is owed
    # and the first confirmer who already sits.
    with pytest.raises(GateError) as caught:
        service.execute(task.task_id, "offline")
    assert "dual control" in str(caught.value)
    assert "second, different confirmer" in str(caught.value)
    assert "s.hale" in str(caught.value)

    # The same name again — under any spacing or casing — fills
    # nothing: the seat is already taken.
    with pytest.raises(GateError):
        service.confirm(task.task_id, " S.Hale ")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "confirmation_refused"]
    assert refused[-1]["detail"]["code"] == "already_confirmed"

    # The planner is never a seat, however the seats stand.
    with pytest.raises(GateError):
        service.confirm(task.task_id, "j.okafor")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "confirmation_refused"]
    assert refused[-1]["detail"]["code"] == "self_confirmation"
    assert service.get_task(task.task_id).status == TaskStatus.PLANNED

    # Seat two, a different confirmer: the gate completes, bound
    # to the version both confirmed.
    task = service.confirm(task.task_id, second_operator)
    assert task.status == TaskStatus.CONFIRMED
    assert task.confirmed_version == 1
    assert task.confirmed_by == second_operator
    assert len(task.confirmations) == 2

    # The trail shows both seats and which one completed it.
    seats = [e for e in service.audit(task.task_id)
             if e["event"] == "plan_confirmed"]
    assert [e["detail"]["seat"] for e in seats] == [1, 2]
    assert seats[0]["detail"]["complete"] is False
    assert seats[0]["detail"]["awaiting"] == "second_confirmer"
    assert seats[1]["detail"]["complete"] is True
    assert seats[1]["detail"]["confirmed_version"] == 1

    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    memos = service.store.list_records("credit_memos")
    assert len(memos) == 1 and memos[0]["amount"] == "500.00"


def test_below_threshold_confirms_with_one_person_as_before(service):
    task = service.intake(BELOW, "j.okafor")
    view = dual_view(service, task)
    assert view["required"] is False and view["seats_required"] == 1
    task = service.confirm(task.task_id, "s.hale")
    assert task.status == TaskStatus.CONFIRMED
    assert [(c.operator, c.plan_version) for c in task.confirmations] \
        == [("s.hale", 1)]
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED


def test_field_triggered_plan_needs_two_confirmers(
        service, second_operator):
    task = service.intake(CREDIT_LIMIT, "s.hale")
    view = dual_view(service, task)
    assert view["required"] is True and view["reasons"] == ["field"]
    assert view["high_stakes_fields"] == ["customers.credit_limit"]
    task = service.confirm(task.task_id, "j.okafor")
    assert task.status == TaskStatus.PLANNED
    task = service.confirm(task.task_id, second_operator)
    assert task.status == TaskStatus.CONFIRMED
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.get_record(
        "customers", "2041")["credit_limit"] == "30000"


def test_threshold_is_governed_not_hardcoded():
    svc = CopilotService(
        Settings(store_kind="memory", store_target="",
                 policy=Policy(dual_control_cap=Decimal("100.00")),
                 page_agent=PageAgentConfig()),
        InMemoryStore())
    svc.seed()
    svc.enroll_operator("s.hale", "r.osei", "operator")
    task = svc.intake(BELOW, "j.okafor")  # $120: ordinary stakes
    assert svc.dual_control_view(task)["required"] is True
    task = svc.confirm(task.task_id, "s.hale")
    assert task.status == TaskStatus.PLANNED
    task = svc.confirm(task.task_id, "r.osei")
    assert task.status == TaskStatus.CONFIRMED


# ------------------------------------------------------- read-back seats

def test_readback_fills_a_seat_with_its_token_binding(
        service, second_operator):
    task = service.intake(AT_CAP, "j.okafor")
    issued = service.issue_readback(task.task_id, "s.hale")
    task = service.confirm_readback(task.task_id, "s.hale",
                                    issued["token"])
    # One seat filled by read-back; the gate has not completed.
    assert task.status == TaskStatus.PLANNED
    assert [(c.operator, c.method) for c in task.confirmations] == [
        ("s.hale", "readback")]
    entry = [e for e in service.audit(task.task_id)
             if e["event"] == "plan_confirmed"][0]
    assert entry["detail"]["method"] == "readback"
    assert entry["detail"]["seat"] == 1
    assert entry["detail"]["complete"] is False
    # The spent token spends no second seat.
    with pytest.raises(GateError):
        service.confirm_readback(task.task_id, "s.hale",
                                 issued["token"])
    # A second confirmer's plain confirmation completes it.
    task = service.confirm(task.task_id, second_operator)
    assert task.status == TaskStatus.CONFIRMED
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED


# ------------------------------------------------- amendment resets seats

def test_amendment_resets_the_seats_to_zero(
        service, second_operator):
    task = service.intake(AT_CAP, "j.okafor")
    service.confirm(task.task_id, "s.hale")
    assert len(service.get_task(task.task_id).confirmations) == 1
    task, plan, _ = service.amend(
        task.task_id, "j.okafor",
        [AmendChangeIn(seq=1, fields={"reason": "goodwill"})])
    assert plan.version == 2 and task.plan_version == 2
    assert task.confirmations == []
    amended = [e for e in service.audit(task.task_id)
               if e["event"] == "plan_amended"]
    assert amended[-1]["detail"]["confirmations_reset"] == 1
    # The first confirmer may sit again on the new version —
    # the v1 seat counted for nothing here — and one seat still
    # does not complete the gate.
    task = service.confirm(task.task_id, "s.hale")
    assert task.status == TaskStatus.PLANNED
    assert [(c.operator, c.plan_version) for c in task.confirmations] \
        == [("s.hale", 2)]
    task = service.confirm(task.task_id, second_operator)
    assert task.status == TaskStatus.CONFIRMED
    assert task.confirmed_version == 2
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    memo = service.store.list_records("credit_memos")[0]
    assert memo["amount"] == "500.00" and memo["reason"] == "goodwill"


# ------------------------------------------------------- folded surfaces

def test_run_sheet_names_the_second_confirmation(service,
                                                  second_operator):
    batch = service.create_batch(
        "j.okafor", [BatchMemberIn(text=AT_CAP)])
    task_id = batch.members[0].task_id
    sheet = service.batch_run_sheet(batch)
    row = sheet["members"][0]
    assert row["next_act"]["action"] == "confirm"
    assert "dual control" in row["next_act"]["detail"]
    assert "two distinct confirmers" in row["next_act"]["detail"]
    service.confirm(task_id, "s.hale")
    row = service.batch_run_sheet(batch)["members"][0]
    assert row["next_act"]["action"] == "confirm"
    assert "second confirmation" in row["next_act"]["detail"]
    assert "s.hale" in row["next_act"]["detail"]
    # Who may take it excludes the planner AND the first seat.
    assert "j.okafor" in row["next_act"]["who"]
    assert "s.hale" in row["next_act"]["who"]
    service.confirm(task_id, second_operator)
    row = service.batch_run_sheet(batch)["members"][0]
    assert row["next_act"]["action"] == "execute"


def test_digest_counts_tasks_awaiting_their_second_confirmer(
        service, second_operator):
    task = service.intake(AT_CAP, "j.okafor")
    day = task.created_at[:10]
    service.confirm(task.task_id, "s.hale")
    digest = service.generate_digest(day, "s.hale")
    assert digest["dual_control"]["required"] == [task.task_id]
    assert digest["dual_control"]["awaiting_second_confirmer"] == [
        task.task_id]
    service.confirm(task.task_id, second_operator)
    service.execute(task.task_id, "offline")
    digest = service.generate_digest(day, "s.hale")
    assert digest["version"] == 2
    assert digest["dual_control"]["required"] == [task.task_id]
    assert digest["dual_control"]["awaiting_second_confirmer"] == []


# ------------------------------------------------------------ over HTTP

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def test_dual_control_over_http(client):
    created = client.post("/api/tasks", json={
        "text": AT_CAP, "operator": "j.okafor"})
    assert created.status_code == 201
    plan = created.json()["plan"]
    assert plan["dual_control_required"] is True
    assert plan["dual_control_reasons"] == ["amount"]
    assert plan["dual_control_total_credited"] == "500.00"
    tid = created.json()["task"]["task_id"]

    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["dual_control"]["required"] is True
    assert detail["dual_control"]["seats_required"] == 2
    assert detail["dual_control"]["awaiting_second_confirmer"] is False

    first = client.post(f"/api/tasks/{tid}/confirm",
                        json={"operator": "s.hale"})
    assert first.status_code == 200
    assert first.json()["status"] == "PLANNED"
    assert first.json()["confirmations"][0]["operator"] == "s.hale"

    blocked = client.post(f"/api/tasks/{tid}/execute",
                          json={"operator": "j.okafor"})
    assert blocked.status_code == 409
    assert "second, different confirmer" in blocked.json()["detail"]

    client.app.state.service.enroll_operator("s.hale", "r.osei",
                                             "operator")
    second = client.post(f"/api/tasks/{tid}/confirm",
                         json={"operator": "r.osei"})
    assert second.json()["status"] == "CONFIRMED"
    done = client.post(f"/api/tasks/{tid}/execute",
                       json={"operator": "j.okafor"})
    assert done.json()["status"] == "VERIFIED"
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["dual_control"]["confirmers"] == ["s.hale", "r.osei"]
