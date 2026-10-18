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

def test_a_single_page_plan_is_not_a_multi_page_flow(service):
    task = service.intake("mark order 1032 as shipped", "op")
    plan = service.get_plan(task.task_id)
    assert plan.pages == ["Orders"]
    assert plan.is_multi_page is False
