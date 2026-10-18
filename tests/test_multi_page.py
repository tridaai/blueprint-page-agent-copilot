"""Round 12 — multi-page flows: pages as governed data, the plan's
page span, navigation between pages inside an execution, the
cross-page grammar shape (a credit memo that also updates the
invoice's customer), and page enforcement at execution time.

The console is one page with screens; what this round adds is the
page as a concept the shell governs: the catalogue a role's
allowlist references, the span a plan declares by its steps, and
the navigation an execution must show when it crosses from one
page to another."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.models import TaskStatus
from page_agent_copilot.planner import PAGE_FOR_KIND
from page_agent_copilot.roles import (
    KNOWN_PAGES,
    PAGE_CATALOGUE,
    page_for_kind,
    page_title,
)
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore

CREDIT_AND_CUSTOMER = (
    "raise a credit memo for invoice 4471, reason damaged goods, "
    "$120 and update the customer's email to "
    "creditnotes@harbourview.example")


def plan_and_confirm(service, text=CREDIT_AND_CUSTOMER,
                     planner="op", confirmer="s.hale"):
    task = service.intake(text, planner)
    assert task.status == TaskStatus.PLANNED, task.refusal_reason
    service.confirm(task.task_id, confirmer)
    return service.get_task(task.task_id)


# ------------------------------------------------------- the catalogue

def test_the_page_catalogue_is_data_and_agrees_with_the_planner():
    assert [p["id"] for p in PAGE_CATALOGUE] == [
        "Orders", "Customers", "Invoices"]
    assert KNOWN_PAGES == frozenset(p["id"] for p in PAGE_CATALOGUE)
    for page in PAGE_CATALOGUE:
        assert page["title"] and page["kinds"]
    # Every record kind the planner places sits on exactly the
    # page the catalogue says hosts it, and the catalogue hosts
    # no kind the planner cannot place.
    for kind, page_id in PAGE_FOR_KIND.items():
        assert page_for_kind(kind) == page_id
    catalogued = {kind for page in PAGE_CATALOGUE
                  for kind in page["kinds"]}
    assert catalogued == set(PAGE_FOR_KIND)
    assert page_title("Invoices") == "Invoices"


def test_pages_are_served_as_data():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    client = TestClient(app)
    body = client.get("/api/pages").json()
    assert [p["id"] for p in body["pages"]] == [
        "Orders", "Customers", "Invoices"]
    invoices = next(p for p in body["pages"] if p["id"] == "Invoices")
    assert invoices["kinds"] == ["invoices", "credit_memos"]


# --------------------------------------------------------- the plan span

def test_a_cross_page_plan_says_so(service):
    task = service.intake(CREDIT_AND_CUSTOMER, "op")
    plan = service.get_plan(task.task_id)
    assert plan.pages == ["Invoices", "Customers"]
    assert plan.is_multi_page is True


def test_a_single_page_plan_is_not_a_multi_page_flow(service):
    task = service.intake("mark order 1032 as shipped", "op")
    plan = service.get_plan(task.task_id)
    assert plan.pages == ["Orders"]
    assert plan.is_multi_page is False


def test_the_api_serves_the_plan_span():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    client = TestClient(app)
    body = client.post("/api/tasks", json={
        "text": CREDIT_AND_CUSTOMER, "operator": "op"}).json()
    assert body["plan"]["pages"] == ["Invoices", "Customers"]
    assert body["plan"]["multi_page"] is True
    detail = client.get(f"/api/tasks/{body['task']['task_id']}").json()
    assert detail["plan"]["pages"] == ["Invoices", "Customers"]
    assert detail["plan"]["multi_page"] is True


# --------------------------------------------------- the cross-page shape

def test_the_cross_page_shape_plans_two_pages_two_kinds(service):
    task = service.intake(CREDIT_AND_CUSTOMER, "op")
    assert task.status == TaskStatus.PLANNED, task.refusal_reason
    plan = service.get_plan(task.task_id)
    assert plan.task_type == "credit_memo_and_customer_update"
    create, update = plan.steps
    assert create.action.value == "create"
    assert create.page == "Invoices"
    assert create.fields == {
        "invoice_id": "4471", "amount": "120.00",
        "reason": "damaged goods", "status": "issued"}
    # The customer step names the invoice's customer — from the
    # record (invoice 4471 belongs to customer 2041), never from
    # the task text, which names no customer at all.
    assert update.action.value == "set"
    assert update.page == "Customers"
    assert update.record_kind == "customers"
    assert update.record_id == "2041"
    assert update.field == "email"
    assert update.value == "creditnotes@harbourview.example"


def test_the_cross_page_shape_verifies_end_to_end(service):
    task = plan_and_confirm(service)
    before = service.store.get_record("customers", "2041")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    memos = service.store.list_records("credit_memos")
    assert len(memos) == 1 and memos[0]["invoice_id"] == "4471"
    after = service.store.get_record("customers", "2041")
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"email"}
    assert after["email"] == "creditnotes@harbourview.example"
    report = service.get_verification(task.task_id)
    assert report.ok
    links = [c for c in report.linkage_checks
             if c.check == "invoice_customer_link"]
    assert len(links) == 1 and links[0].verdict == "match"
    assert links[0].expected == "2041" and links[0].actual == "2041"


def test_the_cross_page_shape_combines_with_the_settle_tail(service):
    text = ("raise a credit memo for invoice 4471, reason damaged "
            "goods, $120 and update the customer's email to "
            "creditnotes@harbourview.example and mark invoice "
            "4471 as paid")
    task = service.intake(text, "op")
    assert task.status == TaskStatus.PLANNED, task.refusal_reason
    plan = service.get_plan(task.task_id)
    assert plan.task_type == ("credit_memo_and_customer_update"
                              "_and_invoice_status")
    assert [(s.record_kind, s.page) for s in plan.steps] == [
        ("credit_memos", "Invoices"), ("customers", "Customers"),
        ("invoices", "Invoices")]
    # The customer value stops at the settle clause — it is not
    # swallowed into the email.
    assert plan.steps[1].value == "creditnotes@harbourview.example"
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.get_record(
        "invoices", "4471")["status"] == "paid"


def test_a_named_customer_in_a_credit_task_is_refused(service):
    # The shape never names a customer: asking for a different one
    # is refused whole, not quietly redirected and not dropped.
    task = service.intake(
        "raise a credit memo for invoice 4471, reason damaged "
        "goods, $120 and update customer 2042 email to "
        "x@harbourview.example", "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("no_plan_derivable")
    assert service.store.list_records("credit_memos") == []


def test_the_cross_page_shape_refuses_a_payment_field(service):
    task = service.intake(
        "raise a credit memo for invoice 4471, reason damaged "
        "goods, $120 and update the customer's bank account to "
        "GB00XXXX", "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("payment_field")
