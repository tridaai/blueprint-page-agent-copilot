"""Domain records and the governed task types.

OrderDesk records (customers, invoices, orders, credit memos) are
plain dicts in the store — one source of truth the console renders,
the planner reads, the drivers mutate, and the verifier diffs.

The task types here are the shell's contract: a Plan is a stored,
versioned list of field-level Steps; a Task moves through the state
machine in `service.py` and nowhere else.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- records

RECORD_KINDS = ("customers", "invoices", "orders", "credit_memos")

# Fields a plan may never touch, on any record: money-movement fields.
# Named here once; policy.py enforces the refusal at plan time.
PAYMENT_FIELDS = frozenset({
    "bank_account", "iban", "sort_code", "account_number",
    "card_number", "card_last4", "card_expiry", "payment_method",
})

# Fields the copilot may plan changes to, per record kind. Anything
# outside this list is not plannable in this blueprint (a human can
# still edit it in the console directly; the copilot may not).
PLANNABLE_FIELDS: dict[str, frozenset[str]] = {
    "customers": frozenset({
        "name", "email", "phone", "address", "city", "postcode",
        "credit_limit",
    }),
    "orders": frozenset({"status", "ship_date", "carrier"}),
    "invoices": frozenset({"status", "due_date", "memo"}),
    "credit_memos": frozenset(),  # created whole, never edited by plan
}

ORDER_STATUSES = frozenset({
    "placed", "packed", "shipped", "delivered", "cancelled",
})
INVOICE_STATUSES = frozenset({"open", "paid", "overdue", "void"})


# ------------------------------------------------------------------- plan

class StepAction(str, Enum):
    SET = "set"          # set one field on a record's console form
    CREATE = "create"    # submit a create form (credit memo) as a whole


class PlanStep(BaseModel):
    """One field-level step of a plan.

    `page` is the console screen the step happens on; `instruction`
    is the natural-language instruction the in-page agent executes
    for this step, compiled from the same fields the verifier later
    diffs — the live agent and the offline driver work from one step
    definition, never two."""

    seq: int
    action: StepAction
    page: str
    record_kind: str
    record_id: Optional[str] = None          # for SET
    field: Optional[str] = None              # for SET
    value: Optional[str] = None              # for SET (string form)
    fields: dict[str, str] = Field(default_factory=dict)  # for CREATE
    instruction: str = ""


class RecordPrecondition(BaseModel):
    """The state a plan was derived from, for one record it touches:
    a content hash of the record as it stood at plan time. Execution
    re-hashes the same records; a difference means the plan's basis
    moved and the plan is stale (see service.execute)."""

    record_kind: str
    record_id: str
    hash: str


class PlanChange(BaseModel):
    """One field-level change, recorded wherever plans change:
    an amendment's diff, a plan-version diff. `old`/`new` are the
    string forms the plan itself carries; None means absent on
    that side."""

    seq: int
    field: str
    old: Optional[str] = None
    new: Optional[str] = None


class Plan(BaseModel):
    task_id: str
    version: int = 1
    task_type: str
    summary: str
    steps: list[PlanStep]
    preconditions: list[RecordPrecondition] = Field(default_factory=list)
    superseded: bool = False
    created_at: str = ""


# ------------------------------------------------------------- plan diffs

class StepDiff(BaseModel):
    """How one step (by seq) differs between two plan versions."""

    seq: int
    change: str  # "added" | "removed" | "changed"
    changes: list[PlanChange] = Field(default_factory=list)


class PlanDiff(BaseModel):
    task_id: str
    from_version: int
    to_version: int
    step_diffs: list[StepDiff] = Field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not self.step_diffs


# ------------------------------------------------------------------- task

class TaskStatus(str, Enum):
    DRAFT = "DRAFT"                # intake received, no plan yet
    PLANNED = "PLANNED"            # a plan is stored, awaiting a human
    CONFIRMED = "CONFIRMED"        # a named operator approved the plan
    VERIFIED = "VERIFIED"          # executed; post-state == plan, exactly
    DISCREPANCY = "DISCREPANCY"    # executed; post-state != plan (named)
    REFUSED = "REFUSED"            # refused at plan time by policy
    STALE = "STALE"                # a record the plan rests on changed
                                   # between planning and execution;
                                   # execution refused, re-plan required
    REJECTED = "REJECTED"          # a human rejected the plan; the
                                   # route onward is a fresh intake


class Task(BaseModel):
    task_id: str
    text: str
    operator: str
    status: TaskStatus = TaskStatus.DRAFT
    plan_version: Optional[int] = None
    confirmed_by: Optional[str] = None
    # The exact plan version the confirmation binds. Execution runs
    # this version — never "the latest" — so a confirmation of v1
    # can never execute a v2 that did not exist when the human said
    # yes. (Amendment after confirmation is refused outright; this
    # binding is the second lock on the same door.)
    confirmed_version: Optional[int] = None
    # Records whose state changed under a stale plan, named as
    # "kind id" (set on the STALE transition, cleared on re-plan).
    stale_records: list[str] = Field(default_factory=list)
    refusal_reason: Optional[str] = None
    driver: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class StepOutcome(BaseModel):
    """One executed step, as recorded by whichever driver ran it."""

    seq: int
    driver: str
    ok: bool
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


class FieldDiff(BaseModel):
    seq: int = 0                 # the plan step whose check failed
    record_kind: str
    record_id: str
    field: str
    planned: Optional[str] = None
    actual: Optional[str] = None


class VerificationResult(BaseModel):
    ok: bool
    diffs: list[FieldDiff] = Field(default_factory=list)
    checked: int = 0


class AuditEntry(BaseModel):
    task_id: str
    ts: str = ""
    actor: str = ""
    event: str
    detail: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------- api shapes

class TaskIntake(BaseModel):
    text: str
    operator: str = "operator"


class ConfirmIn(BaseModel):
    operator: str


class AmendChangeIn(BaseModel):
    """One amendment to one plan step, addressed by step seq.

    For a SET step, `value` is the new planned value. For a CREATE
    step, `fields` carries the replacement values for individual
    create-fields (e.g. {"amount": "150.00"}). The amended plan
    passes every value through the same policy checks planning
    used; a change policy would have refused in a fresh plan is
    refused as an amendment too."""

    seq: int
    value: Optional[str] = None
    fields: dict[str, str] = Field(default_factory=dict)


class AmendIn(BaseModel):
    operator: str
    changes: list[AmendChangeIn]


class RejectIn(BaseModel):
    operator: str


class ReplanIn(BaseModel):
    operator: str


class ExecuteIn(BaseModel):
    driver: str = "offline"  # "offline" | "page-agent-live"
    # Who is executing. Optional: an omitted executor is the task's
    # own operator (the planner running their confirmed plan). The
    # executor's role is re-checked at execution either way.
    operator: Optional[str] = None


class RoleUpdateIn(BaseModel):
    """A recorded role edit: the full replacement definition, and
    the operator making the change (who must hold a role with
    can_administer). The definition passes the strict loader in
    roles.py before anything is stored."""

    operator: str
    pages: list[str] = Field(default_factory=list)
    fields: dict[str, list[str]] = Field(default_factory=dict)
    can_plan: bool = False
    can_confirm: bool = False
    can_amend: bool = False
    can_execute: bool = False
    can_administer: bool = False


class StepReportIn(BaseModel):
    """The browser bridge's report of one live step. Advisory only:
    the shell's verification diffs the store, never this report."""
    driver: str = "page-agent-live"
    ok: bool = True
    detail: str = ""


class ConsoleSaveIn(BaseModel):
    """A console form save (a human at the keyboard, or the in-page
    agent submitting the form it filled). Only the fields the form
    carries are applied."""
    fields: dict[str, str]
    task_id: Optional[str] = None


def money(value: str | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))
