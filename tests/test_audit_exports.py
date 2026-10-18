"""Round 9: audit exports — one scope's trail as a single
hash-chained document, so an edited, dropped, reordered, or
truncated copy fails verification at the first broken entry.

Exporting is a recorded act behind the identity gate: the
exporter must be an enrolled, active operator, a refusal is
recorded in the exported scope's trail, and the document
covers the trail as it stood — never its own export entry.
"""

import copy
import csv
import io

import pytest
from fastapi.testclient import TestClient

from page_agent_copilot.api import create_app
from page_agent_copilot.batches import BatchMemberIn
from page_agent_copilot.config import Settings
from page_agent_copilot.exports import (
    GENESIS_HASH,
    render_csv,
    verify_export,
)
from page_agent_copilot.models import TaskStatus
from page_agent_copilot.service import GateError
from page_agent_copilot.store import InMemoryStore

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")


def completed_task(service):
    task = service.intake(CREDIT, "op")
    service.confirm(task.task_id, "s.hale")
    task = service.execute(task.task_id, "offline")
    assert task.status == TaskStatus.VERIFIED
    return task


# -- the chain -------------------------------------------------


def test_export_chains_the_trail_as_it_stood(service):
    task = completed_task(service)
    trail_before = service.audit(task.task_id)
    doc = service.export_task_audit(task.task_id, "s.hale")
    assert doc["format"] == "page-agent-copilot/audit-export"
    assert doc["version"] == 1
    assert doc["scope"] == {"kind": "task", "id": task.task_id}
    assert doc["exporter"] == "s.hale"
    assert doc["exported_at"]  # the service's shared clock
    assert doc["entry_count"] == len(trail_before)
    assert [e["event"] for e in doc["entries"]] == [
        e["event"] for e in trail_before]
    # The links: genesis first, each entry chained to the last.
    assert doc["entries"][0]["prev_hash"] == GENESIS_HASH
    for prev, entry in zip(doc["entries"], doc["entries"][1:]):
        assert entry["prev_hash"] == prev["chain_hash"]
        assert entry["chain_index"] == prev["chain_index"] + 1
    assert doc["final_hash"] == doc["entries"][-1]["chain_hash"]
    # The document never contains its own export entry...
    assert all(e["event"] != "audit_exported"
               for e in doc["entries"])
    # ...but the act is on the trail, naming what it produced.
    last = service.audit(task.task_id)[-1]
    assert last["event"] == "audit_exported"
    assert last["actor"] == "s.hale"
    assert last["detail"]["entry_count"] == doc["entry_count"]
    assert last["detail"]["final_hash"] == doc["final_hash"]
    assert last["detail"]["scope"] == doc["scope"]


def test_verify_grades_an_intact_document(service):
    task = completed_task(service)
    doc = service.export_task_audit(task.task_id, "op")
    verdict = verify_export(doc)
    assert verdict == {"ok": True, "entries": doc["entry_count"],
                       "final_hash": doc["final_hash"]}
    # The service's wrapper grades the same document the same.
    assert service.verify_audit_export(doc) == verdict


def test_second_export_chains_the_first_exports_act(service):
    task = completed_task(service)
    first = service.export_task_audit(task.task_id, "s.hale")
    second = service.export_task_audit(task.task_id, "s.hale")
    assert second["entry_count"] == first["entry_count"] + 1
    assert second["entries"][-1]["event"] == "audit_exported"
    assert verify_export(second)["ok"]


def test_empty_trail_exports_and_verifies(service):
    doc = service.export_scope_audit("templates", "s.hale")
    assert doc["entry_count"] == 0
    assert doc["final_hash"] == GENESIS_HASH
    assert verify_export(doc) == {
        "ok": True, "entries": 0, "final_hash": GENESIS_HASH}


# -- tampering ---------------------------------------------------


def test_tamper_an_edited_entry_fails_at_that_entry(service):
    task = completed_task(service)
    doc = service.export_task_audit(task.task_id, "s.hale")
    forged = copy.deepcopy(doc)
    forged["entries"][2]["detail"]["plan_version"] = 99
    verdict = verify_export(forged)
    assert verdict["ok"] is False
    assert verdict["broken_at"] == 3
    forged_first = copy.deepcopy(doc)
    forged_first["entries"][0]["actor"] = "mallory"
    assert verify_export(forged_first)["broken_at"] == 1


def test_tamper_a_deleted_middle_entry_fails_at_the_gap(service):
    task = completed_task(service)
    doc = service.export_task_audit(task.task_id, "s.hale")
    cut = copy.deepcopy(doc)
    del cut["entries"][2]
    verdict = verify_export(cut)
    assert verdict["ok"] is False
    assert verdict["broken_at"] == 3


