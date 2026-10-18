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
from page_agent_copilot.models import (
    NavigationReportIn,
    StepReportIn,
    TaskStatus,
)
from page_agent_copilot.records import (
    apply_field_change,
    create_credit_memo,
)
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


def test_the_seeded_template_instantiates_the_cross_page_shape(service):
    task = service.instantiate_template(
        "credit-memo-and-customer-update", None,
        {"invoice": "4472", "reason": "short shipment",
         "amount": "75", "field": "phone", "value": "555-0175"},
        "op")
    assert task.status == TaskStatus.PLANNED, task.refusal_reason
    plan = service.get_plan(task.task_id)
    assert plan.task_type == "credit_memo_and_customer_update"
    assert plan.pages == ["Invoices", "Customers"]
    # Invoice 4472's customer is 2042 — the step names them.
    assert plan.steps[1].record_id == "2042"
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.get_record(
        "customers", "2042")["phone"] == "555-0175"


# ------------------------------------------- page enforcement, both doors

def drop_page(service, role_name, page):
    raw = service.get_role(role_name).model_dump(mode="json")
    raw["pages"] = [p for p in raw["pages"] if p != page]
    service.update_role(role_name, raw, "s.hale")


def test_a_role_missing_a_page_cannot_plan_the_shape(service):
    drop_page(service, "operator", "Customers")
    task = service.intake(CREDIT_AND_CUSTOMER, "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("page_not_permitted")
    assert "Customers" in task.refusal_reason


def test_a_role_missing_the_first_page_cannot_plan_it_either(service):
    drop_page(service, "operator", "Invoices")
    task = service.intake(CREDIT_AND_CUSTOMER, "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("page_not_permitted")
    assert "Invoices" in task.refusal_reason


def test_a_page_lost_after_confirmation_blocks_execution(service):
    # The R3 execution re-check, pinned for pages (round 12): the
    # plan is confirmed while the role holds both pages; the role
    # then loses Customers, and execution refuses — naming the
    # page — with the task held at CONFIRMED.
    task = plan_and_confirm(service)
    drop_page(service, "operator", "Customers")
    with pytest.raises(GateError) as caught:
        service.execute(task.task_id, "offline")
    assert "Customers" in str(caught.value)
    held = service.get_task(task.task_id)
    assert held.status == TaskStatus.CONFIRMED
    assert service.store.list_records("credit_memos") == []
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "execution_refused"]
    assert refused[-1]["detail"]["code"] == "page_not_permitted"
    assert refused[-1]["detail"]["page"] == "Customers"
    # The page restored, the same confirmation executes.
    raw = service.get_role("operator").model_dump(mode="json")
    raw["pages"] = ["Orders", "Customers", "Invoices"]
    service.update_role("operator", raw, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED


def test_the_executor_side_of_the_page_recheck(service):
    # The planner is the supervisor (all pages); the executor is
    # an operator whose role loses Customers after confirmation.
    # The planner's side passes; the executor's side refuses,
    # naming the page and the executor.
    task = plan_and_confirm(service, planner="s.hale",
                            confirmer="op")
    drop_page(service, "operator", "Customers")
    with pytest.raises(GateError) as caught:
        service.execute(task.task_id, "offline", operator="op")
    assert "Customers" in str(caught.value)
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "execution_refused"]
    assert refused[-1]["detail"]["code"] == "page_not_permitted"
    assert refused[-1]["detail"]["who"] == "executor"


# ------------------------------------------------------------ navigation

def test_offline_execution_records_the_navigation(service):
    task = plan_and_confirm(service)
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    attempt = service.get_attempts(task.task_id)[0]
    assert len(attempt.navigations) == 1
    nav = attempt.navigations[0]
    assert (nav.seq, nav.from_page, nav.to_page, nav.driver) == (
        2, "Invoices", "Customers", "offline")
    events = [e for e in service.audit(task.task_id)
              if e["event"] == "page_navigated"]
    assert len(events) == 1
    assert events[0]["detail"]["from_page"] == "Invoices"
    assert events[0]["detail"]["to_page"] == "Customers"
    # A navigation is not a step: the budget's step count is the
    # plan's steps, not steps plus transitions.
    assert len(attempt.steps) == 2


def test_a_single_page_execution_records_no_navigation(service):
    task = plan_and_confirm(
        service, "mark order 1032 as shipped, ship date "
                 "2026-10-20, carrier DHL")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    attempt = service.get_attempts(task.task_id)[0]
    assert attempt.navigations == []


def test_offline_resume_records_the_crossing_it_still_owes(service):
    task = plan_and_confirm(service)
    task = service.execute(task.task_id, "offline",
                           fail={2: "the customer form was not "
                                    "on screen"})
    assert task.status == TaskStatus.EXECUTION_FAILED
    first = service.get_attempts(task.task_id)[0]
    assert [n.to_page for n in first.navigations] == ["Customers"]
    task = service.resume_execution(task.task_id)
    assert task.status == TaskStatus.VERIFIED
    attempts = service.get_attempts(task.task_id)
    assert len(attempts) == 2
    # The resumed attempt stands where the plan left it — on the
    # Invoices page — and records its own crossing to Customers.
    assert [(n.from_page, n.to_page)
            for n in attempts[1].navigations] == [
        ("Invoices", "Customers")]


def live_cross_page(service):
    """A confirmed cross-page task, armed live, with step 1's
    work done in the store the way the bridge would land it."""
    task = plan_and_confirm(service)
    service.execute(task.task_id, "page-agent-live")
    create_credit_memo(
        service.store,
        {"invoice_id": "4471", "amount": "120.00",
         "reason": "damaged goods", "status": "issued"},
        task_id=task.task_id)
    service.report_step(task.task_id, 1, StepReportIn(ok=True))
    return task


def test_live_navigation_then_the_step_report_verifies(service):
    task = live_cross_page(service)
    service.report_navigation(task.task_id, NavigationReportIn(
        from_page="Invoices", to_page="Customers"))
    apply_field_change(service.store, "customers", "2041", "email",
                       "creditnotes@harbourview.example")
    task = service.report_step(task.task_id, 2, StepReportIn(ok=True))
    assert task.status == TaskStatus.VERIFIED
    attempt = service.get_attempts(task.task_id)[0]
    assert [(n.seq, n.from_page, n.to_page, n.driver)
            for n in attempt.navigations] == [
        (2, "Invoices", "Customers", "page-agent-live")]


def test_a_step_report_that_skips_the_navigation_is_refused(service):
    task = live_cross_page(service)
    apply_field_change(service.store, "customers", "2041", "email",
                       "creditnotes@harbourview.example")
    with pytest.raises(GateError) as caught:
        service.report_step(task.task_id, 2, StepReportIn(ok=True))
    assert "navigation_missing" in str(caught.value)
    # Recorded as a protocol refusal; the attempt is undisturbed
    # and the task still awaits its reports.
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "step_report_refused"]
    assert len(refused) == 1
    assert refused[0]["detail"]["code"] == "navigation_missing"
    assert refused[0]["detail"]["current_page"] == "Invoices"
    assert refused[0]["detail"]["step_page"] == "Customers"
    assert service.get_task(task.task_id).status == \
        TaskStatus.CONFIRMED
    # The bridge navigates, reports again, and the run completes:
    # the refusal cost it a round trip, not the execution.
    service.report_navigation(task.task_id, NavigationReportIn(
        from_page="Invoices", to_page="Customers"))
    task = service.report_step(task.task_id, 2, StepReportIn(ok=True))
    assert task.status == TaskStatus.VERIFIED


