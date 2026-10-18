"""Roles: who may do what, held as governed data.

Until this round the shell knew operator *names* and nothing else:
any name could plan, confirm, amend, or execute — including the
planner confirming their own plan. Roles close that. A role is a
record: the console pages it may work on, the record fields its
plans may touch, and the action classes open to it (plan, confirm,
amend, execute, administer). The defaults below are data, loaded
through the same strict loader that validates every later edit;
the service reads the *stored* definitions on every act, so an
edit takes effect on the next act — including an execution that
was confirmed under the old definition.

The discipline the defaults encode:

- **viewer** — read-only. No fields, no action classes: a viewer
  can look at every screen and plan, confirm, amend, and execute
  nothing.
- **operator** — the desk's working role: plans, amends, executes,
  and may confirm — but never a plan they planned themselves
  (see `service.confirm`: self-confirmation is refused for every
  role; the confirmer is always a second person).
- **supervisor** — everything the operator has, plus the fields
  held back from operators (a customer's `credit_limit`), plus
  administering the role definitions themselves.

Roles and the operator directory (which name holds which role)
live in the store as records — kinds `roles` and `operators` —
seeded from the defaults here when absent, and changed only
through `service.update_role`, which validates the new definition
with the strict loader and writes the change to the audit trail.

The directory itself is governed the same way (round 6): names
are enrolled, assigned a role, deactivated, and reactivated only
through recorded supervisor acts in the service, and a
deactivated name may perform no act at all — deactivation is a
state on the record, never a deletion, so every act the name ever
took stays attributable to it. Names resolve normalised
(`operator_named`) at enrolment and at act time alike: a spacing
or casing variant of a directory name is that name.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .models import PLANNABLE_FIELDS, RECORD_KINDS, Plan, StepAction

# The console screens a plan step can name (planner.PAGE_FOR_KIND).
KNOWN_PAGES = frozenset({"Orders", "Customers", "Invoices"})

# The fields of a CREATE step, per record kind. A credit memo is
# created whole, so PLANNABLE_FIELDS lists no editable fields for
# it; creation is governed by these field names instead.
CREATE_FIELDS: dict[str, frozenset[str]] = {
    "credit_memos": frozenset({"invoice_id", "amount", "reason",
                               "status"}),
}

# The audit scope role changes are recorded under: the audit trail
# is keyed by task id, and role administration is not a task, so it
# keeps its own reserved scope.
ROLE_AUDIT_SCOPE = "roles"


class RoleError(ValueError):
    """A role definition that does not validate (HTTP 422)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RoleDefinition(BaseModel):
    """One role, as stored. `fields` maps a record kind to the
    fields a plan by this role may touch on that kind; a kind
    absent from the map may not be touched at all."""

    name: str
    pages: list[str] = Field(default_factory=list)
    fields: dict[str, list[str]] = Field(default_factory=dict)
    can_plan: bool = False
    can_confirm: bool = False
    can_amend: bool = False
    can_execute: bool = False
    can_administer: bool = False

    def allows_field(self, kind: str, field: str) -> bool:
        return field in self.fields.get(kind, [])


def governable_fields(kind: str) -> frozenset[str]:
    """Every field a role may ever list for a kind: the plannable
    fields, plus the create fields for kinds created whole."""
    return PLANNABLE_FIELDS.get(kind, frozenset()) | CREATE_FIELDS.get(
        kind, frozenset())


def load_role(raw: dict) -> RoleDefinition:
    """The strict loader: a stored or proposed definition either
    validates whole or is refused with every problem named. Used
    for the seeded defaults and for every edit, so a definition the
    loader would refuse can never reach the store."""
    try:
        role = RoleDefinition(**raw)
    except Exception as exc:
        raise RoleError("role_invalid", f"invalid role definition: {exc}")
    problems: list[str] = []
    if not role.name.strip():
        problems.append("the role name is empty")
    for page in role.pages:
        if page not in KNOWN_PAGES:
            problems.append(f"'{page}' is not a console page a plan "
                            f"can name")
    for kind, fields in role.fields.items():
        if kind not in RECORD_KINDS:
            problems.append(f"'{kind}' is not a record kind")
            continue
        for field in fields:
            if field not in governable_fields(kind):
                problems.append(
                    f"'{field}' on {kind} is not a field any role "
                    f"may govern")
    if problems:
        raise RoleError("role_invalid",
                        "invalid role definition: "
                        + "; ".join(problems))
    return role


def _full_fields() -> dict[str, list[str]]:
    return {kind: sorted(governable_fields(kind))
            for kind in RECORD_KINDS}


def _operator_fields() -> dict[str, list[str]]:
    fields = _full_fields()
    # Held back from operators: a customer's credit limit is a
    # supervisor's change, not the desk's.
    fields["customers"] = [f for f in fields["customers"]
                           if f != "credit_limit"]
    return fields