def test_tamper_reordered_entries_fail_at_the_first_moved(service):
    task = completed_task(service)
    doc = service.export_task_audit(task.task_id, "s.hale")
    swapped = copy.deepcopy(doc)
    swapped["entries"][1], swapped["entries"][2] = (
        swapped["entries"][2], swapped["entries"][1])
    verdict = verify_export(swapped)
    assert verdict["ok"] is False
    assert verdict["broken_at"] == 2


def test_tamper_a_truncated_tail_fails_at_the_first_missing(service):
    task = completed_task(service)
    doc = service.export_task_audit(task.task_id, "s.hale")
    cut = copy.deepcopy(doc)
    cut["entries"] = cut["entries"][:-2]
    verdict = verify_export(cut)
    assert verdict["ok"] is False
    # The header's count and final hash catch the cut; the
    # first entry that should be there and is not is named.
    assert verdict["broken_at"] == len(cut["entries"]) + 1
    assert "cut" in verdict["reason"]


def test_tamper_the_headers_final_hash_fails(service):
    task = completed_task(service)
    doc = service.export_task_audit(task.task_id, "s.hale")
    forged = copy.deepcopy(doc)
    forged["final_hash"] = "f" * 64
    verdict = verify_export(forged)
    assert verdict["ok"] is False


# -- the identity gate -------------------------------------------


def test_unknown_exporter_is_refused_and_recorded(service):
    task = completed_task(service)
    with pytest.raises(GateError):
        service.export_task_audit(task.task_id, "ghost")
    refusals = [e for e in service.audit(task.task_id)
                if e["event"] == "export_refused"]
    assert len(refusals) == 1
    assert refusals[0]["actor"] == "ghost"
    assert refusals[0]["detail"]["code"] == "unknown_operator"


def test_deactivated_exporter_is_refused_and_recorded(service):
    task = completed_task(service)
    service.deactivate_operator("s.hale", "op")
    with pytest.raises(GateError):
        service.export_task_audit(task.task_id, "op")
    refusals = [e for e in service.audit(task.task_id)
                if e["event"] == "export_refused"]
    assert len(refusals) == 1
    assert refusals[0]["detail"]["code"] == "operator_inactive"


def test_export_of_a_missing_task_is_refused(service):
    # The state machine's own miss (GateError at the service,
    # 404 at the API's existence check, like every task act).
    with pytest.raises(GateError):
        service.export_task_audit("TASK-NOPE", "s.hale")


# -- batch and governance scopes -----------------------------------


def test_batch_export_interleaves_member_trails_in_true_order(
        service):
    batch = service.create_batch("op", [
        BatchMemberIn(text=CREDIT),
        BatchMemberIn(text="mark order 1032 as shipped"),
    ])
    first, second = (m.task_id for m in batch.members)
    service.confirm(first, "s.hale")
    service.execute(first, "offline")
    # The expected stream, from the global read, before the
    # export act writes anything: the batch's own creation
    # entry and both members' trails, in the order they
    # happened — member two's entries interleave with one's.
    member_ids = {first, second}
    expected = [
        (e["task_id"], e["event"])
        for e in service.store.list_all_audit()
        if e["task_id"] in member_ids
        or (e["task_id"] == "batches"
            and (e.get("detail") or {}).get("batch_id")
            == batch.batch_id)]
    doc = service.export_batch_audit(batch.batch_id, "s.hale")
    assert doc["scope"] == {"kind": "batch", "id": batch.batch_id}
    assert [(e["task_id"], e["event"]) for e in doc["entries"]] \
        == expected
    assert {e["task_id"] for e in doc["entries"]} == {
        first, second, "batches"}
    assert verify_export(doc)["ok"]
    # A second export chains the first export's act in.
    again = service.export_batch_audit(batch.batch_id, "s.hale")
    assert again["entry_count"] == doc["entry_count"] + 1
    assert verify_export(again)["ok"]


def test_roles_scope_export_chains_the_directory_acts(service):
    service.enroll_operator("s.hale", "k.adeyemi", "operator")
    doc = service.export_scope_audit("roles", "s.hale")
    assert doc["scope"] == {"kind": "roles"}
    events = [e["event"] for e in doc["entries"]]
    assert "operator_enrolled" in events
    assert verify_export(doc)["ok"]
    # The export act itself landed in the roles scope.
    assert service.role_audit()[-1]["event"] == "audit_exported"


