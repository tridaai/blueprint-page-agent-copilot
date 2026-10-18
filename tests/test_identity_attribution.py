"""Round 6 — identity & attribution: the operator directory as
governed records (enrol, assign, deactivate, reactivate — all
recorded supervisor acts), fail-closed deactivation, orphaned
work and its pickup, the attribution completeness check, and the
per-operator report folded from the audit trail."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.models import AmendChangeIn, TaskStatus
from page_agent_copilot.roles import RoleError
from page_agent_copilot.service import CopilotService, GateError
from page_agent_copilot.store import InMemoryStore, SqlStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
SHIP3 = ("mark order 1032 as shipped, ship date 2026-10-20, "
         "carrier DHL")


def refused_codes(service, task_id, event):
    return [e["detail"]["code"] for e in service.audit(task_id)
            if e["event"] == event]


# ------------------------------------------------------ directory acts

def test_enrolment_is_a_recorded_supervisor_act(service):
    entry = service.enroll_operator("s.hale", " A.Okafor ",
                                    "operator")
    # The name is stored normalised; a variant of it resolves.
    assert entry == {"id": "a.okafor", "role": "operator",
                     "active": True}
    assert service.role_of("A.OKAFOR").name == "operator"
    audit = service.role_audit()
    enrolled = [e for e in audit if e["event"] == "operator_enrolled"]
    assert len(enrolled) == 1
    assert enrolled[0]["actor"] == "s.hale"
    assert enrolled[0]["detail"] == {"operator": "a.okafor",
                                     "role": "operator"}
    # Enrolling the same name again — either spelling of it — is
    # refused, and the refusal is recorded.
    with pytest.raises(GateError):
        service.enroll_operator("s.hale", "a.okafor", "viewer")
    refused = [e for e in service.role_audit()
               if e["event"] == "directory_refused"]
    assert refused[-1]["detail"]["code"] == "operator_exists"
    # A non-supervisor may not enrol anyone.
    with pytest.raises(GateError):
        service.enroll_operator("j.okafor", "b.new", "operator")
    refused = [e for e in service.role_audit()
               if e["event"] == "directory_refused"]
    assert refused[-1]["detail"]["code"] == "role_cannot_administer"
    # And a role that does not exist cannot be assigned at the door.
    with pytest.raises(RoleError):
        service.enroll_operator("s.hale", "b.new", "overlord")


def test_enrolled_operator_can_work_end_to_end(service):
    service.enroll_operator("s.hale", "a.okafor", "operator")
    task = service.intake(CREDIT, "a.okafor")
    assert task.status == TaskStatus.PLANNED
    assert task.planned_by == "a.okafor"
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED


def test_assignment_changes_the_role_and_is_recorded(service):
    updated = service.assign_operator_role("s.hale", "v.ward",
                                           "operator")
    assert updated["role"] == "operator"
    # The viewer who could plan nothing can plan now.
    task = service.intake(CREDIT, "v.ward")
    assert task.status == TaskStatus.PLANNED
    entry = [e for e in service.role_audit()
             if e["event"] == "operator_role_changed"][0]
    assert entry["actor"] == "s.hale"
    assert entry["detail"]["old_role"] == "viewer"
    assert entry["detail"]["new_role"] == "operator"
    # Assigning the role already held changes nothing.
    with pytest.raises(GateError):
        service.assign_operator_role("s.hale", "v.ward", "operator")
    # A name that is not enrolled cannot be assigned.
    with pytest.raises(GateError):
        service.assign_operator_role("s.hale", "ghost", "operator")


def test_deactivation_and_reactivation_are_recorded(service):
    entry = service.deactivate_operator("s.hale", "j.okafor")
    assert entry["active"] is False
    # The name stays in the directory — its role still resolves.
    assert service.role_of("j.okafor").name == "operator"
    with pytest.raises(GateError):
        service.deactivate_operator("s.hale", "j.okafor")
    entry = service.reactivate_operator("s.hale", "j.okafor")
    assert entry["active"] is True
    with pytest.raises(GateError):
        service.reactivate_operator("s.hale", "j.okafor")
    events = [(e["event"], e["detail"]["operator"])
              for e in service.role_audit()
              if e["event"] in ("operator_deactivated",
                                "operator_reactivated")]
    assert events == [("operator_deactivated", "j.okafor"),
                      ("operator_reactivated", "j.okafor")]


def test_the_last_administrator_cannot_be_deactivated(service):
    with pytest.raises(GateError) as caught:
        service.deactivate_operator("s.hale", "s.hale")
    assert "last active administrator" in str(caught.value)
    refused = [e for e in service.role_audit()
               if e["event"] == "directory_refused"]
    assert refused[-1]["detail"]["code"] == "last_administrator"
    # With a second administrator enrolled, the first can go.
    service.enroll_operator("s.hale", "t.reyes", "supervisor")
    entry = service.deactivate_operator("t.reyes", "s.hale")
    assert entry["active"] is False


def test_a_deactivated_actor_cannot_administer(service):
    service.enroll_operator("s.hale", "t.reyes", "supervisor")
    service.deactivate_operator("t.reyes", "s.hale")
    with pytest.raises(GateError) as caught:
        service.deactivate_operator("s.hale", "t.reyes")
    assert "operator_inactive" in str(caught.value)
    with pytest.raises(GateError) as caught:
        service.update_role(
            "operator",
            service.get_role("operator").model_dump(mode="json"),
            "s.hale")
    assert "operator_inactive" in str(caught.value)


# ------------------------------------------- fail-closed deactivation

def test_a_deactivated_operator_can_perform_no_act(service):
    planned = service.intake(CREDIT, "j.okafor")
    confirmed = service.intake(
        "update customer 2041 phone to 555-0134", "j.okafor")
    service.confirm(confirmed.task_id, "s.hale")
    failed = service.intake(SHIP3, "op")
    service.confirm(failed.task_id, "s.hale")
    failed = service.execute(failed.task_id, "offline",
                             fail={2: "form rejected"})
    assert failed.status == TaskStatus.EXECUTION_FAILED
    service.deactivate_operator("s.hale", "j.okafor")
    service.deactivate_operator("s.hale", "op")

    # Plan: refused at intake, on the task, with the code.
    refused = service.intake(CREDIT, "j.okafor")
    assert refused.status == TaskStatus.REFUSED
    assert refused.refusal_reason.startswith("operator_inactive")
    # Confirm: refused and recorded.
    with pytest.raises(GateError):
        service.confirm(planned.task_id, "j.okafor")
    assert refused_codes(service, planned.task_id,
                         "confirmation_refused") == ["operator_inactive"]
    # Amend: refused and recorded.
    with pytest.raises(GateError):
        service.amend(planned.task_id, "j.okafor",
                      [AmendChangeIn(seq=1,
                                     fields={"amount": "1.00"})])
    assert refused_codes(service, planned.task_id,
                         "amendment_refused") == ["operator_inactive"]
    # Execute: the deactivated name is the task's own operator,
    # so even the default executor is refused, and recorded.
    with pytest.raises(GateError):
        service.execute(confirmed.task_id, "offline")
    assert refused_codes(service, confirmed.task_id,
                         "execution_refused") == ["operator_inactive"]
    assert service.get_task(
        confirmed.task_id).status == TaskStatus.CONFIRMED
    # Resume and abandon: refused and recorded.
    with pytest.raises(GateError):
        service.resume_execution(failed.task_id, "op")
    assert refused_codes(service, failed.task_id,
                         "execution_refused") == ["operator_inactive"]
    with pytest.raises(GateError):
        service.abandon(failed.task_id, "op", "closing it out")
    assert refused_codes(service, failed.task_id,
                         "abandon_refused") == ["operator_inactive"]
    assert service.get_task(
        failed.task_id).status == TaskStatus.EXECUTION_FAILED
    # Reject: fail-closed too.
    with pytest.raises(GateError) as caught:
        service.reject(planned.task_id, "j.okafor")
    assert "operator_inactive" in str(caught.value)
    # The control: an active supervisor's acts still land.
    task = service.confirm(planned.task_id, "s.hale")
    assert task.status == TaskStatus.CONFIRMED


def test_a_deactivated_operator_is_refused_on_the_discrepancy_exits(
        service):
    service.enroll_operator("s.hale", "t.reyes", "supervisor")
    discrepancy = service.intake("mark order 1036 as shipped", "op")
    service.confirm(discrepancy.task_id, "s.hale")
    discrepancy = service.execute(discrepancy.task_id, "offline",
                                  tamper={1: "placed"})
    assert discrepancy.status == TaskStatus.DISCREPANCY
    other = service.intake("mark order 1032 as shipped", "op")
    service.confirm(other.task_id, "s.hale")
    other = service.execute(other.task_id, "offline",
                            tamper={1: "placed"})
    assert other.status == TaskStatus.DISCREPANCY
    service.deactivate_operator("s.hale", "op")
    service.deactivate_operator("s.hale", "t.reyes")
    with pytest.raises(GateError):
        service.spawn_corrective(other.task_id, "op")
    assert refused_codes(service, other.task_id,
                         "corrective_refused") == ["operator_inactive"]
    with pytest.raises(GateError):
        service.accept_deviation(discrepancy.task_id, "t.reyes",
                                 "stands as written")
    assert refused_codes(service, discrepancy.task_id,
                         "deviation_refused") == ["operator_inactive"]
    # The active supervisor's acceptance still lands.
    task = service.accept_deviation(discrepancy.task_id, "s.hale",
                                    "stands as written")
    assert task.status == TaskStatus.DISCREPANCY_ACCEPTED


# ------------------------------------------------ orphaned work

def test_orphaned_work_surfaces_and_a_pickup_completes_it(service):
    planned = service.intake(CREDIT, "j.okafor")
    confirmed = service.intake(
        "update customer 2041 phone to 555-0134", "j.okafor")
    service.confirm(confirmed.task_id, "s.hale")
    done = service.intake("mark order 1036 as shipped", "j.okafor")
    service.confirm(done.task_id, "s.hale")
    done = service.execute(done.task_id, "offline")
    assert done.status == TaskStatus.VERIFIED
    assert service.orphaned_work() == []

    service.deactivate_operator("s.hale", "j.okafor")
    orphaned = service.orphaned_work()
    # The finished task is not orphaned; the two open ones are,
    # named with their state and their deactivated operator.
    assert {o["task_id"] for o in orphaned} == {
        planned.task_id, confirmed.task_id}
    by_id = {o["task_id"]: o for o in orphaned}
    assert by_id[planned.task_id]["status"] == "PLANNED"
    assert by_id[confirmed.task_id]["status"] == "CONFIRMED"
    assert by_id[planned.task_id]["operator"] == "j.okafor"
    assert by_id[planned.task_id]["planned_by"] == "j.okafor"

    # Pickup is a supervisor's act, not the desk's.
    with pytest.raises(GateError):
        service.pickup_task(planned.task_id, "op")
    assert refused_codes(service, planned.task_id,
                         "pickup_refused") == ["role_cannot_administer"]
    task = service.pickup_task(planned.task_id, "s.hale")
    assert task.operator == "s.hale"
    # The original planner stays on the record, both ways.
    assert task.planned_by == "j.okafor"
    assert task.picked_up_from == "j.okafor"
    picked = [e for e in service.audit(planned.task_id)
              if e["event"] == "task_picked_up"][0]
    assert picked["actor"] == "s.hale"
    assert picked["detail"]["from_operator"] == "j.okafor"
    assert picked["detail"]["to_operator"] == "s.hale"
    # The picked-up work completes through the usual gates.
    service.confirm(planned.task_id, "op")
    task = service.execute(planned.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert {o["task_id"] for o in service.orphaned_work()} == {
        confirmed.task_id}


def test_pickup_refuses_work_that_is_not_orphaned(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError) as caught:
        service.pickup_task(task.task_id, "s.hale")
    assert "not orphaned" in str(caught.value)
    assert refused_codes(service, task.task_id,
                         "pickup_refused") == ["not_orphaned"]


# ------------------------------------------------------- attribution

def test_attribution_is_complete_on_a_full_run(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.confirm(task.task_id, "j.okafor")  # a known refusal
    service.confirm(task.task_id, "s.hale")
    service.execute(task.task_id, "offline")
    service.intake("delete order 1036", "op")  # policy writes this
    service.enroll_operator("s.hale", "a.okafor", "viewer")
    check = service.attribution_check()
    assert check["ok"] is True
    assert check["problems"] == []
    assert check["checked"] == len(service.store.list_all_audit())
    assert check["checked"] > 0
    assert "verifier" in check["system_actors"]


def test_attribution_names_planted_rows(service):
    task = service.intake(CREDIT, "j.okafor")
    service.store.add_audit({
        "task_id": task.task_id, "ts": "2026-10-18T00:00:00+00:00",
        "actor": "", "event": "plan_confirmed", "detail": {}})
    service.store.add_audit({
        "task_id": task.task_id, "ts": "2026-10-18T00:00:01+00:00",
        "actor": "mallory", "event": "plan_confirmed",
        "detail": {}})
    check = service.attribution_check()
    assert check["ok"] is False
    problems = {(p["actor"], p["problem"]) for p in check["problems"]}
    assert ("", "actor_missing") in problems
    assert ("mallory", "actor_unresolvable") in problems
    named = [p for p in check["problems"]
             if p["problem"] == "actor_unresolvable"][0]
    assert named["scope"] == task.task_id
    assert named["event"] == "plan_confirmed"


def test_a_deactivated_names_history_still_attributes(service):
    task = service.intake(CREDIT, "j.okafor")
    service.confirm(task.task_id, "s.hale")
    service.execute(task.task_id, "offline")
    service.deactivate_operator("s.hale", "j.okafor")
    check = service.attribution_check()
    assert check["ok"] is True


# ------------------------------------------------------------ the report

def test_the_operator_report_reconciles_with_the_audit(service):
    service.enroll_operator("s.hale", "a.okafor", "operator")
    task = service.intake(CREDIT, "a.okafor")
    with pytest.raises(GateError):
        service.confirm(task.task_id, "a.okafor")  # self-confirmation
    service.confirm(task.task_id, "s.hale")
    service.execute(task.task_id, "offline")  # executor: a.okafor
    service.intake("raise a credit memo for invoice 4471, reason "
                   "goodwill, $900", "a.okafor")  # refused at plan time
    other = service.intake("mark order 1036 as shipped", "op")
    service.confirm(other.task_id, "s.hale")
    service.execute(other.task_id, "offline")  # executor: op

    rows = {r["operator"]: r for r in service.operator_report()}
    a = rows["a.okafor"]
    assert a["tasks_planned"] == 2
    assert a["tasks_confirmed"] == 0
    assert a["tasks_executed"] == 1
    assert a["refusals_received"] == 2
    assert a["refusals_by_code"] == {
        "credit_above_cap": 1, "self_confirmation": 1}
    hale = rows["s.hale"]
    assert hale["tasks_confirmed"] == 2
    assert hale["acts_by_kind"]["operator_enrolled"] == 1
    assert rows["op"]["tasks_planned"] == 1
    assert rows["op"]["tasks_executed"] == 1
    assert rows["v.ward"]["acts_total"] == 0

    # Reconciliation against the trail itself: the report's sums
    # are the trail's counts, not a second set of books.
    entries = service.store.list_all_audit()
    assert sum(r["tasks_planned"] for r in rows.values()) == sum(
        1 for e in entries if e["event"] == "task_created")
    assert sum(r["tasks_executed"] for r in rows.values()) == sum(
        1 for e in entries if e["event"] == "execution_completed")
    directory = {o["id"] for o in service.list_operators()}
    assert sum(r["acts_total"] for r in rows.values()) == sum(
        1 for e in entries if e["actor"] in directory)


# ------------------------------------------------------------------ API

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def test_directory_over_http(client):
    # A non-supervisor's enrolment is a 409.
    denied = client.post("/api/operators", json={
        "operator": "j.okafor", "name": "a.okafor",
        "role": "operator"})
    assert denied.status_code == 409
    made = client.post("/api/operators", json={
        "operator": "s.hale", "name": "a.okafor", "role": "operator"})
    assert made.status_code == 201
    assert made.json() == {"id": "a.okafor", "role": "operator",
                           "active": True}
    again = client.post("/api/operators", json={
        "operator": "s.hale", "name": "a.okafor", "role": "viewer"})
    assert again.status_code == 409
    bad_role = client.post("/api/operators", json={
        "operator": "s.hale", "name": "b.new", "role": "overlord"})
    assert bad_role.status_code == 422

    # The enrolled operator plans; the supervisor deactivates;
    # the work orphans; the supervisor picks it up and it lands.
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "a.okafor"})
    tid = created.json()["task"]["task_id"]
    off = client.post("/api/operators/a.okafor/deactivate",
                      json={"operator": "s.hale"})
    assert off.status_code == 200
    assert off.json()["active"] is False
    orphaned = client.get("/api/operators/orphaned").json()
    assert [o["task_id"] for o in orphaned] == [tid]
    assert orphaned[0]["operator"] == "a.okafor"
    picked = client.post(f"/api/tasks/{tid}/pickup",
                         json={"operator": "s.hale"})
    assert picked.status_code == 200
    assert picked.json()["operator"] == "s.hale"
    assert picked.json()["planned_by"] == "a.okafor"
    assert client.get("/api/operators/orphaned").json() == []

    # The report and the attribution check read over HTTP too.
    report = client.get("/api/operators/report").json()
    rows = {r["operator"]: r for r in report}
    assert rows["a.okafor"]["tasks_planned"] == 1
    assert rows["a.okafor"]["active"] is False
    check = client.get("/api/attribution").json()
    assert check["ok"] is True and check["checked"] > 0
    # Role assignment rides the same surface.
    assigned = client.put("/api/operators/a.okafor/role", json={
        "operator": "s.hale", "role": "viewer"})
    assert assigned.status_code == 200
    assert assigned.json()["role"] == "viewer"
    back = client.post("/api/operators/a.okafor/reactivate",
                       json={"operator": "s.hale"})
    assert back.status_code == 200
    assert back.json()["active"] is True


def test_directory_state_round_trips_on_sqlite(settings, tmp_path):
    path = str(tmp_path / "copilot.db")
    first = CopilotService(settings, SqlStore("sqlite", path))
    first.seed()
    task = first.intake(CREDIT, "j.okafor")
    first.deactivate_operator("s.hale", "j.okafor")
    # A fresh service over the same file sees the same directory:
    # the deactivation, and the orphaned work it left.
    second = CopilotService(settings, SqlStore("sqlite", path))
    entry = second.directory_entry("j.okafor")
    assert entry["active"] is False
    assert [o["task_id"] for o in second.orphaned_work()] == [
        task.task_id]
    with pytest.raises(GateError):
        second.confirm(task.task_id, "j.okafor")
    picked = second.pickup_task(task.task_id, "s.hale")
    assert picked.operator == "s.hale"
    assert picked.planned_by == "j.okafor"
