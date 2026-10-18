"""Round 19 — the template regression gate: a version becomes
the default only by passing. Publishing never moved the default
before this round either — it just looked that way, because a
name-only instantiation followed whatever was published last.
Now: evaluations are append-only records grading a version's
exact content hash; promotion is a separate supervisor act that
requires a passing evaluation by hands other than the
publisher's, a promoter who is not the publisher, and no
regression against the standing default's examples; the seeded
versions are grandfathered as defaults, their standing the
health, marked seeded — no fabricated evaluation records."""

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.config import Settings
from page_agent_copilot.service import CopilotService, GateError
from page_agent_copilot.store import InMemoryStore
from page_agent_copilot.templates import TemplateError

SLOTS = [
    {"name": "order", "kind": "record", "record_kind": "orders"},
    {"name": "status", "kind": "text"},
]
EXAMPLE_PACKED = {
    "slots": {"order": "1036", "status": "packed"},
    "task_type": "order_status",
    "steps": [{"action": "set", "record_kind": "orders",
               "record_id": "1036", "field": "status",
               "value": "packed"}],
}

GOOD_V2 = {
    "name": "order-status",
    "pattern": "mark order {order} as {status}",
    "description": "A single order status change (library wording, v2).",
    "slots": SLOTS,
    "examples": [EXAMPLE_PACKED],
}

# The same slots and pattern, but the pinned example now expects
# a shape the rendered text does not derive: the version's own
# evaluation fails.
BROKEN_V2 = {
    **GOOD_V2,
    "description": "A single order status change (broken pins).",
    "examples": [{
        **EXAMPLE_PACKED,
        "steps": [{"action": "set", "record_kind": "orders",
                   "record_id": "1036", "field": "status",
                   "value": "shipped"}]}],
}

# A two-example template, published fresh: the regression craft.
DUO_V1 = {
    "name": "gate-duo",
    "pattern": "mark order {order} as {status}",
    "description": "Two standing scenarios, one template.",
    "slots": SLOTS,
    "examples": [
        EXAMPLE_PACKED,
        {"slots": {"order": "1032", "status": "shipped"},
         "task_type": "order_status",
         "steps": [{"action": "set", "record_kind": "orders",
                    "record_id": "1032", "field": "status",
                    "value": "shipped"}]},
    ],
}

# Same slots, same scenarios — but the pattern now also settles
# an invoice, so each scenario derives two steps where the
# incumbent pins one. The candidate passes its own pins; the
# incumbent's ruler is what it breaks.
DUO_V2 = {
    **DUO_V1,
    "pattern": ("mark order {order} as {status} and mark "
                "invoice 4474 as paid"),
    "description": "Now with the invoice settled in the same task.",
    "examples": [
        {**example,
         "task_type": "status_changes",
         "steps": example["steps"] + [
             {"action": "set", "record_kind": "invoices",
              "record_id": "4474", "field": "status",
              "value": "paid"}]}
        for example in DUO_V1["examples"]],
}

PACKED = {"order": "1036", "status": "packed"}


def second_supervisor(service):
    service.enroll_operator("s.hale", "r.osei", "supervisor")
    return "r.osei"


def refusal_codes(service):
    return [e["detail"]["code"] for e in service.template_audit()
            if e["event"] == "template_refused"]


def promote_good_v2(service):
    """Publish GOOD_V2 (by s.hale), evaluate it (by the
    operator j.okafor), promote it (by a second supervisor).
    Returns the promotion record."""
    promoter = second_supervisor(service)
    service.publish_template("s.hale", GOOD_V2)
    service.evaluate_template("j.okafor", "order-status", 2)
    return service.promote_template(promoter, "order-status", 2)


# ------------------------------------------------------- grandfathering

def test_seeded_versions_are_grandfathered_defaults(service):
    for template in service.list_templates():
        default = service.default_template(template.name)
        assert default is not None and default.version == 1
        view = service.template_view(template)
        assert view["default"] is True
        assert view["promoted"] is False
        assert view["standing"] == "seeded"
        assert view["evaluation"] is None
        # Grandfathering is a standing, not a record: the gate
        # books stay empty until someone actually runs them.
        assert service.template_evaluations(template.name) == []
        assert service.template_promotions(template.name) == []


def test_a_publish_alone_does_not_move_the_default(service):
    service.publish_template("s.hale", GOOD_V2)
    assert service.default_template("order-status").version == 1
    view = service.template_view(
        service.get_template("order-status", 2))
    assert view["default"] is False and view["standing"] == \
        "published"
    task = service.instantiate_template("order-status", None,
                                        PACKED, "j.okafor")
    assert task.template_version == 1
    explicit = service.instantiate_template("order-status", 2,
                                            PACKED, "j.okafor")
    assert explicit.template_version == 2


