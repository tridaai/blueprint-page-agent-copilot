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


class Plan(BaseModel):
    task_id: str
    version: int = 1
    task_type: str
    summary: str
    steps: list[PlanStep]
    created_at: str = ""


# ------------------------------------------------------------------- task

class TaskStatus(str, Enum):
    DRAFT = "DRAFT"                # intake received, no plan yet
    PLANNED = "PLANNED"            # a plan is stored, awaiting a human
    CONFIRMED = "CONFIRMED"        # a named operator approved the plan
    VERIFIED = "VERIFIED"          # executed; post-state == plan, exactly
    DISCREPANCY = "DISCREPANCY"    # executed; post-state != plan (named)
    REFUSED = "REFUSED"            # refused at plan time by policy


class Task(BaseModel):
    task_id: str
    text: str
    operator: str
    status: TaskStatus = TaskStatus.DRAFT
    plan_version: Optional[int] = None
    confirmed_by: Optional[str] = None
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


class ExecuteIn(BaseModel):
    driver: str = "offline"  # "offline" | "page-agent-live"


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