def test_a_navigation_from_the_wrong_page_is_refused(service):
    task = live_cross_page(service)
    with pytest.raises(GateError):
        service.report_navigation(task.task_id, NavigationReportIn(
            from_page="Orders", to_page="Customers"))
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "navigation_refused"]
    assert refused[-1]["detail"]["code"] == "from_page_mismatch"
    assert refused[-1]["detail"]["current_page"] == "Invoices"


def test_a_navigation_to_the_wrong_page_is_refused(service):
    task = live_cross_page(service)
    with pytest.raises(GateError):
        service.report_navigation(task.task_id, NavigationReportIn(
            from_page="Invoices", to_page="Orders"))
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "navigation_refused"]
    assert refused[-1]["detail"]["code"] == \
        "unexpected_destination"


def test_no_navigation_is_owed_before_the_first_step(service):
    task = plan_and_confirm(service)
    service.execute(task.task_id, "page-agent-live")
    with pytest.raises(GateError):
        service.report_navigation(task.task_id, NavigationReportIn(
            from_page="Orders", to_page="Invoices"))
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "navigation_refused"]
    assert refused[-1]["detail"]["code"] == "no_current_page"


def test_no_navigation_is_owed_within_one_page(service):
    task = plan_and_confirm(
        service,
        "raise a credit memo for invoice 4471, reason damaged "
        "goods, $120 and mark invoice 4471 as paid")
    service.execute(task.task_id, "page-agent-live")
    create_credit_memo(
        service.store,
        {"invoice_id": "4471", "amount": "120.00",
         "reason": "damaged goods", "status": "issued"},
        task_id=task.task_id)
    service.report_step(task.task_id, 1, StepReportIn(ok=True))
    with pytest.raises(GateError):
        service.report_navigation(task.task_id, NavigationReportIn(
            from_page="Invoices", to_page="Customers"))
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "navigation_refused"]
    assert refused[-1]["detail"]["code"] == "no_navigation_owed"


def test_a_resumed_live_execution_stands_where_it_failed(service):
    task = live_cross_page(service)
    service.report_navigation(task.task_id, NavigationReportIn(
        from_page="Invoices", to_page="Customers"))
    task = service.report_step(
        task.task_id, 2,
        StepReportIn(ok=False, detail="the form timed out"))
    assert task.status == TaskStatus.EXECUTION_FAILED
    task = service.resume_execution(task.task_id)
    assert task.status == TaskStatus.CONFIRMED
    # The execution stands on Customers — the page the failed
    # step worked on — so the remaining step reports with no new
    # navigation.
    apply_field_change(service.store, "customers", "2041", "email",
                       "creditnotes@harbourview.example")
    task = service.report_step(task.task_id, 2, StepReportIn(ok=True))
    assert task.status == TaskStatus.VERIFIED


def test_navigation_reports_ride_the_api():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    client = TestClient(app)
    body = client.post("/api/tasks", json={
        "text": CREDIT_AND_CUSTOMER, "operator": "op"}).json()
    task_id = body["task"]["task_id"]
    client.post(f"/api/tasks/{task_id}/confirm",
                json={"operator": "s.hale"})
    client.post(f"/api/tasks/{task_id}/execute",
                json={"driver": "page-agent-live"})
    client.post(f"/api/tasks/{task_id}/steps/1/report",
                json={"ok": True})
    # Skipping the navigation: the step report is a 409.
    skipped = client.post(f"/api/tasks/{task_id}/steps/2/report",
                          json={"ok": True})
    assert skipped.status_code == 409
    nav = client.post(f"/api/tasks/{task_id}/navigation", json={
        "from_page": "Invoices", "to_page": "Customers"})
    assert nav.status_code == 200
    detail = client.get(f"/api/tasks/{task_id}").json()
    assert detail["attempts"][0]["navigations"] == [{
        "seq": 2, "from_page": "Invoices", "to_page": "Customers",
        "driver": "page-agent-live"}]
