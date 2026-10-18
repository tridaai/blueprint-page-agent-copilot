"""The task library: governed, reusable task templates.

Rounds 1–6 left the operator typing the governed task grammar
freehand. The shapes the desk asks for are few and they recur —
a credit memo, an order shipped with its details, an invoice
settled — so they belong in a library: a template is a governed
record naming a task-text pattern with named parameter slots, a
note on when to use it, and a version. Like the roles, templates
are data, not code: the defaults below are validated by the same
strict loader that validates every later publish, they live in
the store (kind `templates`), and publishing a new version is a
recorded supervisor act in the service, kept in this module's
own audit scope — old versions stay readable and instantiable,
superseded but never deleted.

A template is a *pattern*, never a plan. Instantiating one fills
its slots, renders the task text, and hands that text to the
ordinary intake: the planner derives the plan from the rendered
text through the same grammar, and policy and the role rules
apply exactly as they do to a typed task. There is no template
door around the planner — a slot value that renders text the
planner cannot derive, or that policy refuses, produces the
planner's own refusal on the task, recorded the usual way.

Each version carries pinned examples: slot values plus the exact
plan shape (task type and steps) the rendered text must derive.
The examples are the template's contract with the planner. The
eval gate runs them, and the library view's health is computed
from them on read — this repo's eval runner stores no results
file, so there is no stored verdict to read back; health is the
examples, re-derived against a seeded scratch store at the
moment of asking. A version whose examples no longer derive to
their pinned shape is marked broken in the library, by name and
version: a planner or policy change that breaks a template says
so where the desk can see it, instead of at the next real task.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from .models import RECORD_KINDS, Plan

# The audit scope template administration is recorded under: the
# audit trail is keyed by task id, and publishing a template is
# not a task, so the library keeps its own reserved scope, the
# way role administration keeps `roles`.
TEMPLATE_AUDIT_SCOPE = "templates"

SLOT_KINDS = ("record", "money", "date", "text")

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SLOT_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


class TemplateError(ValueError):
    """A template, or a set of slot values for one, that does not
    validate (HTTP 422 at the API)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TemplateSlot(BaseModel):
    """One named parameter of a template pattern: `{invoice}` in
    the pattern text. `kind` says how a value for the slot is
    validated at instantiation, before any text is rendered:

    - `record` — the value must name a record that exists, of
      `record_kind` (a template cannot point a plan at a record
      that is not there);
    - `money` — the value must parse as money;
    - `date` — the value must be an ISO calendar date;
    - `text` — the value rides into the rendered text as it
      stands, and the planner governs what it may mean there.
    """

    name: str
    kind: str  # "record" | "money" | "date" | "text"
    record_kind: Optional[str] = None


class TemplateStepExpect(BaseModel):
    """One step of a pinned example's expected plan shape, in the
    planner's own terms: a SET step's record and field and value,
    or a CREATE step's field set."""

    action: str  # "set" | "create"
    record_kind: str
    record_id: Optional[str] = None
    field: Optional[str] = None
    value: Optional[str] = None
    fields: dict[str, str] = Field(default_factory=dict)


class TemplateExample(BaseModel):
    """A pinned instantiation: the slot values, and the plan
    shape — task type and steps — the rendered text must derive.
    If a planner or policy change moves the derivation, the
    example fails and the version is marked broken."""

    slots: dict[str, str]
    task_type: str
    steps: list[TemplateStepExpect]


class TaskTemplate(BaseModel):
    """One version of one template, as stored. `superseded` marks
    a version a newer publish replaced; superseded versions stay
    readable and instantiable by name and version, exactly like
    superseded plan versions."""

    name: str
    version: int = 1
    pattern: str
    description: str = ""  # when to use it
    slots: list[TemplateSlot] = Field(default_factory=list)
    examples: list[TemplateExample] = Field(default_factory=list)
    published_by: str = ""
    published_at: str = ""
    superseded: bool = False

    @property
    def record_id(self) -> str:
        return f"{self.name}:v{self.version}"


