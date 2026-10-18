"""Round 2 — plan depth: multi-record plans verified step by step,
amendments that bind confirmations to exact versions, plan diffs,
and staleness that refuses execution before anything is written."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.models import AmendChangeIn, Plan, PlanStep
from page_agent_copilot.models import StepAction, TaskStatus
from page_agent_copilot.plandiff import diff_plans
from page_agent_copilot.policy import PlanRefusal
from page_agent_copilot.records import apply_field_change
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
CREDIT_AND_SETTLE = CREDIT + " and mark invoice 4471 as paid"
SHIP = ("mark order 1032 as shipped, ship date 2026-10-20, "
        "carrier DHL")


# ------------------------------------------------------- multi-record

def test_credit_memo_plus_invoice_status_one_plan_two_records(service):
    task = service.intake(CREDIT_AND_SETTLE, "j.okafor")
    assert task.status == TaskStatus.PLANNED
    plan = service.get_plan(task.task_id)
    assert plan.task_type == "credit_memo_and_invoice_status"
    assert [s.seq for s in plan.steps] == [1, 2]
    assert plan.steps[0].action == StepAction.CREATE
    assert plan.steps[1].record_kind == "invoices"
    assert plan.steps[1].field == "status"
    assert plan.steps[1].value == "paid"
    # The plan rests on the invoice it touches — one precondition,
    # not two (both steps derive from invoice 4471).
    assert [(p.record_kind, p.record_id) for p in plan.preconditions] == [
        ("invoices", "4471")]
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    memos = service.store.list_records("credit_memos")
    assert len(memos) == 1 and memos[0]["amount"] == "120.00"
    assert service.store.get_record("invoices", "4471")["status"] == "paid"
    seqs = [e["detail"]["seq"] for e in service.audit(task.task_id)
            if e["event"] == "step_executed"]
    assert seqs == [1, 2]  # plan order is execution order


def test_ship_details_three_steps_one_record_exact_diff(service):
    before = service.store.get_record("orders", "1032")
    task = service.intake(SHIP, "j.okafor")
    plan = service.get_plan(task.task_id)
    assert plan.task_type == "order_ship_details"
    assert [(s.field, s.value) for s in plan.steps] == [
        ("status", "shipped"), ("ship_date", "2026-10-20"),
        ("carrier", "DHL")]
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    after = service.store.get_record("orders", "1032")
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"status", "ship_date", "carrier"}


def test_compound_status_changes_across_two_records(service):
    task = service.intake(
        "mark order 1036 as packed and mark invoice 4474 as paid",
        "j.okafor")
    plan = service.get_plan(task.task_id)
    assert plan.task_type == "status_changes"
    assert [(s.record_kind, s.record_id) for s in plan.steps] == [
        ("orders", "1036"), ("invoices", "4474")]
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.get_record("orders", "1036")["status"] == "packed"
    assert service.store.get_record("invoices", "4474")["status"] == "paid"


def test_failing_step_is_named_in_the_discrepancy(service):
    task = service.intake(CREDIT_AND_SETTLE, "j.okafor")
    service.confirm(task.task_id, "s.hale")
    # The invoice step (seq 2) writes 'open' instead of 'paid'.
    task = service.execute(task.task_id, "offline", tamper={2: "open"})
    assert task.status == TaskStatus.DISCREPANCY
    failed = [e for e in service.audit(task.task_id)
              if e["event"] == "verification_failed"]
    diffs = failed[0]["detail"]["diffs"]
    assert len(diffs) == 1
    assert diffs[0]["seq"] == 2
    assert diffs[0]["field"] == "status"
    assert diffs[0]["planned"] == "paid" and diffs[0]["actual"] == "open"
    # Step 1 did happen — and the task still reports no success.
    assert len(service.store.list_records("credit_memos")) == 1


# --------------------------------------------------------- amendments

def test_amendment_creates_v2_and_confirmation_binds_v2(service):
    task = service.intake("update customer 2041 phone to 555-0134",
                          "j.okafor")
    task, plan, changes = service.amend(
        task.task_id, "j.okafor",
        [AmendChangeIn(seq=1, value="555-0177")])
    assert task.status == TaskStatus.PLANNED
    assert plan.version == 2 and task.plan_version == 2
    assert [(c.seq, c.field, c.old, c.new) for c in changes] == [
        (1, "phone", "555-0134", "555-0177")]
    amended = [e for e in service.audit(task.task_id)
               if e["event"] == "plan_amended"]
    assert amended[0]["detail"]["from_version"] == 1
    assert amended[0]["detail"]["to_version"] == 2
    # The v1 record is kept, readable, marked superseded.
    v1 = service.get_plan(task.task_id, 1)
    assert v1.superseded is True and v1.steps[0].value == "555-0134"
    # Confirm binds v2 exactly; execution writes v2's value.
    task = service.confirm(task.task_id, "s.hale")
    assert task.confirmed_version == 2
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.get_record(
        "customers", "2041")["phone"] == "555-0177"


def test_amendment_of_credit_amount_flows_to_the_memo(service):
    task = service.intake(CREDIT, "j.okafor")
    task, plan, changes = service.amend(
        task.task_id, "j.okafor",
        [AmendChangeIn(seq=1, fields={"amount": "150"})])
    assert [(c.field, c.old, c.new) for c in changes] == [
        ("amount", "120.00", "150.00")]  # normalised to plan form
    assert plan.steps[0].fields["amount"] == "150.00"
    assert "150.00" in plan.steps[0].instruction
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    memo = service.store.list_records("credit_memos")[0]
    assert memo["amount"] == "150.00"


def test_confirmed_plan_cannot_be_amended(service):
    task = service.intake(CREDIT, "j.okafor")
    service.confirm(task.task_id, "s.hale")
    with pytest.raises(GateError):
        service.amend(task.task_id, "j.okafor",
                      [AmendChangeIn(seq=1, fields={"amount": "150.00"})])
    # The confirmed v1 executes exactly as confirmed.
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.list_records(
        "credit_memos")[0]["amount"] == "120.00"


@pytest.mark.parametrize("changes, code", [
    ([AmendChangeIn(seq=1, fields={"amount": "900.00"})],
     "credit_above_cap"),
    ([AmendChangeIn(seq=1, fields={"status": "void"})],
     "value_not_governed"),
    ([AmendChangeIn(seq=1, fields={"memo": "x"})], "field_not_plannable"),
    ([AmendChangeIn(seq=9, fields={"amount": "1.00"})], "step_not_found"),
])
def test_amendments_that_break_policy_are_refused(service, changes, code):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(PlanRefusal) as caught:
        service.amend(task.task_id, "j.okafor", changes)
    assert caught.value.code == code
    # A refused amendment changes nothing: still PLANNED at v1.
    after = service.get_task(task.task_id)
    assert after.status == TaskStatus.PLANNED and after.plan_version == 1


def test_amendment_to_ungoverned_status_value_refused(service):
    task = service.intake("mark order 1032 as shipped", "j.okafor")
    with pytest.raises(PlanRefusal) as caught:
        service.amend(task.task_id, "j.okafor",
                      [AmendChangeIn(seq=1, value="teleported")])
    assert caught.value.code == "value_not_governed"


def test_noop_amendment_is_refused(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.amend(task.task_id, "j.okafor",
                      [AmendChangeIn(seq=1,
                                     fields={"amount": "120.00"})])


def test_reject_is_terminal_and_blocks_execution(service):
    task = service.intake(CREDIT, "j.okafor")
    service.confirm(task.task_id, "s.hale")
    task = service.reject(task.task_id, "j.okafor")
    assert task.status == TaskStatus.REJECTED
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline")
    with pytest.raises(GateError):
        service.amend(task.task_id, "j.okafor",
                      [AmendChangeIn(seq=1, fields={"amount": "1.00"})])


# ------------------------------------------------------------- diffs

def test_plan_diff_between_versions(service):
    task = service.intake(CREDIT_AND_SETTLE, "j.okafor")
    service.amend(task.task_id, "j.okafor", [
        AmendChangeIn(seq=1, fields={"amount": "150.00"}),
        AmendChangeIn(seq=2, value="void")])
    diff = service.plan_diff(task.task_id, 1, 2)
    assert diff.from_version == 1 and diff.to_version == 2
    by_seq = {d.seq: d for d in diff.step_diffs}
    assert by_seq[1].change == "changed"
    assert [(c.field, c.old, c.new) for c in by_seq[1].changes] == [
        ("amount", "120.00", "150.00")]
    assert [(c.field, c.old, c.new) for c in by_seq[2].changes] == [
        ("status", "paid", "void")]
    assert service.plan_diff(task.task_id, 1, 1).identical is True


def test_diff_plans_added_and_removed_steps():
    step = PlanStep(seq=1, action=StepAction.SET, page="Orders",
                    record_kind="orders", record_id="1",
                    field="status", value="shipped")
    extra = PlanStep(seq=2, action=StepAction.SET, page="Orders",
                     record_kind="orders", record_id="1",
                     field="carrier", value="DHL")
    one = Plan(task_id="T", task_type="x", summary="", steps=[step])
    two = Plan(task_id="T", task_type="x", summary="",
               steps=[step, extra])
    forward = diff_plans(one, two)
    assert [(d.seq, d.change) for d in forward.step_diffs] == [
        (2, "added")]
    assert [(d.seq, d.change) for d in diff_plans(two, one).step_diffs] == [
        (2, "removed")]


# ---------------------------------------------------------- staleness

def test_record_change_between_plan_and_execute_is_stale(service):
    task = service.intake("update customer 2041 phone to 555-0134",
                          "j.okafor")
    service.confirm(task.task_id, "s.hale")
    # A human edits the same record in the console meanwhile.
    apply_field_change(service.store, "customers", "2041", "city",
                       "Bath")
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline")
    after = service.get_task(task.task_id)
    assert after.status == TaskStatus.STALE
    assert after.stale_records == ["customers 2041"]
    assert after.driver is None  # nothing started
    # The task wrote nothing: the human's edit stands, the planned
    # phone change never landed.
    record = service.store.get_record("customers", "2041")
    assert record["city"] == "Bath" and record["phone"] == "555-0101"
    stale = [e for e in service.audit(task.task_id)
             if e["event"] == "execution_stale"]
    assert stale[0]["detail"]["changed_records"] == ["customers 2041"]


def test_stale_refusal_also_guards_live_arming(service):
    task = service.intake("mark order 1032 as shipped", "j.okafor")
    service.confirm(task.task_id, "s.hale")
    apply_field_change(service.store, "orders", "1032", "carrier",
                       "FedEx")
    with pytest.raises(GateError):
        service.execute(task.task_id, "page-agent-live")
    assert service.get_task(task.task_id).status == TaskStatus.STALE


def test_replan_after_stale_supersedes_and_executes(service):
    task = service.intake("update customer 2041 phone to 555-0134",
                          "j.okafor")
    service.confirm(task.task_id, "s.hale")
    apply_field_change(service.store, "customers", "2041", "city",
                       "Bath")
    with pytest.raises(GateError):
        service.execute(task.task_id, "offline")
    task = service.replan(task.task_id, "j.okafor")
    assert task.status == TaskStatus.PLANNED
    assert task.plan_version == 2 and task.stale_records == []
    v1 = service.get_plan(task.task_id, 1)
    assert v1.superseded is True
    v2 = service.get_plan(task.task_id, 2)
    assert v2.superseded is False
    assert v2.preconditions  # re-derived from the records as they stand
    task = service.confirm(task.task_id, "s.hale")
    assert task.confirmed_version == 2
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    record = service.store.get_record("customers", "2041")
    assert record["phone"] == "555-0134" and record["city"] == "Bath"


def test_replan_requires_stale(service):
    task = service.intake(CREDIT, "j.okafor")
    with pytest.raises(GateError):
        service.replan(task.task_id, "j.okafor")


def test_unrelated_record_change_does_not_stale_the_plan(service):
    task = service.intake("update customer 2041 phone to 555-0134",
                          "j.okafor")
    service.confirm(task.task_id, "s.hale")
    apply_field_change(service.store, "customers", "2042", "city",
                       "Hull")  # a record this plan does not touch
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED


# ---------------------------------------------------------------- API

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def test_amend_diff_and_versions_over_http(client):
    created = client.post("/api/tasks", json={
        "text": CREDIT_AND_SETTLE, "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    amended = client.post(f"/api/tasks/{tid}/amend", json={
        "operator": "j.okafor",
        "changes": [{"seq": 1, "fields": {"amount": "150.00"}}]})
    assert amended.status_code == 200
    assert amended.json()["plan"]["version"] == 2
    assert amended.json()["changes"] == [
        {"seq": 1, "field": "amount", "old": "120.00", "new": "150.00"}]
    versions = client.get(f"/api/tasks/{tid}/plans").json()
    assert [p["version"] for p in versions] == [1, 2]
    assert versions[0]["superseded"] is True
    diff = client.get(
        f"/api/tasks/{tid}/plan-diff?from_version=1&to_version=2").json()
    assert diff["step_diffs"][0]["changes"][0]["field"] == "amount"
    # The amended version is what confirmation binds and execution runs.
    confirmed = client.post(f"/api/tasks/{tid}/confirm",
                            json={"operator": "s.hale"})
    assert confirmed.json()["confirmed_version"] == 2
    done = client.post(f"/api/tasks/{tid}/execute",
                       json={"driver": "offline"})
    assert done.json()["status"] == "VERIFIED"
    memo = client.get("/api/console/credit-memos").json()[0]
    assert memo["amount"] == "150.00"


def test_amend_confirmed_task_over_http_is_409(client):
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    client.post(f"/api/tasks/{tid}/confirm", json={"operator": "s.hale"})
    res = client.post(f"/api/tasks/{tid}/amend", json={
        "operator": "j.okafor",
        "changes": [{"seq": 1, "fields": {"amount": "150.00"}}]})
    assert res.status_code == 409


def test_amend_breaking_policy_over_http_is_422_with_code(client):
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    res = client.post(f"/api/tasks/{tid}/amend", json={
        "operator": "j.okafor",
        "changes": [{"seq": 1, "fields": {"amount": "900.00"}}]})
    assert res.status_code == 422
    assert res.json()["code"] == "credit_above_cap"
    # Nothing changed: the task still sits at plan v1.
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["task"]["plan_version"] == 1


def test_stale_execute_over_http_is_409_and_replan_recovers(client):
    created = client.post("/api/tasks", json={
        "text": "mark order 1032 as shipped", "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    client.post(f"/api/tasks/{tid}/confirm", json={"operator": "s.hale"})
    client.put("/api/console/orders/1032", json={
        "fields": {"carrier": "FedEx"}})  # a human edit in the console
    res = client.post(f"/api/tasks/{tid}/execute",
                      json={"driver": "offline"})
    assert res.status_code == 409
    detail = client.get(f"/api/tasks/{tid}").json()
    assert detail["task"]["status"] == "STALE"
    assert detail["task"]["stale_records"] == ["orders 1032"]
    replanned = client.post(f"/api/tasks/{tid}/replan",
                            json={"operator": "s.hale"})
    assert replanned.json()["task"]["status"] == "PLANNED"
    assert replanned.json()["plan"]["version"] == 2
