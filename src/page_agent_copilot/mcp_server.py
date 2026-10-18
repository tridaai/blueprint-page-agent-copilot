"""The MCP surface: machine clients request TASKS, never clicks.

    page-agent-copilot mcp        # or: python -m page_agent_copilot.mcp_server

PageAgent's own MCP server (beta, in the upstream project) lets
an external client drive the in-page agent directly — click this,
type that. That is precisely the shape this blueprint exists to
govern away, so the shell's surface has the opposite contract,
and it is the surface's founding rule:

    A client may state work and read what the shell made of it.
    It may not confirm a plan, execute one, amend one, delete
    anything, or touch a record. Confirmation and execution are
    human acts, taken in the console or the API, by a named
    second person — no tool here leads to them.

The tools are task-level only: `submit_task`,
`submit_template_task`, `answer_clarification` (an option's
value, never free text), and the reads `get_task`,
`list_templates`, `get_verification`, `get_task_audit`. Every
submission goes through the service's MCP doors
(`submit_task_mcp` and its siblings), which are the ordinary
intake, instantiation, and clarification acts plus one trail
entry naming the channel — so the directory, the role rules, and
policy judge an MCP submission exactly as they judge a typed
one, and the task's audit trail shows where each act came from
(`channel: "mcp"` on the door entries). A plan-time refusal is
not a protocol error: it is a result — the REFUSED task, carrying
the refusal in the shell's own words — because the submission
succeeded and the plan was refused, and a client should be able
to tell those apart. An act the gate refuses (a clarification
answer from a name that may not give one, an unknown task)
returns as a tool error carrying the refusal verbatim, never a
generic failure.

The protocol is the repo's own code, on purpose. The survey for
this round tried the official `mcp` Python SDK first: versions
1.10 through 1.18 all resolve against this repo's frozen
pydantic 2.14.0 pin and then fail at import — the SDK's FastMCP
imports a private pydantic helper (`eval_type_backport`) that
pydantic 2.14 removed, so the SDK in fact needs pydantic < 2.14.
Moving a frozen pin and re-validating the whole shell against a
different pydantic, to serve seven tools, is the tail wagging
the dog; the contract matters more than the library. What is
implemented here is exactly what is claimed, no more:

- transport: stdio, one JSON-RPC 2.0 message per line;
- methods: `initialize`, `ping`, `tools/list`, `tools/call`
  (notifications are accepted and answered with silence);
- protocol versions: `2024-11-05` and `2025-03-26` — the
  message shapes this surface uses are identical in both; the
  server answers with the client's requested version when it is
  one of those, and with `2024-11-05` otherwise;
- tool results: one text content block carrying the JSON
  payload; refusals as `isError` results whose text is the
  shell's refusal, word for word.

No resources, no prompts, no sampling, no roots: a client that
asks for another method is told, by name, that the method is not
here.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

from .config import Settings
from .models import Task
from .service import CopilotService, GateError
from .store import build_store
from .templates import TemplateError

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "page-agent-copilot"
SERVER_VERSION = "0.1.0"  # the package version in pyproject.toml

INSTRUCTIONS = (
    "This surface accepts TASKS for a governed admin copilot: "
    "state the work in words (submit_task, submit_template_task), "
    "answer a plan's clarification with an offered option's value "
    "(answer_clarification), and read what the shell made of it "
    "(get_task, get_verification, get_task_audit). It cannot "
    "confirm, execute, amend, or delete — a human confirms every "
    "plan in the console before anything runs, and verification, "
    "not the agent's word, is the evidence it ran.")

_OBJ = {"type": "object", "additionalProperties": {"type": "string"}}

TOOLS = [
    {
        "name": "submit_task",
        "description": (
            "State a task in words ('raise a credit memo for "
            "invoice 4471, reason damaged goods, $120') for the "
            "named operator. The shell plans it — or refuses it, "
            "or asks a clarification — under the same rules as the "
            "console. Returns the task: its state, its plan "
            "summary, and, when derivation needs one choice made, "
            "the pending question with its options. Nothing is "
            "confirmed or executed by this call."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "the task, in words"},
                "operator": {"type": "string",
                             "description": "the enrolled operator "
                                            "stating the task"},
            },
            "required": ["text", "operator"],
        },
    },
    {
        "name": "get_task",
        "description": (
            "Read one task: its state, its plan summary, its "
            "clarifications, and its ending (the failure "
            "taxonomy's verdict). A read — it changes nothing."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "answer_clarification",
        "description": (
            "Answer a task's pending clarification by picking one "
            "of the offered options, by its value. Free text is "
            "refused with the options restated — the shell never "
            "interprets a reply into a value. The answerer must "
            "be an enrolled, active operator whose role may plan."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "operator": {"type": "string",
                             "description": "the enrolled operator "
                                            "answering"},
                "option": {"type": "string",
                           "description": "the value of one of "
                                          "the offered options"},
            },
            "required": ["task_id", "operator", "option"],
        },
    },
    {
        "name": "list_templates",
        "description": (
            "List the task library: every template version, its "
            "slots, its pinned examples, and its health — the "
            "same read the console serves."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_template_task",
        "description": (
            "State a task from a library template: the name, the "
            "slot values, and the named operator. Slots are "
            "validated before anything renders; the rendered text "
            "goes through the ordinary planner, so a template "
            "cannot smuggle a plan past policy. Returns the task, "
            "as submit_task does."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "operator": {"type": "string",
                             "description": "the enrolled operator "
                                            "stating the task"},
                "slots": _OBJ | {"description": "slot name -> "
                                                "value, all as "
                                                "strings"},
                "version": {"type": ["integer", "null"],
                            "description": "the template version "
                                           "to use; omit for the "
                                           "promoted default — a "
                                           "name with none refuses, "
                                           "naming the state"},
            },
            "required": ["name", "operator", "slots"],
        },
    },
    {
        "name": "get_verification",
        "description": (
            "Read a task's stored verification report — the "
            "field-by-field diff the shell ran after execution — "
            "or null when no verification has run. The report is "
            "the evidence; this read never recomputes it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_audit",
        "description": (
            "Read a task's audit trail: every recorded act, in "
            "order — including the door entries that name the "
            "channel an act arrived by."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOLS]


class _Refusal(Exception):
    """A tool call refused in the shell's own words. Carried back
    as an `isError` tool result, verbatim — never dressed up as a
    generic failure, never swallowed into a success payload."""


class _InvalidParams(Exception):
    """A call that is not shaped like the tool it names: answered
    with JSON-RPC error -32602, naming the tool and the argument."""


def _text_result(payload: dict, is_error: bool = False) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, indent=2)}],
            "isError": is_error}


def _error_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}],
            "isError": True}


class McpSurface:
    """The protocol surface over one service. `handle` is the
    whole contract in one place — a pure function of the message,
    so the suite can drive it in-process; `serve_stdio` is the
    wire loop around it."""

    def __init__(self, service: CopilotService) -> None:
        self.service = service

    # -- JSON-RPC plumbing -------------------------------------------

    def handle(self, message) -> Optional[dict]:
        """One parsed message -> its response, or None for a
        notification (which is answered with silence)."""
        if not isinstance(message, dict) \
                or not isinstance(message.get("method"), str):
            return self._rpc_error(
                message.get("id") if isinstance(message, dict)
                else None,
                -32600, "invalid request: a JSON-RPC message with "
                        "a method is required")
        method = message["method"]
        msg_id = message.get("id")
        params = message.get("params") or {}
        if msg_id is None:  # a notification: never answered
            return None
        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call_tool(params)
            else:
                return self._rpc_error(
                    msg_id, -32601,
                    f"method not found: '{method}' — this surface "
                    f"implements initialize, ping, tools/list, and "
                    f"tools/call, and nothing else")
        except _InvalidParams as exc:
            return self._rpc_error(msg_id, -32602, str(exc))
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _rpc_error(msg_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = (requested if requested in PROTOCOL_VERSIONS
                   else DEFAULT_PROTOCOL_VERSION)
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME,
                           "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    # -- tools ---------------------------------------------------------

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise _InvalidParams(
                "tools/call: 'name' is required and must name a "
                "tool")
        if not isinstance(arguments, dict):
            raise _InvalidParams(
                f"tools/call: 'arguments' for '{name}' must be an "
                f"object")
        handler = {
            "submit_task": self._submit_task,
            "get_task": self._get_task,
            "answer_clarification": self._answer_clarification,
            "list_templates": self._list_templates,
            "submit_template_task": self._submit_template_task,
            "get_verification": self._get_verification,
            "get_task_audit": self._get_task_audit,
        }.get(name)
        if handler is None:
            return _error_result(
                f"unknown tool: '{name}' — this surface offers "
                f"tasks, never raw clicks; its tools are: "
                f"{', '.join(TOOL_NAMES)}")
        try:
            payload = handler(arguments)
        except _Refusal as refusal:
            return _error_result(str(refusal))
        return _text_result(payload)

    @staticmethod
    def _arg(arguments: dict, tool: str, name: str) -> str:
        value = arguments.get(name)
        if not isinstance(value, str) or not value.strip():
            raise _InvalidParams(
                f"{tool}: argument '{name}' is required and must "
                f"be a non-empty string")
        return value

    def _existing_task(self, task_id: str) -> Task:
        task = self.service.get_task(task_id)
        if task is None:
            # the service's own words for a task that is not there
            raise _Refusal(f"task {task_id} does not exist")
        return task

    def _task_payload(self, task: Task) -> dict:
        """A task as a machine client reads it: the record with
        its ending (the API's own read shape), the current plan
        summarised, and the pending clarification — question and
        options — lifted out where a client cannot miss it."""
        view = self.service.task_view(task)
        plan = (self.service.get_plan(task.task_id)
                if task.plan_version is not None else None)
        view["plan"] = None if plan is None else {
            "version": plan.version, "task_type": plan.task_type,
            "summary": plan.summary, "steps": len(plan.steps),
            "pages": plan.pages, "multi_page": plan.is_multi_page}
        pending = task.pending_clarification
        view["pending_clarification"] = None if pending is None \
            else {
                "key": pending.key, "code": pending.code,
                "question": pending.question,
                "options": [{"value": o.value, "label": o.label}
                            for o in pending.options]}
        return view

    def _submit_task(self, arguments: dict) -> dict:
        text = self._arg(arguments, "submit_task", "text")
        operator = self._arg(arguments, "submit_task", "operator")
        task = self.service.submit_task_mcp(text, operator)
        return self._task_payload(task)

    def _get_task(self, arguments: dict) -> dict:
        task_id = self._arg(arguments, "get_task", "task_id")
        return self._task_payload(self._existing_task(task_id))

    def _answer_clarification(self, arguments: dict) -> dict:
        task_id = self._arg(arguments, "answer_clarification",
                            "task_id")
        operator = self._arg(arguments, "answer_clarification",
                             "operator")
        option = self._arg(arguments, "answer_clarification",
                           "option")
        try:
            task = self.service.answer_clarification_mcp(
                task_id, operator, option)
        except GateError as exc:
            raise _Refusal(str(exc)) from exc
        return self._task_payload(task)

    def _list_templates(self, arguments: dict) -> dict:
        return {"templates": self.service.list_template_views()}

    def _submit_template_task(self, arguments: dict) -> dict:
        name = self._arg(arguments, "submit_template_task", "name")
        operator = self._arg(arguments, "submit_template_task",
                             "operator")
        slots = arguments.get("slots")
        if not isinstance(slots, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in slots.items()):
            raise _InvalidParams(
                "submit_template_task: argument 'slots' is "
                "required and must be an object of slot name -> "
                "string value")
        version = arguments.get("version")
        if version is not None and not isinstance(version, int):
            raise _InvalidParams(
                "submit_template_task: argument 'version' must "
                "be an integer or null")
        try:
            task = self.service.instantiate_template_mcp(
                name, version, slots, operator)
        except TemplateError as exc:
            # the library's refusal in its standing format:
            # the code, then the reason, word for word
            raise _Refusal(f"{exc.code}: {exc.message}") from exc
        return self._task_payload(task)

    def _get_verification(self, arguments: dict) -> dict:
        task_id = self._arg(arguments, "get_verification",
                            "task_id")
        task = self._existing_task(task_id)
        report = self.service.get_verification(task_id)
        return {
            "task_id": task_id, "status": task.status.value,
            "report": None if report is None
            else report.model_dump(mode="json")}

    def _get_task_audit(self, arguments: dict) -> dict:
        task_id = self._arg(arguments, "get_task_audit", "task_id")
        self._existing_task(task_id)
        return {"task_id": task_id,
                "entries": self.service.audit(task_id)}


def serve_stdio(service: CopilotService, instream=None,
                outstream=None) -> None:
    """The wire loop: one JSON-RPC message per line in, one
    response line per answered message out. A line that is not
    JSON is answered with a -32700 parse error and the loop goes
    on — a malformed line from a client must not kill the surface
    every other client is using."""
    surface = McpSurface(service)
    instream = instream if instream is not None else sys.stdin
    outstream = outstream if outstream is not None else sys.stdout
    for line in instream:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700,
                                  "message": "parse error: the "
                                             "line is not JSON"}}
            outstream.write(json.dumps(response) + "\n")
            outstream.flush()
            continue
        response = surface.handle(message)
        if response is not None:
            outstream.write(json.dumps(response) + "\n")
            outstream.flush()


def main() -> None:
    """Serve the surface over stdio on the configured store —
    the same settings, the same seed, the same service the
    console serves. Nothing is printed to stdout: on this
    transport, stdout is the protocol."""
    settings = Settings.from_env()
    service = CopilotService(settings, build_store(settings))
    service.seed()
    serve_stdio(service)


if __name__ == "__main__":
    main()