def load_template(raw: dict) -> TaskTemplate:
    """The strict loader: a stored or proposed template either
    validates whole or is refused with every problem named. Used
    for the seeded defaults and for every publish, so a template
    the loader would refuse can never reach the store.

    What it holds a template to: the pattern's placeholders are
    exactly the declared slots (a slot the pattern never uses,
    and a placeholder no slot declares, are both refused); a
    record slot names a real record kind; and every pinned
    example fills exactly the declared slots and pins a plan
    shape in the planner's terms."""
    try:
        template = TaskTemplate(**raw)
    except Exception as exc:
        raise TemplateError("template_invalid",
                            f"invalid template: {exc}")
    problems: list[str] = []
    if not _NAME_RE.match(template.name or ""):
        problems.append(
            "the template name must be a slug (lowercase words "
            "joined by hyphens)")
    if template.version < 1:
        problems.append("the version must be 1 or more")
    if not template.pattern.strip():
        problems.append("the pattern is empty")
    if not template.description.strip():
        problems.append(
            "the description is empty; a template says when to "
            "use it")
    declared = [s.name for s in template.slots]
    if len(declared) != len(set(declared)):
        problems.append("a slot name is declared twice")
    for slot in template.slots:
        if not _SLOT_NAME_RE.match(slot.name or ""):
            problems.append(
                f"'{slot.name}' is not a slot name (lowercase, "
                f"underscores)")
        if slot.kind not in SLOT_KINDS:
            problems.append(
                f"slot '{slot.name}' has kind '{slot.kind}', which "
                f"is not one of {', '.join(SLOT_KINDS)}")
        elif slot.kind == "record":
            if slot.record_kind not in RECORD_KINDS:
                problems.append(
                    f"slot '{slot.name}' is a record slot but "
                    f"names '{slot.record_kind}', which is not a "
                    f"record kind")
        elif slot.record_kind is not None:
            problems.append(
                f"slot '{slot.name}' is a {slot.kind} slot and may "
                f"not name a record kind")
    placeholders = _PLACEHOLDER_RE.findall(template.pattern)
    if template.pattern.count("{") != len(placeholders) or \
            template.pattern.count("}") != len(placeholders):
        problems.append(
            "the pattern carries a brace that is not a slot "
            "placeholder")
    for name in placeholders:
        if not _SLOT_NAME_RE.match(name):
            problems.append(
                f"'{{{name}}}' in the pattern is not a slot name")
    if set(placeholders) != set(declared):
        undeclared = sorted(set(placeholders) - set(declared))
        unused = sorted(set(declared) - set(placeholders))
        if undeclared:
            problems.append(
                "the pattern uses slots no slot declares: "
                + ", ".join(undeclared))
        if unused:
            problems.append(
                "slots declared but never used in the pattern: "
                + ", ".join(unused))
    if not template.examples:
        problems.append(
            "the template pins no examples; a version without "
            "examples has no contract to check its health against")
    for i, example in enumerate(template.examples, start=1):
        if set(example.slots) != set(declared):
            problems.append(
                f"example {i} fills {sorted(example.slots)}, not "
                f"exactly the declared slots {sorted(declared)}")
        if not example.task_type.strip():
            problems.append(f"example {i} pins no task type")
        if not example.steps:
            problems.append(f"example {i} pins no steps")
        for j, step in enumerate(example.steps, start=1):
            where = f"example {i} step {j}"
            if step.action not in ("set", "create"):
                problems.append(
                    f"{where} has action '{step.action}', not a "
                    f"plan step action")
            if step.record_kind not in RECORD_KINDS:
                problems.append(
                    f"{where} names '{step.record_kind}', which "
                    f"is not a record kind")
            if step.action == "set" and not (
                    step.record_id and step.field
                    and step.value is not None):
                problems.append(
                    f"{where} is a set step and must pin its "
                    f"record, field, and value")
            if step.action == "create" and not step.fields:
                problems.append(
                    f"{where} is a create step and must pin its "
                    f"field set")
    if problems:
        raise TemplateError(
            "template_invalid",
            "invalid template: " + "; ".join(problems))
    return template


