"""Round 13 — accessibility mode: the spoken form of a plan,
read-back confirmation, transcript intake, and the operator
preference.

The invariants under test, stated once:

- The spoken form is a view: rendered in code from the stored
  plan, figures spelled, dates spoken — and asking for it
  changes nothing that is stored.
- Read-back confirmation is the same gate with one binding
  added: the token the shell issued when the plan was read,
  bound to the task, the plan version, the spoken form's hash,
  and the operator it was issued to. Every round-3 rule —
  second person, confirm-capable role, active name — runs
  unchanged on this path too.
- Transcript intake normalises the envelope and hands the
  ordinary grammar the cleaned text: the plan a transcript
  produces is the plan the same words typed produce.
- The preference flag is presentation: it changes how the
  shell presents and permits nothing.
"""
import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.models import AmendChangeIn, TaskStatus
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore

CREDIT = ("raise a credit memo for invoice 4471, reason "
          "damaged goods, $120")
CROSS_PAGE = (CREDIT + " and update the customer's email to "
              "creditnotes@harbourview.example")
SHIP = ("mark order 1032 as shipped, ship date 2026-10-20, "
        "carrier Northgate Freight")


def _refusals(service, task_id):
    return [e for e in service.audit(task_id)
            if e["event"] == "confirmation_refused"]


# ------------------------------------------------------- the spoken form

def test_spoken_view_spells_the_amount(service):
    task = service.intake(CREDIT, "op")
    view = service.spoken_view(task.task_id)
    assert view["plan_version"] == 1
    assert "one hundred and twenty" in view["spoken"]
    assert "120.00" not in view["spoken"]
    assert "invoice 4471" in view["spoken"]
    assert "damaged goods" in view["spoken"]
    assert view["pages"] == ["Invoices"]
    assert view["multi_page"] is False


def test_spoken_view_names_both_pages_of_a_multi_page_plan(service):
    task = service.intake(CROSS_PAGE, "op")
    view = service.spoken_view(task.task_id)
    assert view["multi_page"] is True
    assert "two pages: Invoices, then Customers" in view["spoken"]
    assert "one hundred and twenty" in view["spoken"]


def test_spoken_view_speaks_dates(service):
    task = service.intake(SHIP, "op")
    spoken = service.spoken_view(task.task_id)["spoken"]
    assert "the twentieth of October, two thousand and " \
           "twenty-six" in spoken
    assert "2026-10-20" not in spoken


def test_spoken_view_is_a_view_the_stored_plan_never_changes(service):
    task = service.intake(CREDIT, "op")
    before = service.get_plan(task.task_id).model_dump(mode="json")
    first = service.spoken_view(task.task_id)
    second = service.spoken_view(task.task_id)
    after = service.get_plan(task.task_id).model_dump(mode="json")
    assert before == after
    assert first == second  # deterministic: same plan, same words
    assert service.get_task(task.task_id).status == TaskStatus.PLANNED


# ------------------------------------------------------- read-back

def test_readback_confirms_end_to_end(service):
    task = service.intake(CREDIT, "op")
    issued = service.issue_readback(task.task_id, "s.hale")
    assert issued["plan_version"] == 1
    assert issued["issued_to"] == "s.hale"
    assert issued["spoken_hash"] == \
        service.spoken_view(task.task_id)["spoken_hash"]
    confirmed = service.confirm_readback(
        task.task_id, "s.hale", issued["token"])
    assert confirmed.status == TaskStatus.CONFIRMED
    assert confirmed.confirmed_version == 1
    done = service.execute(task.task_id, "offline")
    assert done.status == TaskStatus.VERIFIED
    trail = service.audit(task.task_id)
    assert "readback_issued" in [e["event"] for e in trail]
    entry = [e for e in trail if e["event"] == "plan_confirmed"][0]
    assert entry["detail"]["method"] == "readback"
    assert entry["detail"]["spoken_hash"] == issued["spoken_hash"]
    assert entry["detail"]["confirmed_version"] == 1


def test_readback_token_for_v1_is_refused_after_amendment(service):
    task = service.intake(CREDIT, "op")
    issued = service.issue_readback(task.task_id, "s.hale")
    service.amend(task.task_id, "op", [AmendChangeIn(
        seq=1, fields={"amount": "150.00"})])
    with pytest.raises(GateError, match="read-back"):
        service.confirm_readback(task.task_id, "s.hale",
                                 issued["token"])
    refusals = _refusals(service, task.task_id)
    assert refusals[-1]["detail"]["code"] == "readback_stale"
    # The audit shows why: both versions are named in the reason.
    assert "v1" in refusals[-1]["detail"]["reason"]
    assert "v2" in refusals[-1]["detail"]["reason"]
    assert service.get_task(task.task_id).status == TaskStatus.PLANNED
    # Hearing the new version is the way through: a fresh token
    # bound to v2 confirms v2.
    fresh = service.issue_readback(task.task_id, "s.hale")
    assert fresh["plan_version"] == 2
    assert "one hundred and fifty" in fresh["spoken"]
    confirmed = service.confirm_readback(
        task.task_id, "s.hale", fresh["token"])
    assert confirmed.confirmed_version == 2


