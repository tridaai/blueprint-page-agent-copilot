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
    DISCREPANCY_ACCEPTED = "DISCREPANCY_ACCEPTED"
                                   # a supervisor accepted the named
                                   # deviation, with a reason; terminal,
                                   # and never rewritten to VERIFIED
    REFUSED = "REFUSED"            # refused at plan time by policy
    STALE = "STALE"                # a record the plan rests on changed
                                   # between planning and execution;
                                   # execution refused, re-plan required
    REJECTED = "REJECTED"          # a human rejected the plan; the
                                   # route onward is a fresh intake
    EXECUTION_FAILED = "EXECUTION_FAILED"
                                   # a driver step failed mid-plan;
                                   # execution stopped at that step and
                                   # the completed steps stand as they
                                   # landed — the exits are resume,
                                   # restart, or abandon, all recorded
    ABANDONED = "ABANDONED"        # a failed execution an operator
                                   # closed, with a reason; the partial
                                   # state stays on the records, named


class Task(BaseModel):
    task_id: str
    text: str
    operator: str
    # Who planned the task, as enrolled at intake. `operator` is
    # the name the task currently belongs to — a supervisor's
    # pickup of a deactivated operator's work re-names it — and
    # `planned_by` is how the original planner stays on the
    # record after that. Tasks from before the directory round
    # carry None here; readers fall back to `operator`.
    planned_by: Optional[str] = None
    # The operator a pickup took this task from, when a supervisor
    # picked it up: the name whose deactivation orphaned the work.
    picked_up_from: Optional[str] = None
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
    # The library template this task was instantiated from, when
    # it was: the name and the exact version used — never merely
    # "the latest", because a version a task names is the version
    # whose pattern produced its text. A typed task carries None.
    template_name: Optional[str] = None
    template_version: Optional[int] = None
    # The batch this task was submitted in, when it was: the one
    # thing members of a batch share. A batch groups tasks for
    # attention — the run sheet, the report — and decides nothing
    # about any of them; every gate on this task is the ordinary
    # per-task one. A task stated on its own carries None.
    batch_id: Optional[str] = None
    # The correction chain, recorded both ways: a corrective task
    # names the task whose discrepancy spawned it here, and that
    # task lists the corrective in `corrective_task_ids`.
    parent_task_id: Optional[str] = None
    corrective_task_ids: list[str] = Field(default_factory=list)
    # The deviation acceptance, when a supervisor closes a
    # discrepancy by accepting it: who, and the mandatory reason.
    deviation_accepted_by: Optional[str] = None
    deviation_reason: Optional[str] = None
    # Execution attempts: how many have run (the attempt records
    # themselves are stored records, kind `executions`), and the
    # latest failure's summary, mirrored here so the task names it
    # wherever the task is shown — the failed step and the driver's
    # error, verbatim. Cleared when a new attempt starts; set again
    # if that attempt fails.
    attempts: int = 0
    failed_step: Optional[int] = None
    failure_error: Optional[str] = None
    # The abandonment, when an operator closes a failed execution:
    # who, and the mandatory reason. The partial state the task was
    # left in is named in the final attempt's record and in the
    # audit entry — it is never cleaned up quietly.
    abandoned_by: Optional[str] = None
    abandon_reason: Optional[str] = None
    # The store-wide creation sequence, assigned by the service
    # when the task is first stored and never edited afterwards:
    # the order task lists read in. Neither the id (random hex)
    # nor `created_at` (one-second granularity — two tasks in
    # the same second tie) can order a list, so the sequence is
    # the guarantee that a list reads in the order the work
    # arrived. Tasks stored before the field existed carry 0 and
    # sort behind it by (created_at, task_id).
    seq: int = 0
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


class ExecutionAttempt(BaseModel):
    """One execution attempt at a task's confirmed plan, as a
    stored record (kind `executions`, keyed `<task_id>:<number>`).

    Attempts are numbered from 1 and never rewritten once they
    finish: attempt 1 is the `execute` act; a `resume` or `restart`
    after a failure is attempt n+1, with `mode` saying which.
    While an attempt's outcome is `running` it is also the task's
    execution claim — a second execute/resume/restart is refused
    until it finishes (for the offline driver an attempt finishes
    inside the call that started it; for the live bridge it stays
    running across the step reports, which is exactly the window
    a concurrent execution must not enter).

    `steps` holds every outcome this attempt produced, in order —
    the completed steps with their before/after values, and, when
    the attempt failed, the failed step last with `ok` False and
    its `detail` the driver's error verbatim. `state_lines` names,
    line by line, the state the attempt left in place: on a failed
    attempt that is the partial state, and it is what an
    abandonment records.
    """

    task_id: str
    number: int
    driver: str
    # The operator who ran this attempt — the executor the role
    # re-check passed. The audit's execution entries carry it too,
    # so an execution is attributable to a person, not only to a
    # driver. Attempts from before the directory round carry "".
    executor: str = ""
    plan_version: int
    mode: str  # "execute" | "resume" | "restart"
    outcome: str = "running"  # "running" | "completed" | "failed"
    # For the live flow, the attempt number from which completed
    # steps count toward this execution: a resume counts from 1
    # (earlier attempts' work stands); a restart counts from its
    # own number (the plan runs again from step 1).
    base_attempt: int = 1
    started_at: str = ""
    finished_at: str = ""
    steps: list[StepOutcome] = Field(default_factory=list)
    failed_seq: Optional[int] = None
    error: Optional[str] = None
    state_lines: list[str] = Field(default_factory=list)


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


