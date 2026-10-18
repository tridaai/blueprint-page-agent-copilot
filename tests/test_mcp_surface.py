"""Round 17 — the MCP surface: clients request TASKS, never
raw clicks.

The surface is driven the way a client drives it: JSON-RPC
messages in, responses out — in-process through `McpSurface`
for the contract, and once over a real stdio subprocess for the
wire. The founding rule is graded, not just stated: the tool
list is exactly the task-level tools, a click-shaped call is
refused by name, and confirmation + execution happen only off
the surface, in human hands, between two MCP reads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from page_agent_copilot.mcp_server import TOOL_NAMES, McpSurface

CREDIT = ("raise a credit memo for invoice 4471, reason damaged "
          "goods, $120")
AMBIGUOUS = ("raise a credit memo for the open invoice, reason "
             "damaged goods, $120")
OPERATOR = "j.okafor"     # role: operator (may plan)
SUPERVISOR = "s.hale"     # role: supervisor (may confirm others)
VIEWER = "v.ward"         # role: viewer (may not plan)


@pytest.fixture()
def surface(service) -> McpSurface:
    return McpSurface(service)


def rpc(surface: McpSurface, method: str, params=None, msg_id=1):
    return surface.handle({"jsonrpc": "2.0", "id": msg_id,
                           "method": method,
                           "params": params or {}})


def tool(surface: McpSurface, name: str, arguments=None):
    """A tools/call, returning (payload, raw result): payload is
    the parsed JSON of the text content on success, or the raw
    text on an error result."""
    response = rpc(surface, "tools/call",
                   {"name": name, "arguments": arguments or {}})
    result = response["result"]
    text = result["content"][0]["text"]
    if result["isError"]:
        return text, result
    return json.loads(text), result


# -- the handshake ---------------------------------------------------


def test_initialize_answers_the_versions_it_claims(surface):
    for requested in ("2024-11-05", "2025-03-26"):
        result = rpc(surface, "initialize",
                     {"protocolVersion": requested})["result"]
        assert result["protocolVersion"] == requested
        assert result["serverInfo"]["name"] == "page-agent-copilot"
        assert result["capabilities"] == {"tools": {}}
        assert "cannot" in result["instructions"]
    result = rpc(surface, "initialize",
                 {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] == "2024-11-05"


def test_ping_and_notifications(surface):
    assert rpc(surface, "ping")["result"] == {}
    assert surface.handle({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}) \
        is None
    assert surface.handle({"jsonrpc": "2.0",
                           "method": "notifications/cancelled",
                           "params": {"requestId": 1}}) is None


def test_the_tool_list_is_tasks_only(surface):
    result = rpc(surface, "tools/list")["result"]
    names = [t["name"] for t in result["tools"]]
    assert names == TOOL_NAMES == [
        "submit_task", "get_task", "answer_clarification",
        "list_templates", "submit_template_task",
        "get_verification", "get_task_audit"]
    for name in names:
        for verb in ("click", "execute", "confirm", "amend",
                     "delete", "type", "navigate"):
            assert verb not in name


def test_unknown_method_is_refused_by_name(surface):
    response = rpc(surface, "resources/list")
    assert response["error"]["code"] == -32601
    assert "resources/list" in response["error"]["message"]


def test_a_malformed_message_is_a_protocol_error(surface):
    response = surface.handle({"jsonrpc": "2.0", "id": 9})
    assert response["error"]["code"] == -32600
    response = surface.handle("not a message")
    assert response["error"]["code"] == -32600


def test_missing_arguments_are_named(surface):
    response = rpc(surface, "tools/call",
                   {"name": "submit_task",
                    "arguments": {"text": CREDIT}})
    assert response["error"]["code"] == -32602
    assert "'operator'" in response["error"]["message"]


# -- submission: the ordinary intake, through the door ---------------


def test_submit_task_plans_and_marks_the_trail(surface, service):
    payload, _ = tool(surface, "submit_task",
                      {"text": CREDIT, "operator": OPERATOR})
    assert payload["status"] == "PLANNED"
    assert payload["plan"]["task_type"] == "credit_memo"
    assert payload["plan"]["steps"] >= 1
    assert payload["ending"]["label"] == "in_progress"
    trail = service.audit(payload["task_id"])
    doors = [e for e in trail if e["event"] == "mcp_task_submitted"]
    assert len(doors) == 1
    assert doors[0]["actor"] == OPERATOR
    assert doors[0]["detail"]["channel"] == "mcp"
    assert doors[0]["detail"]["tool"] == "submit_task"


def test_a_viewer_is_refused_in_policy_own_words(surface):
    payload, result = tool(surface, "submit_task",
                           {"text": CREDIT, "operator": VIEWER})
    assert not result["isError"]  # a refusal is a result here
    assert payload["status"] == "REFUSED"
    assert payload["refusal_reason"].startswith("role_cannot_plan:")
    assert "may not plan work" in payload["refusal_reason"]
    assert payload["ending"]["label"] == "refused_at_plan"
    assert payload["ending"]["detail"]["code"] == "role_cannot_plan"


def test_an_unknown_operator_is_refused_by_name(surface):
    payload, _ = tool(surface, "submit_task",
                      {"text": CREDIT, "operator": "n.nobody"})
    assert payload["status"] == "REFUSED"
    assert payload["refusal_reason"].startswith("unknown_operator:")


def test_a_deactivated_operator_is_refused_identically(surface,
                                                        service):
    service.deactivate_operator(SUPERVISOR, OPERATOR)
    payload, _ = tool(surface, "submit_task",
                      {"text": CREDIT, "operator": OPERATOR})
    assert payload["status"] == "REFUSED"
    assert payload["refusal_reason"].startswith("operator_inactive:")


def test_over_cap_credit_refuses_over_mcp_too(surface):
    payload, _ = tool(surface, "submit_task", {
        "text": "raise a credit memo for invoice 4471, reason "
                "goodwill, $900",
        "operator": OPERATOR})
    assert payload["status"] == "REFUSED"
    assert payload["refusal_reason"].startswith("credit_above_cap:")


# -- clarification over the surface -----------------------------------


def test_clarification_round_trip(surface, service):
    payload, _ = tool(surface, "submit_task",
                      {"text": AMBIGUOUS, "operator": OPERATOR})
    assert payload["status"] == "AWAITING_CLARIFICATION"
    pending = payload["pending_clarification"]
    assert pending is not None
    assert len(pending["options"]) >= 2
    task_id = payload["task_id"]

    # free text is not an option: refused, verbatim, still waiting
    text, result = tool(surface, "answer_clarification", {
        "task_id": task_id, "operator": OPERATOR,
        "option": "the big one"})
    assert result["isError"]
    assert "is not one of the options" in text
    after, _ = tool(surface, "get_task", {"task_id": task_id})
    assert after["status"] == "AWAITING_CLARIFICATION"

    # a name that may not plan may not answer
    text, result = tool(surface, "answer_clarification", {
        "task_id": task_id, "operator": VIEWER,
        "option": pending["options"][0]["value"]})
    assert result["isError"]
    assert "may not plan work" in text

    # the offered option, by value, plans the task
    payload, _ = tool(surface, "answer_clarification", {
        "task_id": task_id, "operator": OPERATOR,
        "option": pending["options"][0]["value"]})
    assert payload["status"] == "PLANNED"
    assert payload["pending_clarification"] is None
    events = [e["event"] for e in service.audit(task_id)]
    assert "mcp_clarification_answered" in events


def test_answering_a_task_that_is_not_waiting_is_refused(surface):
    payload, _ = tool(surface, "submit_task",
                      {"text": CREDIT, "operator": OPERATOR})
    text, result = tool(surface, "answer_clarification", {
        "task_id": payload["task_id"], "operator": OPERATOR,
        "option": "4471"})
    assert result["isError"]
    assert "only a task awaiting clarification" in text


# -- the founding rule: no clicks, no confirmation, no execution ------


def test_a_raw_click_request_is_refused_by_name(surface):
    text, result = tool(surface, "click_element",
                        {"selector": "#save-button"})
    assert result["isError"]
    assert "click_element" in text
    assert "never raw clicks" in text


def test_there_is_no_execute_or_confirm_tool_to_call(surface):
    for name in ("execute_task", "confirm_task", "run_plan"):
        text, result = tool(surface, name, {"task_id": "TASK-X"})
        assert result["isError"]
        assert name in text


def test_the_human_split_submit_here_confirm_elsewhere(surface,
                                                        service):
    payload, _ = tool(surface, "submit_task",
                      {"text": CREDIT, "operator": OPERATOR})
    task_id = payload["task_id"]

    # nothing to verify yet, and the surface says so plainly
    payload, _ = tool(surface, "get_verification",
                      {"task_id": task_id})
    assert payload["report"] is None
    assert payload["status"] == "PLANNED"

    # the human acts — off the surface, through the service the
    # console uses: a second person confirms, the plan executes
    service.confirm(task_id, SUPERVISOR)
    service.execute(task_id, operator=OPERATOR)

    # and the evidence reads back over the surface
    payload, _ = tool(surface, "get_verification",
                      {"task_id": task_id})
    assert payload["status"] == "VERIFIED"
    assert payload["report"]["ok"] is True
    payload, _ = tool(surface, "get_task", {"task_id": task_id})
    assert payload["ending"]["label"] == "verified"


# -- templates over the surface ----------------------------------------


def test_templates_list_and_submit(surface, service):
    payload, _ = tool(surface, "list_templates")
    names = {t["name"] for t in payload["templates"]}
    assert "credit-memo" in names

    payload, _ = tool(surface, "submit_template_task", {
        "name": "credit-memo", "operator": OPERATOR,
        "slots": {"invoice": "4471", "reason": "damaged goods",
                  "amount": "120"}})
    assert payload["status"] == "PLANNED"
    assert payload["template_name"] == "credit-memo"
    assert payload["template_version"] == 1
    events = [e["event"] for e in service.audit(payload["task_id"])]
    assert "mcp_template_submitted" in events


def test_template_slot_refusals_carry_the_code(surface):
    text, result = tool(surface, "submit_template_task", {
        "name": "credit-memo", "operator": OPERATOR,
        "slots": {"invoice": "4471", "reason": "damaged goods"}})
    assert result["isError"]
    assert text.startswith("slot_missing:")

    text, result = tool(surface, "submit_template_task", {
        "name": "credit-memo", "operator": OPERATOR,
        "slots": {"invoice": "9999", "reason": "damaged goods",
                  "amount": "120"}})
    assert result["isError"]
    assert text.startswith("record_not_found:")

    text, result = tool(surface, "submit_template_task", {
        "name": "no-such-template", "operator": OPERATOR,
        "slots": {}})
    assert result["isError"]
    assert text.startswith("template_not_found:")


# -- reads of things that are not there ---------------------------------


def test_reads_of_an_unknown_task_are_refused_in_service_words(
        surface):
    for name in ("get_task", "get_verification", "get_task_audit"):
        text, result = tool(surface, name,
                            {"task_id": "TASK-NOPE"})
        assert result["isError"]
        assert text == "task TASK-NOPE does not exist"


def test_get_task_audit_shows_the_whole_trail(surface):
    payload, _ = tool(surface, "submit_task",
                      {"text": CREDIT, "operator": OPERATOR})
    payload, _ = tool(surface, "get_task_audit",
                      {"task_id": payload["task_id"]})
    events = [e["event"] for e in payload["entries"]]
    assert events[0] == "task_created"
    assert "plan_created" in events
    assert "mcp_task_submitted" in events


# -- the wire: one real stdio session ------------------------------------


def test_stdio_wire_round_trip(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["SQLITE_PATH"] = str(tmp_path / "mcp-wire.db")
    env["PYTHONPATH"] = str(repo / "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "page_agent_copilot.mcp_server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, text=True)
    try:
        def send(message):
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if "id" not in message:
                return None
            while True:
                line = proc.stdout.readline()
                assert line, "the server closed the wire early"
                response = json.loads(line)
                if response.get("id") == message["id"]:
                    return response

        result = send({"jsonrpc": "2.0", "id": 1,
                       "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26",
                                  "capabilities": {},
                                  "clientInfo": {"name": "pytest",
                                                 "version": "0"}}})
        assert result["result"]["serverInfo"]["name"] == \
            "page-agent-copilot"
        send({"jsonrpc": "2.0",
              "method": "notifications/initialized"})
        result = send({"jsonrpc": "2.0", "id": 2,
                       "method": "tools/call",
                       "params": {"name": "submit_task",
                                  "arguments": {
                                      "text": CREDIT,
                                      "operator": OPERATOR}}})
        payload = json.loads(
            result["result"]["content"][0]["text"])
        assert payload["status"] == "PLANNED"
        result = send({"jsonrpc": "2.0", "id": 3,
                       "method": "tools/call",
                       "params": {"name": "get_task_audit",
                                  "arguments": {
                                      "task_id": payload["task_id"]}}})
        audit = json.loads(result["result"]["content"][0]["text"])
        assert "mcp_task_submitted" in [
            e["event"] for e in audit["entries"]]
        # a line that is not JSON is answered, not fatal
        proc.stdin.write("this is not json\n")
        proc.stdin.flush()
        response = json.loads(proc.stdout.readline())
        assert response["error"]["code"] == -32700
    finally:
        proc.terminate()
        proc.wait(timeout=10)
