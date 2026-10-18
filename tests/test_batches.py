"""Round 8: batches — work submitted together, worked one
task at a time.

The batch is a record of a submission: member task ids in
order, the creator, and the refusals that stopped a member
before a task existed. Everything else — the run sheet, the
report, whether the batch is complete — is folded from the
member records at read time. And the round's invariant, proven
against the route table itself: there is no batch decision.
Confirmation and execution stay per task, per the role rules
and the version binding, exactly as for a task stated alone.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.batches import (
    BatchError,
    BatchMemberIn,
    next_act_for,
)
from page_agent_copilot.config import Settings
from page_agent_copilot.models import StepReportIn, TaskStatus
from page_agent_copilot.records import apply_field_change
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore, SqlStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
CREDIT_SETTLE = CREDIT + " and mark invoice 4471 as paid"
OVERCAP = ("raise a credit memo for invoice 4471, reason "
           "goodwill, $900")
SHIP = "mark order 1032 as shipped"
SHIP_FULL = ("mark order 1032 as shipped, ship date 2026-10-20, "
             "carrier DHL")


def text_member(text):
    return BatchMemberIn(text=text)


def template_member(name, slots, version=None):
    return BatchMemberIn(template=name, version=version, slots=slots)


def finish(service, task_id):
    """One member's ordinary lifecycle: confirm, execute."""
    service.confirm(task_id, "s.hale")
    return service.execute(task_id, "offline")


# -- creation ---------------------------------------------------------------

def test_batch_from_texts_keeps_order_and_the_refused_member(service):
    batch = service.create_batch("op", [
        text_member(CREDIT), text_member(OVERCAP),
        text_member(SHIP)])
    assert [m.position for m in batch.members] == [1, 2, 3]
    assert [m.kind for m in batch.members] == ["text"] * 3
    assert batch.created_by == "op"
    tasks = [service.get_task(m.task_id) for m in batch.members]
    assert [t.status for t in tasks] == [
        TaskStatus.PLANNED, TaskStatus.REFUSED, TaskStatus.PLANNED]
    # The refused member is IN the batch, with its reason on the
    # task — not dropped, not retried, not hidden.
    assert "credit_above_cap" in (tasks[1].refusal_reason or "")
    # The batch id rides on each member task, and each member's
    # own trail names the batch it was submitted in.
    assert all(t.batch_id == batch.batch_id for t in tasks)
    for m in batch.members:
        events = [e for e in service.audit(m.task_id)
                  if e["event"] == "batch_member"]
        assert len(events) == 1
        assert events[0]["detail"]["batch_id"] == batch.batch_id
        assert events[0]["detail"]["position"] == m.position
    created = [e for e in service.batch_audit()
               if e["event"] == "batch_created"]
    assert len(created) == 1
    assert created[0]["actor"] == "op"
    assert [m["task_id"] for m in created[0]["detail"]["members"]] == \
        [m.task_id for m in batch.members]


def test_batch_record_stores_no_status(service):
    # Completion is a fold, never a stored claim: the record in
    # the store holds the members and the creator, and no field
    # that could drift from them.
    batch = service.create_batch("op", [text_member(CREDIT)])
    raw = service.store.get_record("batches", batch.batch_id)
    assert "status" not in raw
    assert "complete" not in raw
    view = service.batch_view(batch)
    assert view["status"] == "open"
    finish(service, batch.members[0].task_id)
    # Nothing rewrote the batch — the fold simply reads the
    # member as it now stands.
    assert service.store.get_record("batches", batch.batch_id) == raw
    view = service.batch_view(service.get_batch(batch.batch_id))
    assert view["status"] == "complete"
    assert view["members_terminal"] == 1


