"""Round 7 — the task library: templates as governed records
(seeded through the strict loader, published in new versions
only as recorded supervisor acts, old versions instantiable by
name and version), instantiation that validates its slots and
then goes through the ordinary intake — the planner derives the
plan from the rendered text, so a template smuggles nothing past
it — and health computed on read from the pinned examples."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.models import TaskStatus
from page_agent_copilot.service import CopilotService, GateError
from page_agent_copilot.store import InMemoryStore
from page_agent_copilot.templates import (
    DEFAULT_TEMPLATES,
    TemplateError,
    load_template,
)

SEEDED_NAMES = ["credit-memo", "credit-memo-and-settle",
                "customer-update", "invoice-status", "order-status",
                "ship-order"]

ORDER_STATUS_V2 = {
    "name": "order-status",
    "pattern": "mark order {order} as {status}",
    "description": "A single order status change (library wording, v2).",
    "slots": [
        {"name": "order", "kind": "record", "record_kind": "orders"},
        {"name": "status", "kind": "text"},
    ],
    "examples": [{
        "slots": {"order": "1036", "status": "packed"},
        "task_type": "order_status",
        "steps": [{"action": "set", "record_kind": "orders",
                   "record_id": "1036", "field": "status",
                   "value": "packed"}],
    }],
}


def fresh(settings) -> CopilotService:
    svc = CopilotService(settings, InMemoryStore())
    svc.seed()
    return svc


def example_slots(service, name):
    template = service.get_template(name)
    return dict(template.examples[0].slots)


def run_to_verified(service, task):
    service.confirm(task.task_id, "s.hale")
    return service.execute(task.task_id, "offline")


# ------------------------------------------------------------- loader

def test_the_seeded_library_passes_the_strict_loader():
    for raw in DEFAULT_TEMPLATES:
        template = load_template(raw)
        assert template.version == 1 and template.examples


def test_loader_refuses_a_placeholder_no_slot_declares():
    raw = {**ORDER_STATUS_V2,
           "pattern": "mark order {order} as {status} for {reason}"}
    with pytest.raises(TemplateError) as err:
        load_template(raw)
    assert err.value.code == "template_invalid"
    assert "reason" in err.value.message


def test_loader_refuses_a_declared_slot_the_pattern_never_uses():
    raw = {**ORDER_STATUS_V2, "slots": ORDER_STATUS_V2["slots"] + [
        {"name": "carrier", "kind": "text"}]}
    with pytest.raises(TemplateError) as err:
        load_template(raw)
    assert "carrier" in err.value.message


def test_loader_refuses_bad_slot_shapes():
    bad_kind = {**ORDER_STATUS_V2, "slots": [
        {"name": "order", "kind": "record", "record_kind": "memo"},
        {"name": "status", "kind": "text"}]}
    with pytest.raises(TemplateError):
        load_template(bad_kind)
    kind_on_text = {**ORDER_STATUS_V2, "slots": [
        {"name": "order", "kind": "record", "record_kind": "orders"},
        {"name": "status", "kind": "text", "record_kind": "orders"}]}
    with pytest.raises(TemplateError):
        load_template(kind_on_text)


def test_loader_refuses_an_example_that_misfills_the_slots():
    raw = {**ORDER_STATUS_V2, "examples": [{
        "slots": {"order": "1036"},
        "task_type": "order_status",
        "steps": [{"action": "set", "record_kind": "orders",
                   "record_id": "1036", "field": "status",
                   "value": "packed"}]}]}
    with pytest.raises(TemplateError) as err:
        load_template(raw)
    assert "example 1" in err.value.message


def test_loader_refuses_no_description_and_no_examples():
    with pytest.raises(TemplateError):
        load_template({**ORDER_STATUS_V2, "description": ""})
    with pytest.raises(TemplateError):
        load_template({**ORDER_STATUS_V2, "examples": []})
    with pytest.raises(TemplateError):
        load_template({**ORDER_STATUS_V2, "name": "Order Status"})


# ------------------------------------------------------------- seeding

def test_the_library_is_seeded_through_the_loader(service):
    templates = service.list_templates()
    assert [t.name for t in templates] == SEEDED_NAMES
    assert all(t.version == 1 and not t.superseded
               for t in templates)
    assert service.get_template("order-status").pattern == \
        "mark order {order} as {status}"
    assert service.get_template("no-such") is None
    assert service.get_template("order-status", 9) is None


def test_templates_survive_a_store_round_trip(sqlite_service):
    sqlite_service.publish_template("s.hale", ORDER_STATUS_V2)
    again = CopilotService(sqlite_service.settings,
                           sqlite_service.store)
    versions = [t for t in again.list_templates()
                if t.name == "order-status"]
    assert [t.version for t in versions] == [1, 2]
    assert versions[0].superseded and not versions[1].superseded
    task = again.instantiate_template(
        "order-status", 1, {"order": "1036", "status": "packed"},
        "j.okafor")
    assert task.template_version == 1 and task.status == \
        TaskStatus.PLANNED


# ------------------------------------------------------------ publishing

def test_publishing_is_a_recorded_supervisor_act(service):
    published = service.publish_template("s.hale", ORDER_STATUS_V2)
    assert published.version == 2
    assert published.published_by == "s.hale"
    current = service.get_template("order-status")
    assert current.version == 2
    old = service.get_template("order-status", 1)
    assert old.superseded and not current.superseded
    audit = [e for e in service.template_audit()
             if e["event"] == "template_published"]
    assert len(audit) == 1
    assert audit[0]["actor"] == "s.hale"
    assert audit[0]["detail"] == {"template": "order-status",
                                  "version": 2, "supersedes": 1}


def test_an_operator_may_not_publish_and_the_refusal_is_recorded(
        service):
    with pytest.raises(GateError):
        service.publish_template("j.okafor", ORDER_STATUS_V2)
    with pytest.raises(GateError):
        service.publish_template("v.ward", ORDER_STATUS_V2)
    with pytest.raises(GateError):
        service.publish_template("mallory", ORDER_STATUS_V2)
    refusals = [e for e in service.template_audit()
                if e["event"] == "template_refused"]
    assert [e["detail"]["code"] for e in refusals] == [
        "role_cannot_administer", "role_cannot_administer",
        "unknown_operator"]
    assert service.get_template("order-status").version == 1


def test_a_deactivated_supervisor_may_not_publish(service):
    service.enroll_operator("s.hale", "r.okafor", "supervisor")
    service.deactivate_operator("r.okafor", "s.hale")
    with pytest.raises(GateError):
        service.publish_template("s.hale", ORDER_STATUS_V2)
    refusal = [e for e in service.template_audit()
               if e["event"] == "template_refused"]
    assert refusal and refusal[0]["detail"]["code"] == \
        "operator_inactive"


def test_publishing_identical_content_is_refused(service):
    current = service.get_template("order-status")
    same = {
        "name": "order-status", "pattern": current.pattern,
        "description": current.description,
        "slots": [s.model_dump(mode="json") for s in current.slots],
        "examples": [e.model_dump(mode="json")
                     for e in current.examples],
    }
    with pytest.raises(GateError):
        service.publish_template("s.hale", same)
    refusal = [e for e in service.template_audit()
               if e["event"] == "template_refused"]
    assert refusal[0]["detail"]["code"] == "no_change"
    assert service.get_template("order-status").version == 1


def test_publishing_invalid_content_stores_nothing(service):
    bad = {**ORDER_STATUS_V2, "pattern": "mark order {order} as {mood}"}
    with pytest.raises(TemplateError) as err:
        service.publish_template("s.hale", bad)
    assert err.value.code == "template_invalid"
    assert service.get_template("order-status").version == 1


def test_a_new_name_publishes_as_version_one(service):
    raw = {**ORDER_STATUS_V2, "name": "order-status-copy"}
    published = service.publish_template("s.hale", raw)
    assert published.version == 1
    audit = [e for e in service.template_audit()
             if e["event"] == "template_published"]
    assert audit[0]["detail"]["supersedes"] is None


# ---------------------------------------------------------- instantiation

def test_every_seeded_template_instantiates_and_verifies(settings):
    # (name, record kind, record id, fields that must stand after)
    checks = {
        "ship-order": ("orders", "1032",
                       {"status": "shipped",
                        "ship_date": "2026-10-20", "carrier": "DHL"}),
        "order-status": ("orders", "1036", {"status": "packed"}),
        "invoice-status": ("invoices", "4474", {"status": "paid"}),
        "customer-update": ("customers", "2041",
                            {"phone": "555-0134"}),
    }
    for name in SEEDED_NAMES:
        svc = fresh(settings)
        task = svc.instantiate_template(
            name, None, example_slots(svc, name), "j.okafor")
        assert task.status == TaskStatus.PLANNED, name
        assert task.template_name == name
        assert task.template_version == 1
        task = run_to_verified(svc, task)
        assert task.status == TaskStatus.VERIFIED, name
        if name in checks:
            kind, record_id, fields = checks[name]
            record = svc.store.get_record(kind, record_id)
            for field, value in fields.items():
                assert record[field] == value, (name, field)
        if name.startswith("credit-memo"):
            memos = svc.store.list_records("credit_memos")
            assert len(memos) == 1
            assert memos[0]["invoice_id"] == "4471"
            assert memos[0]["amount"] == "120.00"
            assert memos[0]["reason"] == "damaged goods"
            assert memos[0]["status"] == "issued"
        if name == "credit-memo-and-settle":
            invoice = svc.store.get_record("invoices", "4471")
            assert invoice["status"] == "paid"
        if name == "customer-update":
            cust = svc.store.get_record("customers", "2041")
            assert cust["bank_account"].startswith("GB29")


def test_instantiation_is_written_to_the_tasks_audit(service):
    task = service.instantiate_template(
        "order-status", None, {"order": "1036", "status": "packed"},
        "j.okafor")
    entries = [e for e in service.audit(task.task_id)
               if e["event"] == "template_instantiated"]
    assert len(entries) == 1
    assert entries[0]["actor"] == "j.okafor"
    assert entries[0]["detail"]["template"] == "order-status"
    assert entries[0]["detail"]["version"] == 1
    assert entries[0]["detail"]["requested_version"] is None
    assert task.text == "mark order 1036 as packed"


def test_an_old_version_instantiates_by_name_and_version(service):
    service.publish_template("s.hale", ORDER_STATUS_V2)
    slots = {"order": "1036", "status": "packed"}
    old = service.instantiate_template("order-status", 1, slots,
                                       "j.okafor")
    assert old.template_version == 1
    new = service.instantiate_template("order-status", None, slots,
                                       "j.okafor")
    assert new.template_version == 2
    entry = [e for e in service.audit(old.task_id)
             if e["event"] == "template_instantiated"][0]
    assert entry["detail"]["requested_version"] == 1


def test_a_slot_naming_a_missing_record_refuses_at_instantiation(
        service):
    with pytest.raises(TemplateError) as err:
        service.instantiate_template(
            "credit-memo", None,
            {"invoice": "9999", "reason": "damaged goods",
             "amount": "120"}, "j.okafor")
    assert err.value.code == "record_not_found"
    assert "invoice 9999" in err.value.message
    assert service.list_tasks() == []
    refusal = [e for e in service.template_audit()
               if e["event"] == "template_instantiation_refused"]
    assert refusal[0]["detail"]["code"] == "record_not_found"
    assert refusal[0]["detail"]["template"] == "credit-memo"


def test_slot_shape_refusals(service):
    good = {"invoice": "4471", "reason": "damaged goods",
            "amount": "120"}
    with pytest.raises(TemplateError) as err:
        service.instantiate_template(
            "credit-memo", None,
            {"invoice": "4471", "reason": "damaged goods"},
            "j.okafor")
    assert err.value.code == "slot_missing"
    with pytest.raises(TemplateError) as err:
        service.instantiate_template(
            "credit-memo", None, {**good, "extra": "x"}, "j.okafor")
    assert err.value.code == "slot_not_declared"
    with pytest.raises(TemplateError) as err:
        service.instantiate_template(
            "credit-memo", None, {**good, "amount": "abc"},
            "j.okafor")
    assert err.value.code == "slot_not_money"
    with pytest.raises(TemplateError) as err:
        service.instantiate_template(
            "ship-order", None,
            {"order": "1032", "ship_date": "20 Oct 2026",
             "carrier": "DHL"}, "j.okafor")
    assert err.value.code == "slot_not_a_date"
    with pytest.raises(TemplateError) as err:
        service.instantiate_template("no-such", None, {}, "j.okafor")
    assert err.value.code == "template_not_found"
    with pytest.raises(TemplateError) as err:
        service.instantiate_template(
            "order-status", 9, {"order": "1036", "status": "packed"},
            "j.okafor")
    assert err.value.code == "template_not_found"
    assert service.list_tasks() == []


def test_a_policy_breaking_slot_value_fails_with_policys_reason(
        service):
    task = service.instantiate_template(
        "credit-memo", None,
        {"invoice": "4471", "reason": "goodwill", "amount": "900"},
        "j.okafor")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("credit_above_cap")
    assert task.template_name == "credit-memo"
    assert task.template_version == 1


def test_an_ungoverned_slot_value_fails_with_the_planners_reason(
        service):
    task = service.instantiate_template(
        "order-status", None, {"order": "1036", "status": "flying"},
        "j.okafor")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("value_not_governed")
    task = service.instantiate_template(
        "customer-update", None,
        {"customer": "2041", "field": "nickname", "value": "Harby"},
        "j.okafor")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("no_plan_derivable")


def test_instantiation_carries_no_role_bypass(service):
    slots = {"order": "1036", "status": "packed"}
    task = service.instantiate_template("order-status", None, slots,
                                        "v.ward")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("role_cannot_plan")
    service.enroll_operator("s.hale", "t.reyes", "operator")
    service.deactivate_operator("s.hale", "t.reyes")
    task = service.instantiate_template("order-status", None, slots,
                                        "t.reyes")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("operator_inactive")


# ---------------------------------------------------------------- health

def test_every_seeded_version_is_healthy(service):
    for template in service.list_templates():
        health = service.template_health(template)
        assert health["ok"], (template.name, health)
        assert all(e["ok"] for e in health["examples"])


def test_a_version_whose_examples_no_longer_match_is_broken(service):
    broken = {**ORDER_STATUS_V2, "examples": [{
        "slots": {"order": "1036", "status": "packed"},
        "task_type": "order_status",
        "steps": [{"action": "set", "record_kind": "orders",
                   "record_id": "1036", "field": "status",
                   "value": "shipped"}]}]}
    service.publish_template("s.hale", broken)
    health = service.template_health(service.get_template(
        "order-status"))
    assert not health["ok"]
    assert "packed" in health["examples"][0]["detail"]
    view = service.template_view(service.get_template(
        "order-status"))
    assert view["health"]["ok"] is False and view["current"] is True
    assert service.template_health(
        service.get_template("order-status", 1))["ok"]


def test_health_names_a_planner_refusal(service):
    refusing = {**ORDER_STATUS_V2, "name": "order-status-refusing",
                "examples": [{
                    "slots": {"order": "1036", "status": "flying"},
                    "task_type": "order_status",
                    "steps": [{"action": "set",
                               "record_kind": "orders",
                               "record_id": "1036", "field": "status",
                               "value": "flying"}]}]}
    service.publish_template("s.hale", refusing)
    health = service.template_health(
        service.get_template("order-status-refusing"))
    assert not health["ok"]
    assert "value_not_governed" in health["examples"][0]["detail"]


def test_health_is_derived_against_the_seed_not_the_live_store(
        service):
    # A console edit to the live records moves the live store, not
    # the basis the examples pin — health reads the seed.
    invoice = service.store.get_record("invoices", "4471")
    invoice["status"] = "paid"
    service.store.upsert_record("invoices", invoice)
    assert service.template_health(
        service.get_template("credit-memo"))["ok"]


# ------------------------------------------------------------------- API

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def test_library_on_the_api(client):
    listing = client.get("/api/templates")
    assert listing.status_code == 200
    rows = listing.json()
    assert [r["name"] for r in rows] == SEEDED_NAMES
    assert all(r["health"]["ok"] for r in rows)
    one = client.get("/api/templates/order-status")
    assert one.status_code == 200 and one.json()["version"] == 1
    by_version = client.get("/api/templates/order-status/versions/1")
    assert by_version.status_code == 200
    assert client.get("/api/templates/nope").status_code == 404
    assert client.get(
        "/api/templates/order-status/versions/9").status_code == 404


def test_publish_and_instantiate_on_the_api(client):
    refused = client.post("/api/templates", json={
        "operator": "j.okafor", **ORDER_STATUS_V2})
    assert refused.status_code == 409
    invalid = client.post("/api/templates", json={
        "operator": "s.hale", **ORDER_STATUS_V2,
        "pattern": "mark order {order} as {mood}"})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "template_invalid"
    published = client.post("/api/templates", json={
        "operator": "s.hale", **ORDER_STATUS_V2})
    assert published.status_code == 201
    assert published.json()["version"] == 2
    audit = client.get("/api/templates-audit").json()
    assert any(e["event"] == "template_published" for e in audit)

    missing = client.post("/api/templates/credit-memo/instantiate",
                          json={"operator": "j.okafor",
                                "slots": {"invoice": "9999",
                                          "reason": "damaged goods",
                                          "amount": "120"}})
    assert missing.status_code == 422
    assert missing.json()["code"] == "record_not_found"

    created = client.post(
        "/api/templates/order-status/instantiate",
        json={"operator": "j.okafor", "version": 1,
              "slots": {"order": "1036", "status": "packed"}})
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["template_name"] == "order-status"
    assert task["template_version"] == 1
    assert task["status"] == "PLANNED"
    client.post(f"/api/tasks/{task['task_id']}/confirm",
                json={"operator": "s.hale"})
    done = client.post(f"/api/tasks/{task['task_id']}/execute",
                       json={"driver": "offline",
                             "operator": "j.okafor"})
    assert done.json()["status"] == "VERIFIED"