def render_pattern(pattern: str, values: dict[str, str]) -> str:
    """Fill a pattern's slots: every `{name}` becomes its value.
    The values are validated before this is called; the render is
    a plain substitution, nothing interpreted."""
    return _PLACEHOLDER_RE.sub(
        lambda m: values[m.group(1)], pattern)


def plan_shape(plan: Plan) -> tuple[str, list[dict]]:
    """A derived plan in the shape an example pins: its task
    type, and per step the action, record, and the field/value or
    field set — the terms the health check compares in."""
    steps = []
    for step in plan.steps:
        if step.action.value == "create":
            steps.append({
                "action": "create", "record_kind": step.record_kind,
                "record_id": None, "field": None, "value": None,
                "fields": dict(step.fields)})
        else:
            steps.append({
                "action": "set", "record_kind": step.record_kind,
                "record_id": step.record_id, "field": step.field,
                "value": step.value, "fields": {}})
    return plan.task_type, steps


def expected_shape(example: TemplateExample) -> tuple[str, list[dict]]:
    return example.task_type, [
        s.model_dump(mode="json") for s in example.steps]


def shape_detail(plan: Plan, example: TemplateExample) -> Optional[str]:
    """None when the derived plan is the shape the example pins;
    otherwise a sentence naming how it differs — the detail a
    broken marking carries."""
    task_type, steps = plan_shape(plan)
    want_type, want_steps = expected_shape(example)
    if task_type != want_type:
        return (f"the rendered text derives task type "
                f"'{task_type}', not the pinned '{want_type}'")
    if len(steps) != len(want_steps):
        return (f"the rendered text derives {len(steps)} step(s), "
                f"not the pinned {len(want_steps)}")
    for i, (got, want) in enumerate(zip(steps, want_steps), start=1):
        if got != want:
            return (f"step {i} derives as {got}, not the pinned "
                    f"{want}")
    return None