def test_batch_from_templates_and_texts_together(service):
    batch = service.create_batch("op", [
        template_member("credit-memo", {
            "invoice": "4472", "reason": "short shipment",
            "amount": "75"}),
        text_member(SHIP),
        template_member("credit-memo", {
            "invoice": "9999", "reason": "damaged goods",
            "amount": "10"}),
        template_member("credit-memo", {
            "invoice": "4471", "reason": "goodwill",
            "amount": "900"}),
    ])
    one, two, three, four = batch.members
    # A template member instantiates through intake: the task
    # carries the template's name and the exact version used.
    task = service.get_task(one.task_id)
    assert task.status == TaskStatus.PLANNED
    assert task.template_name == "credit-memo"
    assert task.template_version == 1
    assert one.template_version == 1 and one.text == task.text
    assert service.get_task(two.task_id).status == TaskStatus.PLANNED
    # A slot naming a missing record: no task exists, and the
    # member itself carries the refusal — visible in the batch.
    assert three.task_id is None
    assert three.refusal_code == "record_not_found"
    view = service.batch_view(batch)
    row = view["members"][2]
    assert row["status"] == "REFUSED" and row["terminal"] is True
    assert "record_not_found" in row["refusal_reason"]
    assert row["next_act"]["action"] is None
    # A policy-breaking slot value is intake's own REFUSED task,
    # in policy's own words — the batch adds no second language.
    task = service.get_task(four.task_id)
    assert task.status == TaskStatus.REFUSED
    assert (task.refusal_reason or "").startswith("credit_above_cap")
    assert task.template_name == "credit-memo"


def test_batch_template_member_by_name_and_version(service):
    service.publish_template("s.hale", {
        "name": "order-status",
        "pattern": "mark order {order} as {status}",
        "description": "A single order status change (v2 wording).",
        "slots": [
            {"name": "order", "kind": "record",
             "record_kind": "orders"},
            {"name": "status", "kind": "text"},
        ],
        "examples": [{
            "slots": {"order": "1036", "status": "packed"},
            "task_type": "order_status",
            "steps": [{"action": "set", "record_kind": "orders",
                       "record_id": "1036", "field": "status",
                       "value": "packed"}],
        }],
    })
    batch = service.create_batch("op", [
        template_member("order-status",
                        {"order": "1036", "status": "packed"},
                        version=1)])
    member = batch.members[0]
    assert member.template_version == 1
    assert service.get_task(member.task_id).template_version == 1


def test_malformed_batch_submissions_refuse_whole(service):
    for members, code in (
            ([], "no_members"),
            ([BatchMemberIn()], "member_invalid"),
            ([BatchMemberIn(text=CREDIT, template="credit-memo")],
             "member_invalid"),
            ([BatchMemberIn(text="   ")], "member_invalid")):
        with pytest.raises(BatchError) as caught:
            service.create_batch("op", members)
        assert caught.value.code == code
    # Nothing was stated and no batch was stored: a bad list is
    # refused whole, not salvaged into a partial batch.
    assert service.list_tasks() == []
    assert service.list_batches() == []
    refusals = [e for e in service.batch_audit()
                if e["event"] == "batch_refused"]
    assert [r["detail"]["code"] for r in refusals] == [
        "no_members", "member_invalid", "member_invalid",
        "member_invalid"]


def test_batch_creation_adds_no_permission(service):
    # A viewer may not plan — so a viewer's batch is a batch of
    # refusals, each in the planner's own words. The batch door
    # is not a way around the role rules.
    batch = service.create_batch("v.ward", [text_member(CREDIT)])
    task = service.get_task(batch.members[0].task_id)
    assert task.status == TaskStatus.REFUSED
    assert "role_cannot_plan" in (task.refusal_reason or "")
    report = service.batch_report(batch)
    assert report["complete"] is True
    assert report["counts_by_state"] == {"REFUSED": 1}


