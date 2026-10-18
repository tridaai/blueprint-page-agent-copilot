"""Round 3 — permissions: roles as governed records, enforced at
plan time, at confirmation, at amendment, and re-checked at
execution against the definitions then in force."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.models import AmendChangeIn, TaskStatus
from page_agent_copilot.policy import PlanRefusal
from page_agent_copilot.roles import (
    DEFAULT_ROLES,
    RoleError,
    load_role,
)
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
CREDIT_LIMIT = "update customer 2041 credit limit to 30000"


def role_raw(service, name="operator"):
    return service.get_role(name).model_dump(mode="json")


# ------------------------------------------------------------- the data

def test_default_roles_pass_the_strict_loader():
    for raw in DEFAULT_ROLES:
        role = load_role(raw)
        assert role.name == raw["name"]


def test_loader_refuses_ungovernable_fields_and_pages():
    raw = role_raw_dict()
    raw["fields"] = {"orders": ["status", "backdoor"]}
    with pytest.raises(RoleError):
        load_role(raw)
    raw = role_raw_dict()
    raw["pages"] = ["Orders", "Narnia"]
    with pytest.raises(RoleError):
        load_role(raw)


def role_raw_dict():
    return {
        "name": "operator", "pages": ["Orders"], "fields": {},
        "can_plan": True, "can_confirm": True, "can_amend": True,
        "can_execute": True, "can_administer": False,
    }


def test_seeded_roles_and_directory(service):
    assert {r.name for r in service.list_roles()} == {
        "operator", "supervisor", "viewer"}
    directory = {o["id"]: o["role"] for o in service.list_operators()}
    assert directory["j.okafor"] == "operator"
    assert directory["s.hale"] == "supervisor"
    assert directory["v.ward"] == "viewer"
    operator = service.get_role("operator")
    assert "credit_limit" not in operator.fields["customers"]
    supervisor = service.get_role("supervisor")
    assert "credit_limit" in supervisor.fields["customers"]
    viewer = service.get_role("viewer")
    assert not (viewer.can_plan or viewer.can_confirm
                or viewer.can_amend or viewer.can_execute)


# ---------------------------------------------------------- plan time

def test_viewer_cannot_plan(service):
    task = service.intake(CREDIT, "v.ward")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("role_cannot_plan")
    assert "viewer" in task.refusal_reason
    events = [e["event"] for e in service.audit(task.task_id)]
    assert events == ["task_created", "plan_refused"]


def test_unknown_operator_cannot_plan(service):
    task = service.intake(CREDIT, "nobody")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("unknown_operator")


def test_operator_plan_touching_supervisor_only_field_refused(service):
    task = service.intake(CREDIT_LIMIT, "j.okafor")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("field_not_permitted")
    # The refusal names the field and the role.
    assert "credit_limit" in task.refusal_reason
    assert "operator" in task.refusal_reason
    # And nothing was planned, let alone written.
    assert service.store.get_record(
        "customers", "2041")["credit_limit"] == "25000.00"


def test_supervisor_plans_the_credit_limit_change(service):
    task = service.intake(CREDIT_LIMIT, "s.hale")
    assert task.status == TaskStatus.PLANNED
    # An operator may confirm a plan they did not plan.
    task = service.confirm(task.task_id, "j.okafor")
    assert task.status == TaskStatus.CONFIRMED
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.get_record(
        "customers", "2041")["credit_limit"] == "30000"


# --------------------------------------------------------- confirmation

def test_self_confirmation_refused_and_recorded(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.confirm(task.task_id, "j.okafor")
    # A spacing/casing variant of the planner's name is still the
    # planner.
    with pytest.raises(GateError):
        service.confirm(task.task_id, " J.Okafor ")
    after = service.get_task(task.task_id)
    assert after.status == TaskStatus.PLANNED
    assert after.confirmed_by is None
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "confirmation_refused"]
    assert len(refused) == 2
    assert refused[0]["detail"]["code"] == "self_confirmation"


def test_viewer_confirmation_refused_and_recorded(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.confirm(task.task_id, "v.ward")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "confirmation_refused"]
    assert refused[0]["detail"]["code"] == "role_cannot_confirm"
    assert service.get_task(task.task_id).status == TaskStatus.PLANNED


def test_unknown_operator_confirmation_refused(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.confirm(task.task_id, "nobody")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "confirmation_refused"]
    assert refused[0]["detail"]["code"] == "unknown_operator"


def test_supervisor_confirmation_succeeds(service):
    task = service.intake(CREDIT, "j.okafor")
    task = service.confirm(task.task_id, "s.hale")
    assert task.status == TaskStatus.CONFIRMED
    assert task.confirmed_by == "s.hale"


# ------------------------------------------------------------ amendment

def test_amendment_by_viewer_refused_and_recorded(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.amend(task.task_id, "v.ward",
                      [AmendChangeIn(seq=1, fields={"amount": "1.00"})])
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "amendment_refused"]
    assert refused[0]["detail"]["code"] == "role_cannot_amend"
    assert service.get_task(task.task_id).plan_version == 1


def test_amendment_into_a_field_outside_the_amenders_role(service):
    # The supervisor plans a credit-limit change; an operator may
    # amend plans in general, but not into a field their own role
    # holds back.
    task = service.intake(CREDIT_LIMIT, "s.hale")
    with pytest.raises(PlanRefusal) as caught:
        service.amend(task.task_id, "j.okafor",
                      [AmendChangeIn(seq=1, value="31000")])
    assert caught.value.code == "field_not_permitted"
    assert "credit_limit" in caught.value.message
    assert service.get_task(task.task_id).plan_version == 1


# ------------------------------------------------------------- execution

def test_role_narrowed_after_confirmation_blocks_execution(service):
    task = service.intake("mark order 1032 as shipped", "j.okafor")
    service.confirm(task.task_id, "s.hale")
    # The supervisor narrows the operator role: orders.status is
    # no longer the operator's to touch.
    raw = role_raw(service, "operator")
    raw["fields"]["orders"] = ["carrier", "ship_date"]
    service.update_role("operator", raw, "s.hale")
    with pytest.raises(GateError) as caught:
        service.execute(task.task_id, "offline")
    assert "status" in str(caught.value)
    assert "operator" in str(caught.value)
    # The task is held, not executed: still CONFIRMED, no driver,
    # the record untouched.
    held = service.get_task(task.task_id)
    assert held.status == TaskStatus.CONFIRMED
    assert held.driver is None
    assert service.store.get_record(
        "orders", "1032")["status"] == "packed"
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "execution_refused"]
    assert refused[0]["detail"]["code"] == "field_not_permitted"
    assert refused[0]["detail"]["field"] == "status"
    # Widen the role back and the same confirmed plan executes.
    raw["fields"]["orders"] = ["carrier", "ship_date", "status"]
    service.update_role("operator", raw, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED


def test_execution_by_a_role_that_may_not_execute_is_refused(service):
    task = service.intake("mark order 1032 as shipped", "j.okafor")
    service.confirm(task.task_id, "s.hale")
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline", operator="v.ward")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "execution_refused"]
    assert refused[0]["detail"]["code"] == "role_cannot_execute"
    assert service.get_task(task.task_id).status == TaskStatus.CONFIRMED


# ------------------------------------------------------------ role edits

def test_role_edit_requires_an_administering_role(service):
    raw = role_raw(service, "operator")
    raw["pages"] = ["Orders"]
    with pytest.raises(GateError):
        service.update_role("operator", raw, "j.okafor")
    with pytest.raises(GateError):
        service.update_role("operator", raw, "v.ward")
    with pytest.raises(GateError):
        service.update_role("operator", raw, "nobody")


def test_role_edit_that_fails_the_loader_stores_nothing(service):
    before = service.get_role("operator")
    raw = role_raw(service, "operator")
    raw["fields"]["orders"] = ["status", "backdoor"]
    with pytest.raises(RoleError):
        service.update_role("operator", raw, "s.hale")
    assert service.get_role("operator") == before


def test_noop_role_edit_refused(service):
    with pytest.raises(GateError):
        service.update_role("operator", role_raw(service, "operator"),
                            "s.hale")


def test_role_edits_are_audited_with_the_diff(service):
    raw = role_raw(service, "operator")
    raw["fields"]["orders"] = ["carrier", "ship_date"]
    service.update_role("operator", raw, "s.hale")
    audit = service.role_audit()
    assert len(audit) == 1
    entry = audit[0]
    assert entry["event"] == "role_updated"
    assert entry["actor"] == "s.hale"
    assert entry["ts"]  # when
    changes = {c["field"]: c for c in entry["detail"]["changes"]}
    assert changes["fields.orders"]["old"] == [
        "carrier", "ship_date", "status"]
    assert changes["fields.orders"]["new"] == ["carrier", "ship_date"]
    assert entry["detail"]["role"] == "operator"


def test_role_edit_round_trips_on_sqlite(sqlite_service):
    raw = role_raw(sqlite_service, "viewer")
    raw["can_amend"] = True
    sqlite_service.update_role("viewer", raw, "s.hale")
    assert sqlite_service.get_role("viewer").can_amend is True
    assert len(sqlite_service.role_audit()) == 1


# ------------------------------------------------------------------ API

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def test_roles_over_http(client):
    body = client.get("/api/roles").json()
    assert {r["name"] for r in body["roles"]} == {
        "operator", "supervisor", "viewer"}
    directory = {o["id"]: o["role"] for o in body["operators"]}
    assert directory["s.hale"] == "supervisor"
    one = client.get("/api/roles/operator").json()
    assert one["can_plan"] is True
    assert "credit_limit" not in one["fields"]["customers"]
    assert client.get("/api/roles/nope").status_code == 404


def test_role_edit_over_http_and_its_audit(client):
    current = client.get("/api/roles/operator").json()
    body = {k: v for k, v in current.items() if k != "name"}
    body["fields"] = {**body["fields"],
                      "orders": ["carrier", "ship_date"]}
    # An operator may not administer roles.
    denied = client.put("/api/roles/operator",
                        json={**body, "operator": "j.okafor"})
    assert denied.status_code == 409
    # An invalid definition is refused whole.
    bad = {**body, "fields": {"orders": ["status", "backdoor"]}}
    invalid = client.put("/api/roles/operator",
                         json={**bad, "operator": "s.hale"})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "role_invalid"
    # The supervisor's edit lands, and the audit shows it.
    done = client.put("/api/roles/operator",
                      json={**body, "operator": "s.hale"})
    assert done.status_code == 200
    assert done.json()["fields"]["orders"] == ["carrier", "ship_date"]
    audit = client.get("/api/roles-audit").json()
    assert len(audit) == 1
    assert audit[0]["event"] == "role_updated"
    assert audit[0]["actor"] == "s.hale"


def test_self_confirmation_over_http_is_409_and_recorded(client):
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    own = client.post(f"/api/tasks/{tid}/confirm",
                      json={"operator": "j.okafor"})
    assert own.status_code == 409
    confirmed = client.post(f"/api/tasks/{tid}/confirm",
                            json={"operator": "s.hale"})
    assert confirmed.status_code == 200
    events = [e["event"] for e in client.get(
        f"/api/tasks/{tid}/audit").json()]
    assert events == ["task_created", "plan_created",
                      "confirmation_refused", "plan_confirmed"]


def test_execute_as_viewer_over_http_is_409(client):
    created = client.post("/api/tasks", json={
        "text": "mark order 1032 as shipped", "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    client.post(f"/api/tasks/{tid}/confirm",
                json={"operator": "s.hale"})
    blocked = client.post(f"/api/tasks/{tid}/execute", json={
        "driver": "offline", "operator": "v.ward"})
    assert blocked.status_code == 409
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["task"]["status"] == "CONFIRMED"
    done = client.post(f"/api/tasks/{tid}/execute",
                       json={"driver": "offline"})
    assert done.json()["status"] == "VERIFIED"