# ------------------------------------------------------------ evaluation

def test_evaluation_records_the_verdict_against_the_hash(service):
    service.publish_template("s.hale", GOOD_V2)
    record = service.evaluate_template("j.okafor", "order-status",
                                       2)
    assert record["ok"] is True
    assert record["evaluated_by"] == "j.okafor"
    assert record["template"] == "order-status"
    assert record["version"] == 2
    assert all(e["ok"] for e in record["examples"])
    view = service.template_view(
        service.get_template("order-status", 2))
    assert record["content_hash"] == view["content_hash"]
    assert view["evaluation"]["ok"] is True
    assert view["evaluation"]["hash_matches"] is True
    assert view["evaluation"]["hash_short"] == \
        record["content_hash"][:12]
    audit = [e for e in service.template_audit()
             if e["event"] == "template_evaluated"]
    assert len(audit) == 1 and audit[0]["actor"] == "j.okafor"
    assert audit[0]["detail"]["content_hash"] == \
        record["content_hash"]
    # Append-only: a second evaluation is a second record.
    again = service.evaluate_template("j.okafor", "order-status",
                                      2)
    assert again["seq"] > record["seq"]
    assert len(service.template_evaluations("order-status",
                                            2)) == 2


def test_evaluation_is_behind_the_identity_gate(service):
    service.publish_template("s.hale", GOOD_V2)
    with pytest.raises(GateError):
        service.evaluate_template("mallory", "order-status", 2)
    with pytest.raises(GateError):
        service.evaluate_template("j.okafor", "order-status", 9)
    assert refusal_codes(service) == [
        "unknown_operator", "template_not_found"]
    assert service.template_evaluations("order-status") == []


def test_a_failing_evaluation_is_recorded_as_a_fail(service):
    service.publish_template("s.hale", BROKEN_V2)
    record = service.evaluate_template("j.okafor", "order-status",
                                       2)
    assert record["ok"] is False
    assert record["examples"][0]["ok"] is False
    assert "shipped" in record["examples"][0]["detail"]


# ------------------------------------------------------------- promotion

def test_promotion_without_an_evaluation_is_refused(service):
    promoter = second_supervisor(service)
    service.publish_template("s.hale", GOOD_V2)
    with pytest.raises(GateError):
        service.promote_template(promoter, "order-status", 2)
    assert refusal_codes(service) == ["no_evaluation"]
    assert service.default_template("order-status").version == 1


def test_the_publisher_may_not_promote_their_own_version(service):
    service.publish_template("s.hale", GOOD_V2)
    service.evaluate_template("j.okafor", "order-status", 2)
    with pytest.raises(GateError):
        service.promote_template("s.hale", "order-status", 2)
    assert refusal_codes(service) == ["promoter_was_publisher"]


def test_the_publishers_own_evaluation_does_not_count(service):
    promoter = second_supervisor(service)
    service.publish_template("s.hale", GOOD_V2)
    own = service.evaluate_template("s.hale", "order-status", 2)
    assert own["ok"] is True  # recorded — the record is honest
    with pytest.raises(GateError):
        service.promote_template(promoter, "order-status", 2)
    assert refusal_codes(service) == ["evaluator_was_publisher"]
    # Another hand's pass is the one the gate counts.
    service.evaluate_template("j.okafor", "order-status", 2)
    record = service.promote_template(promoter, "order-status", 2)
    assert record["version"] == 2


def test_promotion_is_a_supervisor_act(service):
    service.publish_template("s.hale", GOOD_V2)
    service.evaluate_template("j.okafor", "order-status", 2)
    with pytest.raises(GateError):
        service.promote_template("j.okafor", "order-status", 2)
    with pytest.raises(GateError):
        service.promote_template("mallory", "order-status", 2)
    assert refusal_codes(service) == [
        "role_cannot_administer", "unknown_operator"]


def test_a_clean_candidate_promotes_and_becomes_the_default(
        service):
    record = promote_good_v2(service)
    assert record["template"] == "order-status"
    assert record["version"] == 2
    assert record["previous_default"] == 1
    assert record["promoted_by"] == "r.osei"
    assert record["evaluation_source"] == "evaluation"
    assert record["evaluated_by"] == "j.okafor"
    view = service.template_view(
        service.get_template("order-status", 2))
    assert view["default"] is True and view["promoted"] is True
    assert view["standing"] == "promoted"
    assert record["content_hash"] == view["content_hash"]
    seeded = service.template_view(
        service.get_template("order-status", 1))
    assert seeded["default"] is False
    assert seeded["standing"] == "seeded"
    task = service.instantiate_template("order-status", None,
                                        PACKED, "j.okafor")
    assert task.template_version == 2
    audit = [e for e in service.template_audit()
             if e["event"] == "template_promoted"]
    assert len(audit) == 1
    assert audit[0]["detail"]["previous_default"] == 1
    with pytest.raises(GateError):
        service.promote_template("r.osei", "order-status", 2)
    assert refusal_codes(service) == ["already_default"]


