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
                            json={"operator": "j.okafor"})
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["confirmed_by"] == "j.okafor"

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
    # The library's own field names (page-agent@1.12.4 LLMConfig).
    assert set(cfg) == {"model", "baseURL", "apiKey", "language",
                        "maxSteps", "configured"}


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