# The default library, as data. Every template here passes
# load_template, and every example pins the shape the planner
# derives from the seeded OrderDesk records — the loader test and
# the health check prove both, so the seed can never drift into a
# shape the publish path would refuse or the planner cannot fill.
DEFAULT_TEMPLATES: list[dict] = [
    {
        "name": "credit-memo",
        "version": 1,
        "pattern": ("raise a credit memo for invoice {invoice}, "
                    "reason {reason}, ${amount}"),
        "description": (
            "A customer is owed money back on an invoice — "
            "damaged goods, a short shipment, a goodwill credit "
            "— up to the governed cap."),
        "slots": [
            {"name": "invoice", "kind": "record",
             "record_kind": "invoices"},
            {"name": "reason", "kind": "text"},
            {"name": "amount", "kind": "money"},
        ],
        "examples": [{
            "slots": {"invoice": "4471", "reason": "damaged goods",
                      "amount": "120"},
            "task_type": "credit_memo",
            "steps": [{
                "action": "create", "record_kind": "credit_memos",
                "fields": {"invoice_id": "4471", "amount": "120.00",
                           "reason": "damaged goods",
                           "status": "issued"},
            }],
        }],
    },
    {
        "name": "credit-memo-and-settle",
        "version": 1,
        "pattern": ("raise a credit memo for invoice {invoice}, "
                    "reason {reason}, ${amount} and mark invoice "
                    "{invoice} as paid"),
        "description": (
            "The credit settles the invoice in full: the memo is "
            "issued and the same invoice is marked paid, in one "
            "plan across two records."),
        "slots": [
            {"name": "invoice", "kind": "record",
             "record_kind": "invoices"},
            {"name": "reason", "kind": "text"},
            {"name": "amount", "kind": "money"},
        ],
        "examples": [{
            "slots": {"invoice": "4471", "reason": "damaged goods",
                      "amount": "120"},
            "task_type": "credit_memo_and_invoice_status",
            "steps": [
                {"action": "create", "record_kind": "credit_memos",
                 "fields": {"invoice_id": "4471", "amount": "120.00",
                            "reason": "damaged goods",
                            "status": "issued"}},
                {"action": "set", "record_kind": "invoices",
                 "record_id": "4471", "field": "status",
                 "value": "paid"},
            ],
        }],
    },
    {
        "name": "ship-order",
        "version": 1,
        "pattern": ("mark order {order} as shipped, ship date "
                    "{ship_date}, carrier {carrier}"),
        "description": (
            "An order leaves the warehouse: its status, ship "
            "date, and carrier are set in one plan, in that "
            "order."),
        "slots": [
            {"name": "order", "kind": "record",
             "record_kind": "orders"},
            {"name": "ship_date", "kind": "date"},
            {"name": "carrier", "kind": "text"},
        ],
        "examples": [{
            "slots": {"order": "1032", "ship_date": "2026-10-20",
                      "carrier": "DHL"},
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
                 "value": "DHL"},
            ],
        }],
    },
    {
        "name": "order-status",
        "version": 1,
        "pattern": "mark order {order} as {status}",
        "description": (
            "A single order status change that carries no "
            "shipment details."),
        "slots": [
            {"name": "order", "kind": "record",
             "record_kind": "orders"},
            {"name": "status", "kind": "text"},
        ],
        "examples": [{
            "slots": {"order": "1036", "status": "packed"},
            "task_type": "order_status",
            "steps": [{
                "action": "set", "record_kind": "orders",
                "record_id": "1036", "field": "status",
                "value": "packed"},
            ],
        }],
    },
    {
        "name": "invoice-status",
        "version": 1,
        "pattern": "mark invoice {invoice} as {status}",
        "description": (
            "A single invoice status change — marking an invoice "
            "paid, void, and so on."),
        "slots": [
            {"name": "invoice", "kind": "record",
             "record_kind": "invoices"},
            {"name": "status", "kind": "text"},
        ],
        "examples": [{
            "slots": {"invoice": "4474", "status": "paid"},
            "task_type": "invoice_status",
            "steps": [{
                "action": "set", "record_kind": "invoices",
                "record_id": "4474", "field": "status",
                "value": "paid"},
            ],
        }],
    },
    {
        "name": "customer-update",
        "version": 1,
        "pattern": "update customer {customer} {field} to {value}",
        "description": (
            "One field on a customer record — phone, email, "
            "address, and the rest of the governed fields. "
            "Payment fields are never available, here or "
            "anywhere."),
        "slots": [
            {"name": "customer", "kind": "record",
             "record_kind": "customers"},
            {"name": "field", "kind": "text"},
            {"name": "value", "kind": "text"},
        ],
        "examples": [{
            "slots": {"customer": "2041", "field": "phone",
                      "value": "555-0134"},
            "task_type": "customer_update",
            "steps": [{
                "action": "set", "record_kind": "customers",
                "record_id": "2041", "field": "phone",
                "value": "555-0134"},
            ],
        }],
    },
]


class TemplatePublishIn(BaseModel):
    """Publish a new version of a template. A recorded supervisor
    act: `operator` is the acting supervisor; the content passes
    the strict loader before anything is stored, and the version
    number is the service's to assign — the next for the name."""

    operator: str
    name: str
    pattern: str
    description: str = ""
    slots: list[TemplateSlot] = Field(default_factory=list)
    examples: list[TemplateExample] = Field(default_factory=list)


class TemplateInstantiateIn(BaseModel):
    """Instantiate a template: fill its slots and state the task.
    `version` may be omitted, in which case the current version
    is used — and the exact version used is named on the task and
    in the audit either way, never silently."""

    operator: str
    version: Optional[int] = None
    slots: dict[str, str] = Field(default_factory=dict)