def test_a_failing_candidate_cannot_promote(service):
    promoter = second_supervisor(service)
    service.publish_template("s.hale", BROKEN_V2)
    service.evaluate_template("j.okafor", "order-status", 2)
    with pytest.raises(GateError) as caught:
        service.promote_template(promoter, "order-status", 2)
    assert refusal_codes(service) == ["evaluation_failed"]
    assert "1036" in str(caught.value)
    assert service.default_template("order-status").version == 1


# ------------------------------------------------------------- regression

def promote_duo_v1(service):
    promoter = second_supervisor(service)
    service.publish_template("s.hale", DUO_V1)
    service.evaluate_template("j.okafor", "gate-duo", 1)
    service.promote_template(promoter, "gate-duo", 1)
    return promoter


def test_regression_refuses_and_names_the_broken_examples(
        service):
    promoter = promote_duo_v1(service)
    service.publish_template("s.hale", DUO_V2)
    evaluation = service.evaluate_template("j.okafor", "gate-duo",
                                           2)
    assert evaluation["ok"] is True  # its own pins pass
    with pytest.raises(GateError) as caught:
        service.promote_template(promoter, "gate-duo", 2)
    assert refusal_codes(service) == ["regression"]
    assert "1036" in str(caught.value)
    assert "1032" in str(caught.value)
    refusal = [e for e in service.template_audit()
               if e["event"] == "template_refused"][0]
    broken = refusal["detail"]["broken"]
    assert {tuple(sorted(b["slots"].items())) for b in broken} == {
        (("order", "1032"), ("status", "shipped")),
        (("order", "1036"), ("status", "packed"))}
    assert service.default_template("gate-duo").version == 1


def test_regression_names_what_the_candidate_gains(service):
    promoter = promote_duo_v1(service)
    wider = {
        **DUO_V1,
        "pattern": ("mark order {order} as {status}, ship date "
                    "{ship_date}, carrier {carrier}"),
        "description": "Now with shipment details.",
        "slots": SLOTS + [
            {"name": "ship_date", "kind": "date"},
            {"name": "carrier", "kind": "text"}],
        "examples": [{
            "slots": {"order": "1032", "status": "shipped",
                      "ship_date": "2026-10-20", "carrier": "DHL"},
            "task_type": "order_ship_details",
            "steps": [
                {"action": "set", "record_kind": "orders",
                 "record_id": "1032", "field": "status",
                 "value": "shipped"},
                {"action": "set", "record_kind": "orders",
                 "record_id": "1032", "field": "ship_date",
                 "value": "2026-10-20"},
                {"action": "set", "record_kind": "orders",
                 "record_id": "1032", "field": "carrier",
                 "value": "DHL"}]}],
    }
    service.publish_template("s.hale", wider)
    evaluation = service.evaluate_template("j.okafor", "gate-duo",
                                           2)
    assert evaluation["ok"] is True
    with pytest.raises(GateError):
        service.promote_template(promoter, "gate-duo", 2)
    assert refusal_codes(service) == ["regression"]
    refusal = [e for e in service.template_audit()
               if e["event"] == "template_refused"][0]
    # The standing scenarios are unfillable under the new slots;
    # the candidate's own new scenario is named as its gain.
    assert len(refusal["detail"]["broken"]) == 2
    assert refusal["detail"]["gains"] == [
        {"order": "1032", "status": "shipped",
         "ship_date": "2026-10-20", "carrier": "DHL"}]


# ---------------------------------------------------------------- tamper

def test_content_changed_after_evaluation_refuses_on_hash(
        service):
    promoter = second_supervisor(service)
    service.publish_template("s.hale", GOOD_V2)
    record = service.evaluate_template("j.okafor", "order-status",
                                       2)
    # A store-level edit — versions are never edited through the
    # service, so this is bytes moved behind the gate's back.
    raw = next(r for r in service.store.list_records("templates")
               if r["id"] == "order-status:v2")
    raw = {**raw, "description": "Edited after evaluation."}
    service.store.upsert_record("templates", raw)
    view = service.template_view(
        service.get_template("order-status", 2))
    assert view["content_hash"] != record["content_hash"]
    assert view["evaluation"]["hash_matches"] is False
    with pytest.raises(GateError):
        service.promote_template(promoter, "order-status", 2)
    assert refusal_codes(service) == ["hash_mismatch"]
    # Evaluating the version as it stands restores its standing.
    service.evaluate_template("j.okafor", "order-status", 2)
    promoted = service.promote_template(promoter, "order-status",
                                         2)
    assert promoted["content_hash"] == view["content_hash"]


