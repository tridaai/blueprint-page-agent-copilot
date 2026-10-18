"""Round 14: ambiguous tasks get a clarification, never an
invented value.

Derivation that matches a governed shape in more than one way —
and only that derivation — parks the task in
AWAITING_CLARIFICATION with options computed from the records.
The classes, in code: a record described by a governed status
word that matches several records; a credit amount the task
omits where the records offer a governed candidate set; a status
value that prefixes two governed values. Everything else refuses
exactly as before. Answering is a recorded act under the
planning role rules; a non-answer is refused with the options
restated; and after two distinct questions a third ambiguity
refuses the task with the history attached."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.batches import BatchMemberIn, next_act_for
from page_agent_copilot.config import Settings
from page_agent_copilot.models import TaskStatus
from page_agent_copilot.records import apply_field_change
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore

DESCRIPTOR_CREDIT = ("raise a credit memo for the open invoice, "
                     "reason damaged goods, $120")
PLAIN_CREDIT = ("raise a credit memo for invoice 4471, reason "
                "damaged goods, $120")
AMOUNTLESS_CREDIT = ("raise a credit memo for invoice 4471, "
                     "reason damaged goods")


@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def run_to_verified(svc, text, operator="op"):
    task = svc.intake(text, operator)
    svc.confirm(task.task_id, "s.hale")
    task = svc.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    return task


def audit_codes(svc, task_id, event):
    return [e["detail"].get("code") for e in svc.audit(task_id)
            if e["event"] == event]


# ------------------------------------------------------- descriptors

def test_a_descriptor_with_several_matches_asks_and_names_them(service):
    task = service.intake(DESCRIPTOR_CREDIT, "op")
    assert task.status == TaskStatus.AWAITING_CLARIFICATION
    assert service.get_plan(task.task_id) is None
    pending = task.pending_clarification
    assert pending is not None
    assert pending.key == "credit_invoice"
    assert pending.code == "ambiguous_record"
    # The four invoices the seed leaves open, each named with the
    # record fields that distinguish it.
    assert [o.value for o in pending.options] == [
        "4471", "4472", "4475", "4476"]
    labels = {o.value: o.label for o in pending.options}
    assert "$1,840.00" in labels["4471"]
    assert "2026-10-31" in labels["4471"]
    assert "Harbourview" in labels["4471"]
    assert "the open invoice" in pending.question
    asked = [e for e in service.audit(task.task_id)
             if e["event"] == "clarification_asked"]
    assert len(asked) == 1
    assert asked[0]["detail"]["code"] == "ambiguous_record"


def test_answering_the_descriptor_plans_and_verifies(service):
    task = service.intake(DESCRIPTOR_CREDIT, "op")
    task = service.answer_clarification(task.task_id, "op", "4471")
    assert task.status == TaskStatus.PLANNED
    plan = service.get_plan(task.task_id)
    assert plan.steps[0].fields == {
        "invoice_id": "4471", "amount": "120.00",
        "reason": "damaged goods", "status": "issued"}
    entry = task.clarifications[0]
    assert entry.answer == "4471" and entry.answerer == "op"
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    memo = service.store.get_record("credit_memos", "CM-5001")
    assert memo["invoice_id"] == "4471"
    assert memo["amount"] == "120.00"


def test_a_descriptor_with_one_match_derives_without_asking(service):
    # Exactly one invoice is overdue in the seed: the choice is
    # derivable from the records, so no question is asked.
    task = service.intake(
        "raise a credit memo for the overdue invoice, reason "
        "damaged goods, $120", "op")
    assert task.status == TaskStatus.PLANNED
    assert task.clarifications == []
    plan = service.get_plan(task.task_id)
    assert plan.steps[0].fields["invoice_id"] == "4474"


def test_a_descriptor_with_no_match_refuses(service):
    task = service.intake(
        "raise a credit memo for the void invoice, reason "
        "damaged goods, $120", "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("record_not_found")


def test_too_many_matches_refuses_rather_than_offering(service):
    # Paying nothing and voiding nothing, open six invoices by
    # hand: a choice that wide is stated by number, not picked
    # from a clarification.
    for rid in ("4473", "4474"):
        apply_field_change(service.store, "invoices", rid,
                           "status", "open")
    task = service.intake(DESCRIPTOR_CREDIT, "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("no_plan_derivable")
    assert "6 invoices" in task.refusal_reason


def test_a_descriptor_status_clause_asks(service):
    task = service.intake("mark the packed order as shipped", "op")
    assert task.status == TaskStatus.AWAITING_CLARIFICATION
    pending = task.pending_clarification
    assert pending.key == "status_record:orders:packed"
    assert [o.value for o in pending.options] == ["1032", "1037"]
    task = service.answer_clarification(task.task_id, "op", "1032")
    plan = service.get_plan(task.task_id)
    assert plan.steps[0].record_id == "1032"
    assert plan.steps[0].value == "shipped"
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.get_record("orders", "1032")["status"] \
        == "shipped"


def test_a_descriptor_clause_mixed_with_a_numbered_clause_refuses(
        service):
    task = service.intake(
        "mark the packed order as shipped and mark order 1033 "
        "as delivered", "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("no_plan_derivable")


def test_a_descriptor_word_the_kind_cannot_carry_refuses(service):
    # "paid" is a governed invoice status, not an order status:
    # an order cannot be described by it.
    task = service.intake("mark the paid order as shipped", "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("no_plan_derivable")


# ------------------------------------------------------ missing amount

def test_a_missing_amount_with_candidates_asks(service):
    run_to_verified(service, PLAIN_CREDIT)  # CM-5001, $120 on 4471
    task = service.intake(AMOUNTLESS_CREDIT, "op")
    assert task.status == TaskStatus.AWAITING_CLARIFICATION
    pending = task.pending_clarification
    assert pending.key == "credit_amount"
    assert pending.code == "ambiguous_amount"
    assert [o.value for o in pending.options] == ["1840.00", "120.00"]
    labels = {o.value: o.label for o in pending.options}
    assert "invoice's full amount" in labels["1840.00"]
    assert "CM-5001" in labels["120.00"]
    task = service.answer_clarification(task.task_id, "op", "120.00")
    assert task.status == TaskStatus.PLANNED
    plan = service.get_plan(task.task_id)
    assert plan.steps[0].fields["amount"] == "120.00"
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    memos = service.store.list_records("credit_memos")
    assert len(memos) == 2
    assert memos[1]["amount"] == "120.00"


def test_a_clarified_amount_is_still_policy_checked(service):
    # The options are candidates the records offer, not
    # approvals: picking the invoice's full amount walks into
    # the same credit cap a stated amount would.
    run_to_verified(service, PLAIN_CREDIT)
    task = service.intake(AMOUNTLESS_CREDIT, "op")
    task = service.answer_clarification(task.task_id, "op", "1840.00")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("credit_above_cap")


def test_a_missing_amount_without_a_candidate_set_refuses(service):
    # No memo stands against 4471 yet, so the only candidate is
    # the invoice's own amount — a set of one is not a choice,
    # and the refusal stands: clarification is not a backdoor.
    task = service.intake(AMOUNTLESS_CREDIT, "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("no_plan_derivable")
    assert task.clarifications == []


# ------------------------------------------------------ prefix status

def test_a_prefix_status_value_asks_between_governed_values(service):
    task = service.intake("mark order 1036 as p", "op")
    assert task.status == TaskStatus.AWAITING_CLARIFICATION
    pending = task.pending_clarification
    assert pending.key == "status_value:orders:1036"
    assert pending.code == "ambiguous_status"
    assert [o.value for o in pending.options] == ["packed", "placed"]
    task = service.answer_clarification(task.task_id, "op", "packed")
    plan = service.get_plan(task.task_id)
    assert plan.steps[0].value == "packed"
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    assert service.store.get_record("orders", "1036")["status"] \
        == "packed"


def test_a_prefix_on_an_invoice_clause_asks(service):
    task = service.intake("mark invoice 4474 as o", "op")
    assert task.status == TaskStatus.AWAITING_CLARIFICATION
    pending = task.pending_clarification
    assert [o.value for o in pending.options] == ["open", "overdue"]
    task = service.answer_clarification(task.task_id, "op", "open")
    plan = service.get_plan(task.task_id)
    assert plan.steps[0].value == "open"


def test_a_prefix_matching_one_value_stays_a_refusal(service):
    # "ship" prefixes exactly one governed order status. A
    # unique continuation is not a choice between governed
    # values, so the old refusal stands — clarification is not
    # a spelling corrector.
    task = service.intake("mark order 1036 as ship", "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("value_not_governed")
    task = service.intake("mark order 1036 as vanished", "op")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("value_not_governed")


# ------------------------------------------------------------ answering

def test_a_non_answer_is_refused_with_the_options_restated(service):
    task = service.intake(DESCRIPTOR_CREDIT, "op")
    with pytest.raises(GateError) as caught:
        service.answer_clarification(task.task_id, "op",
                                     "the big one")
    message = str(caught.value)
    assert "not one of the options" in message
    assert "4471" in message and "4476" in message
    task = service.get_task(task.task_id)
    assert task.status == TaskStatus.AWAITING_CLARIFICATION
    assert task.pending_clarification.answer is None
    assert audit_codes(service, task.task_id,
                       "clarification_refused") == ["not_an_option"]
    # The question still stands, and a real answer proceeds.
    task = service.answer_clarification(task.task_id, "op", "4472")
    assert task.status == TaskStatus.PLANNED


def test_the_planner_may_answer_but_confirmation_stays_second_person(
        service):
    task = service.intake(DESCRIPTOR_CREDIT, "op")
    task = service.answer_clarification(task.task_id, "op", "4471")
    assert task.status == TaskStatus.PLANNED
    with pytest.raises(GateError):
        service.confirm(task.task_id, "op")  # self-confirmation
    service.confirm(task.task_id, "s.hale")
    assert service.get_task(task.task_id).status == TaskStatus.CONFIRMED


def test_a_viewer_may_not_answer(service):
    task = service.intake(DESCRIPTOR_CREDIT, "op")
    with pytest.raises(GateError):
        service.answer_clarification(task.task_id, "v.ward", "4471")
    assert audit_codes(service, task.task_id,
                       "clarification_refused") == ["role_cannot_plan"]
    assert service.get_task(task.task_id).status \
        == TaskStatus.AWAITING_CLARIFICATION


def test_an_unknown_name_may_not_answer(service):
    task = service.intake(DESCRIPTOR_CREDIT, "op")
    with pytest.raises(GateError):
        service.answer_clarification(task.task_id, "nobody", "4471")
    assert audit_codes(service, task.task_id,
                       "clarification_refused") == ["unknown_operator"]


def test_a_deactivated_operator_may_not_answer(service):
    task = service.intake(DESCRIPTOR_CREDIT, "j.okafor")
    service.deactivate_operator("s.hale", "j.okafor")
    with pytest.raises(GateError):
        service.answer_clarification(task.task_id, "j.okafor",
                                     "4471")
    assert audit_codes(service, task.task_id,
                       "clarification_refused") == ["operator_inactive"]
    # Another plan-capable operator can still answer it.
    task = service.answer_clarification(task.task_id, "op", "4471")
    assert task.status == TaskStatus.PLANNED


def test_answering_a_task_that_is_not_waiting_raises(service):
    task = service.intake(PLAIN_CREDIT, "op")
    assert task.status == TaskStatus.PLANNED
    with pytest.raises(GateError):
        service.answer_clarification(task.task_id, "op", "4471")
    assert audit_codes(service, task.task_id,
                       "clarification_refused") == []


# ------------------------------------------------------ patience + pins

def test_a_third_distinct_ambiguity_refuses_with_the_history(service):
    run_to_verified(service, PLAIN_CREDIT)
    task = service.intake(
        "raise a credit memo for the open invoice, reason "
        "damaged goods and mark invoice 4472 as o", "op")
    assert task.pending_clarification.key == "credit_invoice"
    task = service.answer_clarification(task.task_id, "op", "4471")
    assert task.pending_clarification.key == "credit_amount"
    task = service.answer_clarification(task.task_id, "op", "120.00")
    # The settle tail's "o" is a third distinct question: the
    # task refuses, and the refusal carries the history.
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("clarification_exhausted")
    assert len(task.clarifications) == 2
    assert all(c.answer is not None for c in task.clarifications)
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "plan_refused"]
    assert refused[-1]["detail"]["code"] == "clarification_exhausted"
    assert len(refused[-1]["detail"]["clarifications"]) == 2


def test_a_basis_that_moves_reasks_without_spending_patience(service):
    run_to_verified(service, PLAIN_CREDIT)
    task = service.intake(
        "raise a credit memo for the open invoice, reason "
        "damaged goods", "op")
    task = service.answer_clarification(task.task_id, "op", "4471")
    assert task.pending_clarification.key == "credit_amount"
    # The invoice the operator picked is paid before they answer
    # the amount question: the pin no longer stands among the
    # descriptor's matches, so the question is asked again with
    # the fresh options — the same question, so no new patience
    # is spent, and the stale pin is never honoured.
    apply_field_change(service.store, "invoices", "4471",
                       "status", "paid")
    task = service.answer_clarification(task.task_id, "op", "120.00")
    assert task.status == TaskStatus.AWAITING_CLARIFICATION
    pending = task.pending_clarification
    assert pending.key == "credit_invoice"
    assert [o.value for o in pending.options] == [
        "4472", "4475", "4476"]
    assert [c.key for c in task.clarifications] == [
        "credit_invoice", "credit_amount", "credit_invoice"]
    # Answering with a different invoice re-derives against THAT
    # invoice's records: the $120 candidate belonged to 4471's
    # records, not 4472's, so the pinned amount no longer stands
    # either — and 4472 offers no candidate set to clarify
    # between, so the task refuses rather than carrying a value
    # across records.
    task = service.answer_clarification(task.task_id, "op", "4472")
    assert task.status == TaskStatus.REFUSED
    assert task.refusal_reason.startswith("no_plan_derivable")


def test_an_awaiting_task_of_a_deactivated_operator_is_orphaned(
        service):
    # The new state owes its operator an act like the others: if
    # that operator is deactivated, the work is stranded work —
    # it surfaces as orphaned, and a pickup hands it to a
    # supervisor, who can then answer it.
    task = service.intake(DESCRIPTOR_CREDIT, "j.okafor")
    service.deactivate_operator("s.hale", "j.okafor")
    orphaned = service.orphaned_work()
    assert [o["task_id"] for o in orphaned] == [task.task_id]
    service.pickup_task(task.task_id, "s.hale")
    task = service.answer_clarification(task.task_id, "s.hale",
                                        "4471")
    assert task.status == TaskStatus.PLANNED


# ------------------------------------------------------------ surfaces

def test_a_batch_member_awaiting_clarification_shows_the_clarify_act(
        service):
    batch = service.create_batch(
        "op", [BatchMemberIn(text=DESCRIPTOR_CREDIT)])
    sheet = service.batch_run_sheet(batch)
    row = sheet["members"][0]
    assert row["status"] == "AWAITING_CLARIFICATION"
    assert row["next_act"]["action"] == "clarify"
    assert "Which invoice" in row["next_act"]["detail"]
    report = service.batch_report(batch)
    assert report["complete"] is False
    task = service.get_task(row["task_id"])
    assert next_act_for(task)["action"] == "clarify"


def test_clarifications_persist_on_the_sqlite_store(sqlite_service):
    task = sqlite_service.intake(DESCRIPTOR_CREDIT, "op")
    task = sqlite_service.answer_clarification(
        task.task_id, "op", "4475")
    stored = sqlite_service.get_task(task.task_id)
    assert stored.status == TaskStatus.PLANNED
    assert stored.clarifications[0].answer == "4475"
    assert stored.clarifications[0].answerer == "op"
    plan = sqlite_service.get_plan(task.task_id)
    assert plan.steps[0].fields["invoice_id"] == "4475"


def test_clarify_over_http(client):
    created = client.post("/api/tasks", json={
        "text": DESCRIPTOR_CREDIT, "operator": "op"})
    assert created.status_code == 201
    task = created.json()["task"]
    assert task["status"] == "AWAITING_CLARIFICATION"
    assert task["clarifications"][0]["options"][0]["value"] == "4471"
    tid = task["task_id"]
    refused = client.post(f"/api/tasks/{tid}/clarify", json={
        "operator": "op", "choice": "the big one"})
    assert refused.status_code == 409
    assert "4471" in refused.json()["detail"]
    answered = client.post(f"/api/tasks/{tid}/clarify", json={
        "operator": "op", "choice": "4471"})
    assert answered.status_code == 200
    body = answered.json()
    assert body["status"] == "PLANNED"
    assert body["clarifications"][0]["answer"] == "4471"
    assert body["clarifications"][0]["answerer"] == "op"