def test_batches_list_in_submission_order(service):
    # Three batches, so the order cannot pass by luck: it is
    # the creation sequence's order, not the timestamp's — all
    # three can land inside the same second, where created_at
    # ties and only the sequence separates them (the id is
    # random and separates nothing).
    first = service.create_batch("op", [text_member(SHIP)])
    second = service.create_batch("op", [text_member(CREDIT)])
    third = service.create_batch("op", [text_member(SHIP_FULL)])
    assert [b.batch_id for b in service.list_batches()] == [
        first.batch_id, second.batch_id, third.batch_id]
    assert first.seq < second.seq < third.seq
    assert [b.seq for b in service.list_batches()] == [
        first.seq, second.seq, third.seq]
    assert service.get_batch("BATCH-NOPE") is None


# -- the run sheet ------------------------------------------------------------

def test_run_sheet_names_the_next_act_and_who(service):
    batch = service.create_batch("op", [
        text_member(CREDIT), text_member(OVERCAP)])
    sheet = service.batch_run_sheet(batch)
    assert sheet["status"] == "open"
    assert sheet["owing"] == [batch.members[0].task_id]
    first, second = sheet["members"]
    assert first["next_act"]["action"] == "confirm"
    assert "v1" in first["next_act"]["detail"]
    # Who may confirm: a confirm-capable second person — the
    # sheet names the planner so the exclusion is visible.
    assert "op" in first["next_act"]["who"]
    assert "other than the planner" in first["next_act"]["who"]
    # A refused member owes nothing and says why it ended.
    assert second["next_act"]["action"] is None
    assert "credit_above_cap" in second["next_act"]["detail"]
    # Confirm the first member per-task and the sheet moves on.
    service.confirm(first["task_id"], "s.hale")
    sheet = service.batch_run_sheet(batch)
    assert sheet["members"][0]["next_act"]["action"] == "execute"
    assert sheet["members"][0]["status"] == "CONFIRMED"


def test_next_act_for_each_owing_state(service):
    failed = service.create_batch("op", [text_member(SHIP_FULL)])
    tid = failed.members[0].task_id
    service.confirm(tid, "s.hale")
    service.execute(tid, "offline", fail={2: "bridge timeout"})
    sheet = service.batch_run_sheet(failed)
    act = sheet["members"][0]["next_act"]
    assert act["action"] == "resume"
    assert "step 2" in act["detail"]
    assert "bridge timeout" in act["detail"]

    stale = service.create_batch("op", [text_member(SHIP)])
    tid = stale.members[0].task_id
    service.confirm(tid, "s.hale")
    apply_field_change(service.store, "orders", "1032", "carrier",
                       "FedEx")
    with pytest.raises(GateError):
        service.execute(tid, "offline")
    sheet = service.batch_run_sheet(stale)
    act = sheet["members"][0]["next_act"]
    assert act["action"] == "replan"
    assert "orders 1032" in act["detail"]

    discrepant = service.create_batch("op", [
        text_member("mark order 1036 as shipped")])
    tid = discrepant.members[0].task_id
    service.confirm(tid, "s.hale")
    service.execute(tid, "offline", tamper={1: "placed"})
    sheet = service.batch_run_sheet(discrepant)
    act = sheet["members"][0]["next_act"]
    assert act["action"] == "corrective"
    assert "supervisor" in act["who"]


def test_next_act_terminal_wording():
    from page_agent_copilot.models import Task
    task = Task(task_id="T", text="x", operator="op",
                status=TaskStatus.VERIFIED)
    act = next_act_for(task)
    assert act["action"] is None and act["who"] is None
    assert "verified" in act["detail"]


# -- the report ---------------------------------------------------------------