def test_readback_token_binds_task_and_operator(service):
    task = service.intake(CREDIT, "op")
    other = service.intake(SHIP, "op")
    issued = service.issue_readback(task.task_id, "s.hale")
    # Another operator presenting the token: refused — the
    # operator who heard the plan confirms it.
    service.enroll_operator("s.hale", "k.reyes", "supervisor")
    with pytest.raises(GateError):
        service.confirm_readback(task.task_id, "k.reyes",
                                 issued["token"])
    assert _refusals(service, task.task_id)[-1]["detail"]["code"] \
        == "readback_operator_mismatch"
    # The token presented against a different task: refused.
    with pytest.raises(GateError):
        service.confirm_readback(other.task_id, "s.hale",
                                 issued["token"])
    assert _refusals(service, other.task_id)[-1]["detail"]["code"] \
        == "readback_unknown"
    # A token the shell never issued: refused.
    with pytest.raises(GateError):
        service.confirm_readback(task.task_id, "s.hale", "0" * 32)
    assert _refusals(service, task.task_id)[-1]["detail"]["code"] \
        == "readback_unknown"


def test_readback_keeps_every_confirmation_rule(service):
    task = service.intake(CREDIT, "op")
    # Self-confirmation: the planner's own token confirms
    # nothing, exactly as on the written path.
    own = service.issue_readback(task.task_id, "op")
    with pytest.raises(GateError):
        service.confirm_readback(task.task_id, "op", own["token"])
    assert _refusals(service, task.task_id)[-1]["detail"]["code"] \
        == "self_confirmation"
    # A role that cannot confirm: refused on this path too.
    viewer = service.issue_readback(task.task_id, "v.ward")
    with pytest.raises(GateError):
        service.confirm_readback(task.task_id, "v.ward",
                                 viewer["token"])
    assert _refusals(service, task.task_id)[-1]["detail"]["code"] \
        == "role_cannot_confirm"
    # A deactivated confirmer: refused at issue and at confirm.
    service.enroll_operator("s.hale", "boss", "supervisor")
    hale = service.issue_readback(task.task_id, "s.hale")
    service.deactivate_operator("boss", "s.hale")
    with pytest.raises(GateError):
        service.confirm_readback(task.task_id, "s.hale",
                                 hale["token"])
    assert _refusals(service, task.task_id)[-1]["detail"]["code"] \
        == "operator_inactive"
    with pytest.raises(GateError):
        service.issue_readback(task.task_id, "s.hale")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "readback_refused"]
    assert refused[-1]["detail"]["code"] == "operator_inactive"
    # An unknown name is refused a token, on the record.
    with pytest.raises(GateError):
        service.issue_readback(task.task_id, "mallory")
    refused = [e for e in service.audit(task.task_id)
               if e["event"] == "readback_refused"]
    assert refused[-1]["detail"]["code"] == "unknown_operator"


def test_readback_of_a_task_with_no_plan_waiting_refuses(service):
    task = service.intake(CREDIT, "op")
    service.confirm(task.task_id, "s.hale")
    with pytest.raises(GateError, match="PLANNED"):
        service.issue_readback(task.task_id, "s.hale")


# ------------------------------------------------------- transcripts

TRANSCRIPT = (
    "[00:03] Operator: raise a credit memo for invoice 4471,\n"
    "Operator: reason damaged goods, $120")


def test_transcript_intake_plans_identically_to_typed_text(service):
    typed = service.intake(CREDIT, "op")
    spoken_task = service.intake_transcript(TRANSCRIPT, "op")
    assert spoken_task.text == CREDIT
    typed_plan = service.get_plan(typed.task_id)
    spoken_plan = service.get_plan(spoken_task.task_id)
    assert spoken_plan.task_type == typed_plan.task_type
    assert spoken_plan.summary == typed_plan.summary
    assert [s.model_dump(mode="json") for s in spoken_plan.steps] \
        == [s.model_dump(mode="json") for s in typed_plan.steps]
    trail = service.audit(spoken_task.task_id)
    entry = [e for e in trail
             if e["event"] == "transcript_intake"][0]
    assert entry["detail"]["raw"] == TRANSCRIPT
    assert entry["detail"]["normalized"] == CREDIT


def test_transcript_with_spelled_out_numbers_is_refused(service):
    # The documented non-goal: numbers as words are not
    # converted — ids and amounts must arrive as digits, so
    # this transcript is underivable text and refuses whole.
    task = service.intake_transcript(
        "Operator: raise a credit memo for invoice four four "
        "seven one, reason damaged goods, one hundred twenty "
        "dollars", "op")
    assert task.status == TaskStatus.REFUSED