# The default roles, as data. Every definition here passes
# load_role; the loader test proves it, so the seed can never
# drift into a shape the edit path would refuse.
DEFAULT_ROLES: list[dict] = [
    {
        "name": "operator",
        "pages": sorted(KNOWN_PAGES),
        "fields": _operator_fields(),
        "can_plan": True, "can_confirm": True, "can_amend": True,
        "can_execute": True, "can_administer": False,
    },
    {
        "name": "supervisor",
        "pages": sorted(KNOWN_PAGES),
        "fields": _full_fields(),
        "can_plan": True, "can_confirm": True, "can_amend": True,
        "can_execute": True, "can_administer": True,
    },
    {
        "name": "viewer",
        "pages": sorted(KNOWN_PAGES),
        "fields": {},
        "can_plan": False, "can_confirm": False, "can_amend": False,
        "can_execute": False, "can_administer": False,
    },
]

# Which operator name holds which role, and whether the name is
# active. Seeded like the roles; from the seed on, the directory
# changes only through the recorded acts in the service (enrol,
# assign, deactivate, reactivate). An unknown name holds no role
# at all — it may do nothing — and a deactivated name, though it
# stays in the directory so its history keeps resolving, may do
# nothing either.
DEFAULT_OPERATORS: list[dict] = [
    {"id": "j.okafor", "role": "operator", "active": True},
    {"id": "op", "role": "operator", "active": True},
    {"id": "s.hale", "role": "supervisor", "active": True},
    {"id": "v.ward", "role": "viewer", "active": True},
]

# The shell's own actors. Not every audit entry is a person's
# act: the planner writes `plan_created` under "planner", policy
# refusals land under "policy", the verifier writes its verdicts
# under "verifier", and the drivers write their steps under their
# driver names (drivers.DRIVER_OFFLINE / DRIVER_LIVE). These
# names are governed here so attribution has a closed world to
# resolve against: an audit actor is attributable when it is a
# directory name, in any state, or one of these components.
SYSTEM_ACTORS = frozenset({
    "planner", "policy", "verifier",
    "offline", "page-agent-live",
})


def step_fields(step) -> list[tuple[str, str]]:
    """The (record kind, field) pairs a plan step touches: a SET
    step's one field; a CREATE step's governed create-fields."""
    if step.action == StepAction.SET:
        return [(step.record_kind, step.field or "")]
    return [(step.record_kind, name) for name in sorted(step.fields)]


def plan_problems(role: RoleDefinition,
                  plan: Plan) -> list[dict]:
    """Every way this plan exceeds the role's allowlist, as named
    problems ({seq, page, record_kind, field, problem}) — the page
    a step works on and each field it touches must both be the
    role's to touch. An empty list means the plan fits the role.
    The same check runs at plan time, at amendment, and again at
    execution against the definitions then in force."""
    problems: list[dict] = []
    for step in plan.steps:
        if step.page not in role.pages:
            problems.append({
                "seq": step.seq, "page": step.page,
                "record_kind": step.record_kind, "field": None,
                "problem": "page_not_permitted"})
        for kind, field in step_fields(step):
            if not role.allows_field(kind, field):
                problems.append({
                    "seq": step.seq, "page": step.page,
                    "record_kind": kind, "field": field,
                    "problem": "field_not_permitted"})
    return problems


def describe_problem(role: RoleDefinition, problem: dict) -> str:
    """A problem as a sentence naming the field (or page) and the
    role — the form refusals carry."""
    if problem["problem"] == "page_not_permitted":
        return (f"role '{role.name}' may not work on the "
                f"{problem['page']} page (step {problem['seq']})")
    return (f"role '{role.name}' may not touch "
            f"'{problem['field']}' on {problem['record_kind']} "
            f"(step {problem['seq']})")


def role_diff(old: RoleDefinition, new: RoleDefinition) -> list[dict]:
    """The field-level difference between two definitions of one
    role — what a role edit changed, for the audit record."""
    changes: list[dict] = []
    if old.pages != new.pages:
        changes.append({"field": "pages", "old": old.pages,
                        "new": new.pages})
    for kind in sorted(set(old.fields) | set(new.fields)):
        before = old.fields.get(kind, [])
        after = new.fields.get(kind, [])
        if before != after:
            changes.append({"field": f"fields.{kind}", "old": before,
                            "new": after})
    for flag in ("can_plan", "can_confirm", "can_amend",
                 "can_execute", "can_administer"):
        if getattr(old, flag) != getattr(new, flag):
            changes.append({"field": flag, "old": getattr(old, flag),
                            "new": getattr(new, flag)})
    return changes


def operator_named(name: Optional[str]) -> str:
    """Actor names compare normalised: ' S.Hale ' is s.hale. The
    self-confirmation check depends on it — a spacing or casing
    variant of the planner's own name is still the planner."""
    return (name or "").strip().casefold()