def test_report_reconciles_a_completed_mixed_batch(service):
    batch = service.create_batch("op", [
        text_member(CREDIT),
        template_member("credit-memo", {
            "invoice": "4472", "reason": "short shipment",
            "amount": "75"}),
        text_member("raise a credit memo for invoice 4475, "
                    "reason damaged goods, $200 and mark invoice "
                    "4475 as paid"),
        text_member(OVERCAP),
    ])
    for member in batch.members[:3]:
        done = finish(service, member.task_id)
        assert done.status == TaskStatus.VERIFIED
    report = service.batch_report(batch)
    assert report["complete"] is True
    assert report["counts_by_state"] == {"REFUSED": 1, "VERIFIED": 3}
    assert report["owing_acts"] == []
    assert report["failures"] == []
    assert report["discrepancies_open"] == []
    assert [r["task_id"] for r in report["refused"]] == [
        batch.members[3].task_id]
    # The credited total, recomputed independently from the
    # members' stored verification reports — the amount checks
    # the re-read passed — must equal the report's fold exactly.
    expected = Decimal("0.00")
    for member in batch.members[:3]:
        rep = service.get_verification(member.task_id)
        assert rep is not None and rep.ok
        for check in rep.field_checks:
            if (check.record_kind == "credit_memos"
                    and check.field == "amount"
                    and check.verdict == "match"):
                expected += Decimal(check.actual)
    assert expected == Decimal("395.00")
    assert report["total_credited"] == str(expected)
    assert {c["task_id"] for c in report["credited"]} == {
        m.task_id for m in batch.members[:3]}


def test_report_counts_a_discrepancy_as_no_credit(service):
    batch = service.create_batch("op", [
        text_member(CREDIT),
        text_member("raise a credit memo for invoice 4472, "
                    "reason short shipment, $80"),
    ])
    good, bad = (m.task_id for m in batch.members)
    finish(service, good)
    # The second member's bridge reports success and does
    # nothing: the shell's verdict is DISCREPANCY, and a plan
    # value of $80 contributes exactly nothing to the credited
    # total — only verified amounts are credited.
    service.confirm(bad, "s.hale")
    service.execute(bad, "page-agent-live")
    task = service.report_step(bad, 1, StepReportIn(ok=True))
    assert task.status == TaskStatus.DISCREPANCY
    report = service.batch_report(batch)
    assert report["complete"] is False
    assert report["total_credited"] == "120.00"
    assert [c["task_id"] for c in report["credited"]] == [good]
    assert len(report["discrepancies_open"]) == 1
    entry = report["discrepancies_open"][0]
    assert entry["task_id"] == bad and entry["diffs"]
    assert report["owing_acts"] == [{
        "task_id": bad, "position": 2, "status": "DISCREPANCY",
        "next_act": "corrective"}]
    # The member's own exit closes the batch — still per task.
    service.accept_deviation(bad, "s.hale",
                             "the memo was issued by hand instead")
    report = service.batch_report(batch)
    assert report["complete"] is True
    assert report["total_credited"] == "120.00"


def test_report_names_failures_with_their_steps(service):
    batch = service.create_batch("op", [
        text_member(SHIP_FULL), text_member(CREDIT)])
    failed, fine = (m.task_id for m in batch.members)
    service.confirm(failed, "s.hale")
    service.execute(failed, "offline",
                    fail={2: "element not found: Ship date"})
    finish(service, fine)
    report = service.batch_report(batch)
    assert report["complete"] is False
    assert report["failures"] == [{
        "task_id": failed, "failed_step": 2,
        "failure_error": "element not found: Ship date"}]
    assert report["counts_by_state"] == {
        "EXECUTION_FAILED": 1, "VERIFIED": 1}
    # The failed member's exit is its own: resume it, per task.
    done = service.resume_execution(failed)
    assert done.status == TaskStatus.VERIFIED
    assert service.batch_report(batch)["complete"] is True


# -- member independence ------------------------------------------------------