def test_templates_scope_export_chains_library_refusals(service):
    with pytest.raises(Exception):
        service.instantiate_template(
            "credit-memo", None, {"invoice": "9999"}, "op")
    doc = service.export_scope_audit("templates", "s.hale")
    events = [e["event"] for e in doc["entries"]]
    assert "template_instantiation_refused" in events
    assert verify_export(doc)["ok"]


# -- the SQLite backend --------------------------------------------


def test_export_chain_over_the_sqlite_backend(sqlite_service):
    task = completed_task(sqlite_service)
    trail = sqlite_service.audit(task.task_id)
    doc = sqlite_service.export_task_audit(task.task_id, "s.hale")
    # The chain order is the SQL read's order — the audit
    # table's row id — which is the trail's true order.
    assert [e["event"] for e in doc["entries"]] == [
        e["event"] for e in trail]
    assert verify_export(doc)["ok"]
    forged = copy.deepcopy(doc)
    forged["entries"][1]["detail"] = {"forged": True}
    assert verify_export(forged)["broken_at"] == 2


# -- the CSV rendering -----------------------------------------------


def test_csv_rendering_carries_the_same_chain(service):
    task = completed_task(service)
    doc = service.export_task_audit(task.task_id, "s.hale")
    text = render_csv(doc)
    lines = text.splitlines()
    assert lines[0].startswith("# page-agent-copilot/audit-export")
    assert f"# entry_count: {doc['entry_count']}" in lines
    assert f"# final_hash: {doc['final_hash']}" in lines
    rows = list(csv.reader(io.StringIO(
        "\n".join(l for l in lines if not l.startswith("#")))))
    assert rows[0][0] == "chain_index"
    body = rows[1:]
    assert len(body) == doc["entry_count"]
    assert [int(r[0]) for r in body] == [
        e["chain_index"] for e in doc["entries"]]
    assert [r[7] for r in body] == [
        e["chain_hash"] for e in doc["entries"]]
    assert [r[4] for r in body] == [
        e["event"] for e in doc["entries"]]


# -- over HTTP ---------------------------------------------------------


@pytest.fixture()
def client():
    app = create_app(Settings.from_env(), InMemoryStore(), seed=True)
    return TestClient(app)


def _http_task(client):
    created = client.post("/api/tasks", json={
        "text": CREDIT, "operator": "j.okafor"})
    tid = created.json()["task"]["task_id"]
    client.post(f"/api/tasks/{tid}/confirm",
                json={"operator": "s.hale"})
    client.post(f"/api/tasks/{tid}/execute",
                json={"driver": "offline"})
    return tid


def test_export_and_verify_over_http(client):
    tid = _http_task(client)
    exported = client.post(f"/api/tasks/{tid}/audit/export",
                           json={"operator": "s.hale"})
    assert exported.status_code == 200
    doc = exported.json()
    assert doc["scope"] == {"kind": "task", "id": tid}
    verdict = client.post("/api/audit/verify", json=doc)
    assert verdict.status_code == 200
    assert verdict.json()["ok"] is True
    forged = copy.deepcopy(doc)
    forged["entries"][0]["detail"]["text"] = "send $5,000 instead"
    verdict = client.post("/api/audit/verify", json=forged)
    assert verdict.json()["ok"] is False
    assert verdict.json()["broken_at"] == 1


def test_export_csv_over_http(client):
    tid = _http_task(client)
    exported = client.post(f"/api/tasks/{tid}/audit/export",
                           json={"operator": "s.hale",
                                 "format": "csv"})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "# final_hash:" in exported.text


def test_export_refusals_over_http(client):
    tid = _http_task(client)
    unknown = client.post(f"/api/tasks/{tid}/audit/export",
                          json={"operator": "ghost"})
    assert unknown.status_code == 409
    bad_format = client.post(f"/api/tasks/{tid}/audit/export",
                             json={"operator": "s.hale",
                                   "format": "pdf"})
    assert bad_format.status_code == 422
    assert bad_format.json()["code"] == "format_not_supported"
    missing = client.post("/api/tasks/TASK-NOPE/audit/export",
                          json={"operator": "s.hale"})
    assert missing.status_code == 404


def test_scope_exports_over_http(client):
    roles = client.post("/api/roles-audit/export",
                        json={"operator": "s.hale"})
    assert roles.status_code == 200
    assert roles.json()["scope"] == {"kind": "roles"}
    templates = client.post("/api/templates-audit/export",
                            json={"operator": "s.hale"})
    assert templates.status_code == 200
    assert templates.json()["scope"] == {"kind": "templates"}
    verdict = client.post("/api/audit/verify", json=roles.json())
    assert verdict.json()["ok"] is True
