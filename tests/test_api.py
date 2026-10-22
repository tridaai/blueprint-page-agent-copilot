"""The HTTP surface: lifecycle over the wire, 409s at the gate,
payment fields refused on console saves, bridge config shape."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.store import InMemoryStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")


@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def test_health_and_readiness(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/readiness")
    assert ready.status_code == 200
    body = ready.json()
    assert body["records"]["customers"] == 6
    assert body["default_driver"] == "offline"


def test_full_lifecycle_over_http(client):
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "j.okafor"})
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["status"] == "PLANNED"
    plan = created.json()["plan"]
    assert plan["steps"][0]["fields"]["amount"] == "120.00"
    tid = task["task_id"]

    blocked = client.post(f"/api/tasks/{tid}/execute",
                          json={"driver": "offline"})
    assert blocked.status_code == 409  # unconfirmed cannot execute

    confirmed = client.post(f"/api/tasks/{tid}/confirm",
                            json={"operator": "s.hale"})
    assert confirmed.json()["status"] == "CONFIRMED"
    # The confirmer is the supervisor, not the planner: confirmation
    # is a second person's act (self-confirmation is refused).
    assert confirmed.json()["confirmed_by"] == "s.hale"

    done = client.post(f"/api/tasks/{tid}/execute",
                       json={"driver": "offline"})
    assert done.json()["status"] == "VERIFIED"

    audit = client.get(f"/api/tasks/{tid}/audit").json()
    assert [e["event"] for e in audit][-1] == "verification_passed"
    memos = client.get("/api/console/credit-memos").json()
    assert len(memos) == 1 and memos[0]["amount"] == "120.00"


def test_refusal_over_http(client):
    created = client.post("/api/tasks", json={
        "text": "raise a credit memo for invoice 4471, reason "
                "goodwill, $900", "operator": "op"})
    task = created.json()["task"]
    assert task["status"] == "REFUSED"
    assert "credit_above_cap" in task["refusal_reason"]
    blocked = client.post(f"/api/tasks/{task['task_id']}/confirm",
                          json={"operator": "op"})
    assert blocked.status_code == 409


def test_agent_config_shape(client):
    cfg = client.get("/api/agent-config").json()
    # The library's own field names (page-agent@1.12.4 LLMConfig),
    # plus the deployment profile the read carries since round 16
    # (profile, delivery, endpointClass, bundleURL, csp) — added
    # fields only; the library fields are untouched.
    assert set(cfg) == {"model", "baseURL", "apiKey", "language",
                        "maxSteps", "configured",
                        "profile", "delivery", "endpointClass",
                        "bundleURL", "csp"}


def test_console_save_refuses_payment_field(client):
    res = client.put("/api/console/customers/2041", json={
        "fields": {"bank_account": "GB00 HACK 0000"}})
    assert res.status_code == 422
    customer = client.get("/api/console/customers/2041").json()
    assert customer["bank_account"].startswith("GB29")


def test_console_form_round_trip(client):
    res = client.put("/api/console/orders/1032", json={
        "fields": {"status": "shipped", "ship_date": "2026-10-18",
                   "carrier": "DHL"}})
    assert res.json()["status"] == "shipped"


def test_console_page_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "OrderDesk" in page.text
    assert "page-agent.bundle.js" in page.text


def test_console_credit_memo_amount_is_a_money_field(client):
    # The console form's amount is validated and stored to the cent:
    # text that is not a positive amount never reaches the records,
    # and "120" lands as the "120.00" a plan states.
    for bad in ("abc", "0", "-5", "", "NaN"):
        refused = client.post("/api/console/credit-memos", json={
            "fields": {"invoice_id": "4471", "reason": "damaged goods",
                       "amount": bad}})
        assert refused.status_code == 422, bad
    no_reason = client.post("/api/console/credit-memos", json={
        "fields": {"invoice_id": "4471", "reason": " ", "amount": "10"}})
    assert no_reason.status_code == 422
    assert client.get("/api/console/credit-memos").json() == []
    issued = client.post("/api/console/credit-memos", json={
        "fields": {"invoice_id": "4471", "reason": "damaged goods",
                   "amount": "$120"}})
    assert issued.status_code == 200
    assert issued.json()["amount"] == "120.00"


def test_live_execute_is_attributed_to_the_executor(client):
    # The live bridge names the operator on the console as the
    # executor: the role gate applies to that name, and the
    # execution_started act is theirs — not the planner's.
    tid = client.post("/api/tasks", json={
        "text": "mark order 1036 as packed",
        "operator": "j.okafor"}).json()["task"]["task_id"]
    client.post(f"/api/tasks/{tid}/confirm", json={"operator": "s.hale"})
    viewer = client.post(f"/api/tasks/{tid}/execute", json={
        "driver": "page-agent-live", "operator": "v.ward"})
    assert viewer.status_code == 409
    armed = client.post(f"/api/tasks/{tid}/execute", json={
        "driver": "page-agent-live", "operator": "s.hale"})
    assert armed.status_code == 200
    started = [e for e in client.get(f"/api/tasks/{tid}/audit").json()
               if e["event"] == "execution_started"]
    assert [e["actor"] for e in started] == ["s.hale"]


def test_console_save_edits_only_the_forms_fields(client):
    # The save endpoint is where form submissions land — a human's
    # or the in-page agent's. A field the form does not edit (the
    # record's id, its customer, its total) and a status outside the
    # governed set are refused, and nothing is written.
    before = client.get("/api/console/orders").json()
    for fields in ({"id": "9999"}, {"customer_id": "0000"},
                   {"total": "abc"}, {"status": "flying"},
                   {"status": "packed", "id": "9999"}):
        res = client.put("/api/console/orders/1033",
                         json={"fields": fields})
        assert res.status_code == 422, fields
    assert client.put("/api/console/invoices/4471", json={
        "fields": {"status": "lost"}}).status_code == 422
    assert client.get("/api/console/orders").json() == before


def test_console_credit_memo_task_id_must_name_a_task(client):
    res = client.post("/api/console/credit-memos", json={
        "fields": {"invoice_id": "4471", "reason": "damaged goods",
                   "amount": "10"},
        "task_id": "TASK-GHOST"})
    assert res.status_code == 422
    assert client.get("/api/console/credit-memos").json() == []