def test_one_members_staleness_never_blocks_another(service):
    batch = service.create_batch("op", [
        text_member(SHIP),
        text_member("mark order 1036 as packed")])
    stale_id, fine_id = (m.task_id for m in batch.members)
    service.confirm(stale_id, "s.hale")
    service.confirm(fine_id, "s.hale")
    # A record under member A's plan changes; member A's
    # execution refuses into STALE. Member B's lifecycle is
    # untouched by it — B executes and verifies on the same
    # store, in the same batch.
    apply_field_change(service.store, "orders", "1032", "carrier",
                       "FedEx")
    with pytest.raises(GateError):
        service.execute(stale_id, "offline")
    assert service.get_task(stale_id).status == TaskStatus.STALE
    done = service.execute(fine_id, "offline")
    assert done.status == TaskStatus.VERIFIED
    report = service.batch_report(batch)
    assert report["complete"] is False
    assert report["counts_by_state"] == {
        "STALE": 1, "VERIFIED": 1}
    assert [o["task_id"] for o in report["owing_acts"]] == [stale_id]
    assert report["owing_acts"][0]["next_act"] == "replan"
    # A's way back is A's own: re-plan, confirm, execute.
    service.replan(stale_id, "op")
    done = finish(service, stale_id)
    assert done.status == TaskStatus.VERIFIED
    assert service.batch_report(batch)["complete"] is True


def test_members_share_no_plan_state(service):
    # Amending one member's plan says nothing about another's:
    # versions, confirmations, and executions are per task.
    batch = service.create_batch("op", [
        text_member(CREDIT),
        text_member("raise a credit memo for invoice 4472, "
                    "reason short shipment, $80")])
    one, two = (m.task_id for m in batch.members)
    from page_agent_copilot.models import AmendChangeIn
    service.amend(one, "op",
                  [AmendChangeIn(seq=1, fields={"amount": "150.00"})])
    assert service.get_task(one).plan_version == 2
    assert service.get_task(two).plan_version == 1
    done = finish(service, two)
    assert done.status == TaskStatus.VERIFIED
    assert service.get_task(one).status == TaskStatus.PLANNED


# -- the API, and the no-bulk-decisions proof ---------------------------------

@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def test_batch_over_http(client):
    created = client.post("/api/batches", json={
        "operator": "op",
        "members": [{"text": CREDIT}, {"text": OVERCAP},
                    {"template": "credit-memo",
                     "slots": {"invoice": "4472",
                               "reason": "short shipment",
                               "amount": "75"}}]})
    assert created.status_code == 201
    body = created.json()
    bid = body["batch_id"]
    assert body["status"] == "open"
    assert [m["status"] for m in body["members"]] == [
        "PLANNED", "REFUSED", "PLANNED"]
    member_task = body["members"][0]["task_id"]
    detail = client.get(f"/api/tasks/{member_task}").json()
    assert detail["task"]["batch_id"] == bid
    listing = client.get("/api/batches").json()
    assert [b["batch_id"] for b in listing] == [bid]
    sheet = client.get(f"/api/batches/{bid}/run-sheet").json()
    assert sheet["members"][0]["next_act"]["action"] == "confirm"
    report = client.get(f"/api/batches/{bid}/report").json()
    assert report["counts_by_state"] == {
        "PLANNED": 2, "REFUSED": 1}
    assert client.get("/api/batches/BATCH-NOPE").status_code == 404
    assert client.get(
        "/api/batches/BATCH-NOPE/run-sheet").status_code == 404
    assert client.get(
        "/api/batches/BATCH-NOPE/report").status_code == 404
    malformed = client.post("/api/batches", json={
        "operator": "op", "members": []})
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "no_members"
    events = [e["event"] for e in
              client.get("/api/batches-audit").json()]
    assert events == ["batch_created", "batch_refused"]


DECISION_SEGMENTS = {
    "confirm", "execute", "amend", "reject", "replan", "resume",
    "restart", "abandon", "corrective", "accept-deviation",
    "pickup", "verify",
}