# ------------------------------------------------------- verification

class FieldCheck(BaseModel):
    """One planned field, checked: what the plan said, what the
    re-read through the console read surface found, and the
    verdict. Every planned field gets a row — a report that only
    listed failures could not show what was actually checked."""

    seq: int
    record_kind: str
    record_id: str
    field: str
    expected: Optional[str] = None
    actual: Optional[str] = None
    verdict: str  # "match" | "mismatch"


class LinkageCheck(BaseModel):
    """One linkage assertion about a step's records: not a planned
    field's value, but whether the records hang together the way
    the console's own rules say they should — a created memo names
    its invoice, its customer, and the task that created it; the
    invoice lists the memo back; a touched record still names a
    customer that exists; and the execution record in the audit
    trail (who ran the step, when) agrees with the state the
    re-read found. A failed linkage check is a discrepancy with a
    named field, exactly like a value mismatch: it lands in the
    report's `diffs` under `field`."""

    seq: int
    record_kind: str
    record_id: str
    check: str  # created_for_task | memo_customer | invoice_customer
                # | invoice_reflects_memo | customer_link
                # | execution_record
    field: str  # the field a failure is named under in the diffs
    expected: Optional[str] = None
    actual: Optional[str] = None
    verdict: str  # "match" | "mismatch"
    detail: str = ""


class CorrectionEntry(BaseModel):
    """One corrective task in a discrepancy's correction chain, as
    the parent task's verification report shows it. Composed at
    read time from the task records — the stored report itself is
    never rewritten after verification."""

    task_id: str
    status: str
    plan_version: Optional[int] = None
    verification_ok: Optional[bool] = None


class VerificationReport(BaseModel):
    """The stored record of one verification act: the plan version
    verified, the driver that ran it, the read path the verdict
    came from (`console_read_surface` — see reads.py), every field
    check and linkage check with its verdict, and the diffs (the
    failing checks, field and linkage alike). Written once, when
    execution completes; re-verification is refused, because the
    report is the evidence, not a recomputation."""

    task_id: str
    plan_version: int
    driver: str
    read_path: str
    verified_at: str
    ok: bool
    checked: int = 0  # field checks performed (the audit's count)
    field_checks: list[FieldCheck] = Field(default_factory=list)
    linkage_checks: list[LinkageCheck] = Field(default_factory=list)
    diffs: list[FieldDiff] = Field(default_factory=list)
    # Read-time composition (service.get_verification): the
    # corrective tasks this task's discrepancy spawned, with their
    # outcomes. Always empty in the stored record.
    corrections: list[CorrectionEntry] = Field(default_factory=list)


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


class EnrollOperatorIn(BaseModel):
    """Enrol a name in the operator directory. A recorded
    supervisor act: `operator` is the acting supervisor, `name`
    the name being enrolled (stored normalised), `role` the
    role it is assigned."""
    operator: str
    name: str
    role: str


class AssignRoleIn(BaseModel):
    """Change the role an enrolled name holds. A recorded
    supervisor act; the before and after ride the audit entry."""
    operator: str
    role: str


class DirectoryActIn(BaseModel):
    """Deactivate or reactivate an enrolled name. A recorded
    supervisor act; `operator` is the acting supervisor."""
    operator: str


class PickupIn(BaseModel):
    """Pick up a deactivated operator's orphaned task. A recorded
    supervisor act: the task's operator becomes the supervisor,
    and the original planner stays on the record."""
    operator: str


class CorrectiveIn(BaseModel):
    """Spawn a corrective task for a DISCREPANCY task. The operator
    is the corrective plan's planner — the R3 role rules apply to
    it exactly as to any plan."""
    operator: str


class AcceptDeviationIn(BaseModel):
    """Accept a discrepancy's named deviation. A supervisor act
    (a role with can_administer), and the reason is mandatory:
    an acceptance without one is refused."""
    operator: str
    reason: str = ""


class ResumeIn(BaseModel):
    """Resume a failed execution: run only the steps that never
    completed. The operator rides along (defaulting to the task's
    own operator) — the resume is an execution act and the
    executor's role is re-checked, as at execution."""
    operator: Optional[str] = None


class RestartIn(BaseModel):
    """Restart a failed execution from step 1, as a new attempt.
    Refused once any CREATE step has completed — a created record
    is never created twice."""
    operator: Optional[str] = None


class AbandonIn(BaseModel):
    """Abandon a failed execution. The operator closes the task
    with a mandatory reason; the partial state stays on the
    records and is named in the audit entry."""
    operator: str
    reason: str = ""


class VerifyIn(BaseModel):
    """A request to verify a task (again). Verification ran at the
    end of execution and its report is recorded; the service
    refuses the repeat — the operator rides along so the refusal
    is attributed in the audit trail."""
    operator: str = "operator"


class AuditExportIn(BaseModel):
    """A request to export one scope's audit trail, chained.
    Exporting is a recorded act: `operator` is the exporter, who
    must be an enrolled, active operator. `format` names the
    rendering — the JSON document, or its CSV rendering; the
    chain is the same either way, and verification runs over
    the document."""
    operator: str
    format: str = "json"  # "json" | "csv"


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
