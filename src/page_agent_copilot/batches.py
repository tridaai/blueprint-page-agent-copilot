"""Batches: work submitted together, worked one task at a time.

The desk's work rarely arrives one job at a time — it arrives as
a morning list. A batch is the record of that list: the tasks
submitted together, in submission order, under one creator.
Like the roles and the templates, a batch is a governed record
in the store (kind `batches`), written once at creation and
never edited afterwards.

What a batch is not is a unit of decision. There is no batch
confirmation, no batch execution, no batch anything-act: the
confirmation gate binds one plan version on one task, confirmed
by one named second person, and a batch changes none of that.
Every member is an ordinary task — stated through ordinary
intake, or instantiated from a library template and stated
through ordinary intake — and it takes its whole lifecycle one
task at a time, under the same role rules. The batch organises
attention; it never takes acts. What it adds is two reads folded
from the member records at the moment of asking: a **run sheet**
(each member's state, the next act it needs, and who may take
it) and a **report** (counts by state, the credited total read
from the members' verification reports, the discrepancies and
failures still open). Neither is a stored claim — the batch
record holds only what creation produced: the member task ids
in order, the creator, and, for a member that never became a
task, the refusal that stopped it.

A member that refuses is a member. A task text the planner or
policy refuses at plan time comes back from intake as a REFUSED
task, and it sits in the batch with its reason, visible — a
batch never silently drops the work it could not take. A
template member whose slots do not validate creates no task at
all (instantiation refuses before intake, the library's own
rule), so the batch records that refusal on the member itself.
Members share nothing but the batch id: no shared plan state,
no shared fate — one member going STALE, failing, or landing
in DISCREPANCY says nothing about any other member's lifecycle.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .models import Task, TaskStatus

# The audit scope batch administration is recorded under: the
# audit trail is keyed by task id, and creating a batch is not
# one task's act, so batches keep their own reserved scope, the
# way role administration keeps `roles` and the library keeps
# `templates`. (Each member task's own trail also names its
# batch — see `batch_member` in the service.)
BATCH_AUDIT_SCOPE = "batches"

# The states a task does not leave: each is an ending a report
# can close on. DISCREPANCY is not here — a discrepancy still
# owes one of its two recorded exits — and neither is any state
# that still owes a confirmation, an execution, a re-plan, or a
# recovery act.
TERMINAL_STATUSES = (
    TaskStatus.VERIFIED,
    TaskStatus.DISCREPANCY_ACCEPTED,
    TaskStatus.REFUSED,
    TaskStatus.REJECTED,
    TaskStatus.ABANDONED,
)


class BatchError(ValueError):
    """A batch submission that does not validate (HTTP 422 at
    the API): refused whole, before any member is stated — a
    malformed list is not a batch with some members missing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BatchMemberRecord(BaseModel):
    """One member of a batch, as creation left it: its position
    in submission order, how it was submitted, and the task it
    became — or, for a template member whose slots refused
    before a task existed, the refusal itself. A member whose
    task exists carries no refusal here: a plan-time refusal
    lives on the task, where every other refusal lives, and the
    batch's reads take it from there."""

    position: int
    kind: str  # "text" | "template"
    task_id: Optional[str] = None
    text: Optional[str] = None
    template_name: Optional[str] = None
    template_version: Optional[int] = None
    refusal_code: Optional[str] = None
    refusal_reason: Optional[str] = None


class Batch(BaseModel):
    """One batch, as stored. Deliberately absent: any status.
    Whether the batch is complete, and everything the run sheet
    and report say, is folded from the member tasks at read
    time — a stored status would be a second claim that could
    drift from the members it summarises."""

    batch_id: str
    created_by: str
    created_at: str = ""
    members: list[BatchMemberRecord] = Field(default_factory=list)

    @property
    def record_id(self) -> str:
        return self.batch_id


def next_act_for(task: Task) -> dict:
    """The next act one task needs, and who may take it —
    derived from the state machine and the role rules, in one
    place, so the run sheet and the report can never disagree
    about what a state is owed. Terminal states owe nothing:
    `action` is None and `detail` says how the task ended."""
    planner = task.planned_by or task.operator
    status = task.status
    if status == TaskStatus.PLANNED:
        return {
            "action": "confirm",
            "detail": (f"confirm plan v{task.plan_version} — or "
                       f"amend it, or reject it"),
            "who": (f"a holder of a confirm-capable role other "
                    f"than the planner ({planner}); "
                    f"self-confirmation is refused for every role"),
        }
    if status == TaskStatus.CONFIRMED:
        return {
            "action": "execute",
            "detail": (f"execute the confirmed plan "
                       f"v{task.confirmed_version}"),
            "who": ("a holder of an execute-capable role; the "
                    "bound plan is re-checked against their "
                    "allowlist at execution"),
        }
    if status == TaskStatus.STALE:
        changed = (f" (records changed under the plan: "
                   f"{', '.join(task.stale_records)})"
                   if task.stale_records else "")
        return {
            "action": "replan",
            "detail": (f"re-plan from the records as they now "
                       f"stand{changed}"),
            "who": f"the task's operator ({task.operator})",
        }
    if status == TaskStatus.EXECUTION_FAILED:
        return {
            "action": "resume",
            "detail": (f"resume, restart, or abandon — the run "
                       f"stopped at step {task.failed_step}: "
                       f"{task.failure_error}"),
            "who": ("a holder of an execute-capable role; "
                    "abandoning needs a reason"),
        }
    if status == TaskStatus.DISCREPANCY:
        return {
            "action": "corrective",
            "detail": ("spawn a corrective task that closes "
                       "the named differences, or accept the "
                       "deviation with a reason"),
            "who": ("a plan-capable operator spawns the "
                    "corrective; accepting the deviation is a "
                    "supervisor act"),
        }
    if status == TaskStatus.DRAFT:
        return {
            "action": "plan",
            "detail": "state the task so a plan can be derived",
            "who": f"the task's operator ({task.operator})",
        }
    endings = {
        TaskStatus.VERIFIED:
            "complete — executed and verified against the plan",
        TaskStatus.DISCREPANCY_ACCEPTED:
            "complete — a supervisor accepted the named deviation",
        TaskStatus.REFUSED:
            f"complete — refused at plan time: "
            f"{task.refusal_reason}",
        TaskStatus.REJECTED:
            "complete — the plan was rejected; the route "
            "onward is a fresh task",
        TaskStatus.ABANDONED:
            "complete — the failed execution was closed as "
            "abandoned, with a reason",
    }
    return {"action": None,
            "detail": endings.get(status, "complete"),
            "who": None}


class BatchMemberIn(BaseModel):
    """One member of a batch submission: either a task text, or
    a library template with its slot values — exactly one of
    the two. `version` may be omitted for a template member, in
    which case the library's current version is used, and the
    exact version is named on the member either way."""

    text: Optional[str] = None
    template: Optional[str] = None
    version: Optional[int] = None
    slots: dict[str, str] = Field(default_factory=dict)


class BatchCreateIn(BaseModel):
    """Create a batch: the creator, and the members in
    submission order. Creating a batch states each member
    through ordinary intake — it decides nothing about any of
    them, and there is deliberately no batch-level act beside
    this one."""

    operator: str
    members: list[BatchMemberIn] = Field(default_factory=list)