def test_no_batch_route_performs_a_decision(client):
    """The round's invariant, asserted against the route table
    the way the diligence workbench asserts its no-bulk-decision
    rule: batch routes are creation plus reads, and every route
    that takes a decision names a task id. One deliberate
    addition since round 9: the audit export
    (`POST /api/batches/{batch_id}/audit/export`) — a read of
    the batch's trail that records itself in the batches scope
    and decides nothing about any member. It is the only batch
    write beside creation, it names no decision segment, and
    the probe below proves it leaves every member untouched.
    One more named exception, since round 13:
    `POST /api/tasks/transcript` sits under /api/tasks/ with no
    task id because it is an intake door, not an act on a task —
    it states a new task from a transcript, exactly as
    `POST /api/tasks` states one from typed text, and decides
    nothing about any task that exists."""
    app = client.app
    batch_routes = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "batch" in path:
            batch_routes.append((path, methods))
        # No route anywhere pairs a batch id with a decision:
        # reads, plus the one self-recording audit export.
        if "{batch_id}" in path:
            assert methods <= {"GET"} or (
                path == "/api/batches/{batch_id}/audit/export"
                and methods == {"POST"}), (
                f"batch route {path} allows {methods}")
            assert not (set(path.split("/")) & DECISION_SEGMENTS), (
                f"batch route {path} names a decision")
        # Every act on an existing task names that task. The
        # transcript door is the one exception: an intake, not
        # an act — it creates a task and decides nothing.
        if path.startswith("/api/tasks/") and "POST" in methods:
            assert "{task_id}" in path \
                or path == "/api/tasks/transcript", (
                    f"task decision route without a task id: {path}")
    # The batch writes are creation — and the audit export,
    # which decides nothing (proven by the probe below).
    posts = [(p, m) for p, m in batch_routes if "POST" in m]
    assert posts == [("/api/batches", {"POST"}),
                     ("/api/batches/{batch_id}/audit/export",
                      {"POST"})], posts
    # And the plausible bulk paths are simply not there.
    created = client.post("/api/batches", json={
        "operator": "op", "members": [{"text": CREDIT}]})
    bid = created.json()["batch_id"]
    for path in (f"/api/batches/{bid}/confirm",
                 f"/api/batches/{bid}/execute",
                 f"/api/batches/{bid}/reject",
                 f"/api/batches/{bid}/run",
                 "/api/batches/confirm-all",
                 "/api/batches/execute-all"):
        response = client.post(path, json={"operator": "s.hale"})
        assert response.status_code in (404, 405), (
            f"{path} answered {response.status_code}")
    # The probes decided nothing: the member is still PLANNED,
    # still awaiting its own per-task confirmation.
    sheet = client.get(f"/api/batches/{bid}/run-sheet").json()
    assert sheet["members"][0]["status"] == "PLANNED"
    # And the one batch write beside creation — the audit
    # export — decides nothing either: it reads the trail and
    # records itself, and the member stands exactly as it did.
    exported = client.post(f"/api/batches/{bid}/audit/export",
                           json={"operator": "s.hale"})
    assert exported.status_code == 200
    assert exported.json()["scope"] == {"kind": "batch", "id": bid}
    sheet = client.get(f"/api/batches/{bid}/run-sheet").json()
    assert sheet["members"][0]["status"] == "PLANNED"
    assert sheet["status"] == "open"


# -- the store round-trip -------------------------------------------------------

def test_batch_round_trips_through_sqlite(tmp_path):
    path = str(tmp_path / "copilot.db")
    store = SqlStore("sqlite", path)
    from page_agent_copilot.service import CopilotService
    svc = CopilotService(Settings.from_env(), store)
    svc.seed()
    batch = svc.create_batch("op", [
        text_member(CREDIT), text_member(OVERCAP)])
    finish(svc, batch.members[0].task_id)
    # A fresh service over the same store reads the batch back:
    # the record persisted, and the folds read the members' live
    # states from the same store.
    svc2 = CopilotService(Settings.from_env(),
                          SqlStore("sqlite", path))
    found = svc2.get_batch(batch.batch_id)
    assert found is not None
    assert [m.task_id for m in found.members] == [
        m.task_id for m in batch.members]
    report = svc2.batch_report(found)
    assert report["complete"] is True
    assert report["total_credited"] == "120.00"