def test_transcript_intake_keeps_the_identity_rules(service):
    task = service.intake_transcript(TRANSCRIPT, "v.ward")
    assert task.status == TaskStatus.REFUSED
    assert "role_cannot_plan" in task.refusal_reason


# ------------------------------------------------------- the preference

def test_preference_is_a_recorded_supervisor_act(service):
    service.set_operator_preference("s.hale", "op", True)
    entry = service.directory_entry("op")
    assert service.entry_prefers_spoken(entry) is True
    scope = service.role_audit()
    changed = [e for e in scope
               if e["event"] == "operator_preference_changed"]
    assert changed[-1]["detail"] == {
        "operator": "op", "prefers_spoken": True}
    # An operator may not set it — administration is a
    # supervisor act, recorded as refused.
    with pytest.raises(GateError):
        service.set_operator_preference("op", "j.okafor", True)
    refused = [e for e in service.role_audit()
               if e["event"] == "directory_refused"]
    assert refused[-1]["detail"]["code"] == "role_cannot_administer"
    # Setting the value the entry already carries: refused.
    with pytest.raises(GateError):
        service.set_operator_preference("s.hale", "op", True)
    refused = [e for e in service.role_audit()
               if e["event"] == "directory_refused"]
    assert refused[-1]["detail"]["code"] == "no_change"


def test_preference_changes_presentation_only(service):
    service.set_operator_preference("s.hale", "op", True)
    # The planner who prefers the spoken form still cannot
    # confirm their own plan — by either path.
    task = service.intake(CREDIT, "op")
    with pytest.raises(GateError):
        service.confirm(task.task_id, "op")
    own = service.issue_readback(task.task_id, "op")
    with pytest.raises(GateError):
        service.confirm_readback(task.task_id, "op", own["token"])
    codes = [e["detail"]["code"]
             for e in _refusals(service, task.task_id)]
    assert codes == ["self_confirmation", "self_confirmation"]
    # A viewer with the preference still plans nothing.
    service.set_operator_preference("s.hale", "v.ward", True)
    refused = service.intake(CREDIT, "v.ward")
    assert refused.status == TaskStatus.REFUSED
    # And the preference holder's ordinary flow is untouched.
    issued = service.issue_readback(task.task_id, "s.hale")
    service.confirm_readback(task.task_id, "s.hale",
                             issued["token"])
    assert service.execute(task.task_id, "offline").status \
        == TaskStatus.VERIFIED


# ------------------------------------------------------- the API surface

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def _intake(client, text=CREDIT, operator="op"):
    res = client.post("/api/tasks",
                      json={"text": text, "operator": operator})
    assert res.status_code == 201
    return res.json()["task"]


def test_api_spoken_read_and_readback_flow(client):
    task = _intake(client)
    res = client.get(f"/api/tasks/{task['task_id']}/plan/spoken")
    assert res.status_code == 200
    view = res.json()
    assert "one hundred and twenty" in view["spoken"]
    issued = client.post(f"/api/tasks/{task['task_id']}/readback",
                         json={"operator": "s.hale"})
    assert issued.status_code == 200
    token = issued.json()["token"]
    confirmed = client.post(
        f"/api/tasks/{task['task_id']}/confirm-readback",
        json={"operator": "s.hale", "token": token})
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"
    # A bogus token on a fresh task refuses at the gate: 409.
    task2 = _intake(client, SHIP)
    refused = client.post(
        f"/api/tasks/{task2['task_id']}/confirm-readback",
        json={"operator": "s.hale", "token": "f" * 32})
    assert refused.status_code == 409


def test_api_transcript_intake(client):
    res = client.post("/api/tasks/transcript", json={
        "text": TRANSCRIPT, "operator": "op"})
    assert res.status_code == 201
    body = res.json()
    assert body["task"]["text"] == CREDIT
    assert body["task"]["status"] == "PLANNED"
    assert body["plan"]["steps"][0]["fields"]["amount"] == "120.00"


def test_api_task_detail_leads_with_spoken_when_preferred(client):
    plain = _intake(client)
    detail = client.get(f"/api/tasks/{plain['task_id']}").json()
    assert detail["operator_prefers_spoken"] is False
    assert "spoken" not in detail
    res = client.put("/api/operators/op/preference", json={
        "operator": "s.hale", "prefers_spoken": True})
    assert res.status_code == 200
    assert res.json()["prefers_spoken"] is True
    voiced = _intake(client, SHIP)
    detail = client.get(f"/api/tasks/{voiced['task_id']}").json()
    assert detail["operator_prefers_spoken"] is True
    assert "the twentieth of October" in detail["spoken"]["spoken"]