# --------------------------------------------------------------- rollback

def test_rollback_restores_the_seeded_default(service):
    promoter = promote_good_v2(service)["promoted_by"]
    assert service.default_template("order-status").version == 2
    record = service.promote_template(promoter, "order-status", 1)
    assert record["previous_default"] == 2
    # The seeded version's standing is its health — no evaluation
    # record was fabricated for it on the way back either.
    assert record["evaluation_source"] == "seeded"
    assert service.template_evaluations("order-status", 1) == []
    assert service.default_template("order-status").version == 1
    task = service.instantiate_template("order-status", None,
                                        PACKED, "j.okafor")
    assert task.template_version == 1
    assert len(service.template_promotions("order-status")) == 2


# ------------------------------------------------------------ brand-new

def test_a_brand_new_template_has_no_default_until_promoted(
        service):
    promoter = second_supervisor(service)
    service.publish_template("s.hale", {
        **GOOD_V2, "name": "order-status-copy"})
    with pytest.raises(TemplateError) as caught:
        service.instantiate_template("order-status-copy", None,
                                     PACKED, "j.okafor")
    assert caught.value.code == "no_promoted_default"
    refusal = [e for e in service.template_audit()
               if e["event"] ==
               "template_instantiation_refused"]
    assert refusal[0]["detail"]["code"] == "no_promoted_default"
    assert service.list_tasks() == []
    explicit = service.instantiate_template(
        "order-status-copy", 1, PACKED, "j.okafor")
    assert explicit.template_version == 1
    service.evaluate_template("j.okafor", "order-status-copy", 1)
    service.promote_template(promoter, "order-status-copy", 1)
    task = service.instantiate_template("order-status-copy", None,
                                        PACKED, "j.okafor")
    assert task.template_version == 1


# ------------------------------------------------------------ persistence

def test_the_gate_survives_a_store_round_trip(sqlite_service):
    promoter = second_supervisor(sqlite_service)
    sqlite_service.publish_template("s.hale", GOOD_V2)
    sqlite_service.evaluate_template("j.okafor", "order-status", 2)
    sqlite_service.promote_template(promoter, "order-status", 2)
    again = CopilotService(sqlite_service.settings,
                           sqlite_service.store)
    assert again.default_template("order-status").version == 2
    assert len(again.template_evaluations("order-status", 2)) == 1
    assert len(again.template_promotions("order-status")) == 1
    task = again.instantiate_template("order-status", None,
                                      PACKED, "j.okafor")
    assert task.template_version == 2


# ------------------------------------------------------------------- API

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(),
                     seed=True)
    return TestClient(app)


def test_the_gate_on_the_api(client):
    client.post("/api/operators", json={
        "operator": "s.hale", "name": "r.osei",
        "role": "supervisor"})
    published = client.post("/api/templates", json={
        "operator": "s.hale", **GOOD_V2})
    assert published.status_code == 201
    listing = client.get("/api/templates").json()
    v2 = next(r for r in listing
              if r["name"] == "order-status" and r["version"] == 2)
    assert v2["default"] is False and v2["standing"] == "published"
    assert v2["evaluation"] is None

    early = client.post("/api/templates/order-status/promote",
                        json={"operator": "r.osei", "version": 2})
    assert early.status_code == 409
    evaluated = client.post("/api/templates/order-status/evaluate",
                            json={"operator": "j.okafor",
                                  "version": 2})
    assert evaluated.status_code == 201
    assert evaluated.json()["ok"] is True
    own = client.post("/api/templates/order-status/promote",
                      json={"operator": "s.hale", "version": 2})
    assert own.status_code == 409
    promoted = client.post("/api/templates/order-status/promote",
                           json={"operator": "r.osei",
                                 "version": 2})
    assert promoted.status_code == 201
    assert promoted.json()["previous_default"] == 1

    listing = client.get("/api/templates").json()
    v1 = next(r for r in listing
              if r["name"] == "order-status" and r["version"] == 1)
    v2 = next(r for r in listing
              if r["name"] == "order-status" and r["version"] == 2)
    assert v2["default"] is True and v2["standing"] == "promoted"
    assert v2["evaluation"]["ok"] is True
    assert v1["default"] is False and v1["standing"] == "seeded"
    audit = client.get("/api/templates-audit").json()
    events = [e["event"] for e in audit]
    assert "template_evaluated" in events
    assert "template_promoted" in events
    created = client.post(
        "/api/templates/order-status/instantiate",
        json={"operator": "j.okafor", "slots": PACKED})
    assert created.status_code == 201
    assert created.json()["task"]["template_version"] == 2
