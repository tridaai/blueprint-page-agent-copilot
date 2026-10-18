"""The governed task shell: intake -> plan -> confirm -> execute ->
verify, with the audit written as it goes.

The state machine is enforced here, in code, against the store —
not by the API layer and never by the agent:

    DRAFT -> PLANNED -> CONFIRMED -> VERIFIED | DISCREPANCY
    DRAFT -> REFUSED            (plan-time policy refusal)
    PLANNED -> PLANNED          (amendment: plan version n+1, the
                                 diff recorded, confirmation still
                                 owed on the new version)
    PLANNED | CONFIRMED -> REJECTED   (a human's no; the route
                                 onward is a fresh intake)
    CONFIRMED -> STALE          (a record the plan rests on changed
                                 before execution; nothing ran)
    STALE -> PLANNED            (re-plan: a fresh version derived
                                 from the records as they now stand,
                                 the stale version kept, superseded)

Executing an unconfirmed plan raises GateError (the API maps it to
HTTP 409). Confirmation binds an exact plan version, and execution
runs that version. A task whose post-state does not match its
approved plan exactly lands in DISCREPANCY with the differing
fields and their steps named; no path reports it as a success.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from .config import Settings
from .drivers import DRIVER_LIVE, DRIVER_OFFLINE, OfflineDriver
from .models import (
    AmendChangeIn,
    AuditEntry,
    Plan,
    PlanChange,
    PlanDiff,
    StepOutcome,
    StepReportIn,
    Task,
    TaskStatus,
)
from .plandiff import diff_plans
from .planner import (
    amend_steps,
    capture_preconditions,
    derive_plan,
    stale_records,
    summarize_steps,
)
from .policy import PlanRefusal
from .store import Store
from .verify import verify_plan


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class GateError(Exception):
    """A state-machine refusal (HTTP 409 at the API)."""


class CopilotService:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self._lock = threading.Lock()

    # -- setup ------------------------------------------------------
    def seed(self) -> bool:
        from .dataset import seed_records

        return self.store.seed_if_empty(seed_records())

    # -- audit --------------------------------------------------------
    def _audit(self, task_id: str, actor: str, event: str,
               detail: Optional[dict] = None) -> None:
        self.store.add_audit(AuditEntry(
            task_id=task_id, ts=_now(), actor=actor, event=event,
            detail=detail or {}).model_dump(mode="json"))

    def audit(self, task_id: str) -> list[dict]:
        return self.store.list_audit(task_id)

    # -- intake + planning ---------------------------------------------
    def intake(self, text: str, operator: str) -> Task:
        task_id = f"TASK-{uuid.uuid4().hex[:8].upper()}"
        task = Task(task_id=task_id, text=text, operator=operator,
                    created_at=_now(), updated_at=_now())
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task_id, operator, "task_created", {"text": text})
        try:
            plan = derive_plan(text, self.store, self.settings.policy,
                               task_id)
        except PlanRefusal as refusal:
            task.status = TaskStatus.REFUSED
            task.refusal_reason = f"{refusal.code}: {refusal.message}"
            task.updated_at = _now()
            self.store.put_task(task.model_dump(mode="json"))
            self._audit(task_id, "policy", "plan_refused", {
                "code": refusal.code, "reason": refusal.message})
            return task
        plan.created_at = _now()
        plan.preconditions = capture_preconditions(self.store, plan)
        self.store.put_plan(plan.model_dump(mode="json"))
        task.status = TaskStatus.PLANNED
        task.plan_version = plan.version
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
        self._audit(task_id, "planner", "plan_created", {
            "plan_version": plan.version, "task_type": plan.task_type,
            "summary": plan.summary, "steps": len(plan.steps)})
        return task

    # -- reads ----------------------------------------------------------
    def get_task(self, task_id: str) -> Optional[Task]:
        raw = self.store.get_task(task_id)
        return Task(**raw) if raw else None

    def list_tasks(self) -> list[Task]:
        return [Task(**t) for t in self.store.list_tasks()]

    def get_plan(self, task_id: str,
                 version: Optional[int] = None) -> Optional[Plan]:
        raw = (self.store.get_plan(task_id, version)
               if version is not None
               else self.store.latest_plan(task_id))
        return Plan(**raw) if raw else None

    def list_plans(self, task_id: str) -> list[Plan]:
        plans: list[Plan] = []
        version = 1
        while True:
            plan = self.get_plan(task_id, version)
            if plan is None:
                return plans
            plans.append(plan)
            version += 1

    def plan_diff(self, task_id: str, from_version: int,
                  to_version: int) -> PlanDiff:
        self._require_task(task_id)
        old = self.get_plan(task_id, from_version)
        new = self.get_plan(task_id, to_version)
        if old is None or new is None:
            missing = from_version if old is None else to_version
            raise GateError(
                f"task {task_id} has no plan version {missing} to diff")
        return diff_plans(old, new)

    def _store_plan_version(self, task: Task, plan: Plan) -> None:
        """Store a new plan version for the task, marking the
        version it replaces as superseded (kept, readable, never
        executed again — execution binds a confirmed version, and a
        superseded one is never the latest a confirmation can name).
        """
        previous = self.get_plan(task.task_id)
        if previous is not None and previous.version != plan.version:
            previous.superseded = True
            self.store.put_plan(previous.model_dump(mode="json"))
        self.store.put_plan(plan.model_dump(mode="json"))
        task.plan_version = plan.version

    def _require_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise GateError(f"task {task_id} does not exist")
        return task

    def _save(self, task: Task) -> Task:
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
        return task

    # -- plan amendment, rejection, re-planning ---------------------------
    def amend(self, task_id: str, operator: str,
              changes: list[AmendChangeIn]) -> tuple[Task, Plan,
                                                     list[PlanChange]]:
        """Amend a PLANNED task's plan. The amendment becomes plan
        version n+1, with its field-level diff recorded in the
        audit; the task stays PLANNED and owes a fresh confirmation
        of the new version. Amending a confirmed task is refused:
        a confirmation binds the version the human saw, and the
        route past a confirmed plan is rejection + a fresh intake,
        never an edit. A value policy would refuse in a fresh plan
        is refused here too (PlanRefusal) and changes nothing."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.PLANNED:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a PLANNED "
                f"task's plan can be amended — a confirmed plan is "
                f"executed as confirmed or rejected, never amended")
        current = self.get_plan(task_id)
        assert current is not None
        new_steps, recorded = amend_steps(
            current, changes, self.settings.policy, self.store)
        if not recorded:
            raise GateError(
                f"the amendment to task {task_id} changes nothing; "
                f"plan v{current.version} stands")
        amended = current.model_copy(deep=True)
        amended.version = current.version + 1
        amended.steps = new_steps
        amended.summary = summarize_steps(new_steps)
        amended.created_at = _now()
        amended.superseded = False
        amended.preconditions = capture_preconditions(
            self.store, amended)
        self._store_plan_version(task, amended)
        self._save(task)
        self._audit(task_id, operator, "plan_amended", {
            "from_version": current.version,
            "to_version": amended.version,
            "changes": [c.model_dump(mode="json") for c in recorded]})
        return task, amended, recorded

    def reject(self, task_id: str, operator: str) -> Task:
        """A human's no to a planned or confirmed task. Terminal:
        the operator states a fresh task if the work is still
        wanted, so no rejected plan can be revived into execution."""
        task = self._require_task(task_id)
        if task.status not in (TaskStatus.PLANNED, TaskStatus.CONFIRMED):
            raise GateError(
                f"task {task_id} is {task.status.value}; only a PLANNED "
                f"or CONFIRMED task can be rejected")
        task.status = TaskStatus.REJECTED
        self._save(task)
        self._audit(task_id, operator, "plan_rejected", {
            "plan_version": task.plan_version})
        return task

    def replan(self, task_id: str, operator: str) -> Task:
        """Re-derive a STALE task's plan from the records as they
        now stand. The stale version is kept and marked superseded;
        the task returns to PLANNED under the new version, owing a
        fresh confirmation. If the records as they now stand no
        longer support a plan at all, the task is REFUSED with the
        planner's reason — a re-plan cannot launder a refusal."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.STALE:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a STALE "
                f"task can be re-planned")
        previous = self.get_plan(task_id)
        assert previous is not None
        try:
            plan = derive_plan(task.text, self.store,
                               self.settings.policy, task_id)
        except PlanRefusal as refusal:
            task.status = TaskStatus.REFUSED
            task.refusal_reason = f"{refusal.code}: {refusal.message}"
            task.stale_records = []
            self._save(task)
            self._audit(task_id, "policy", "plan_refused", {
                "code": refusal.code, "reason": refusal.message,
                "during": "replan"})
            return task
        plan.version = previous.version + 1
        plan.created_at = _now()
        plan.preconditions = capture_preconditions(self.store, plan)
        self._store_plan_version(task, plan)
        task.status = TaskStatus.PLANNED
        task.confirmed_by = None
        task.confirmed_version = None
        task.stale_records = []
        self._save(task)
        self._audit(task_id, operator, "plan_replanned", {
            "from_version": previous.version,
            "to_version": plan.version,
            "superseded_version": previous.version})
        return task

    # -- the gate ---------------------------------------------------------
    def confirm(self, task_id: str, operator: str) -> Task:
        task = self._require_task(task_id)
        if task.status != TaskStatus.PLANNED:
            raise GateError(
                f"task {task_id} is {task.status.value}; only a PLANNED "
                f"task can be confirmed")
        task.status = TaskStatus.CONFIRMED
        task.confirmed_by = operator
        # The confirmation binds this exact version. Execution loads
        # it by number, so nothing that happens to the plan store
        # afterwards can widen what the human approved.
        task.confirmed_version = task.plan_version
        self._save(task)
        self._audit(task_id, operator, "plan_confirmed", {
            "plan_version": task.plan_version,
            "confirmed_version": task.confirmed_version})
        return task

    def _bound_plan(self, task: Task) -> Plan:
        """The plan version this task's execution is bound to: the
        confirmed version once confirmed, else the current plan."""
        version = task.confirmed_version or task.plan_version
        plan = (self.get_plan(task.task_id, version)
                if version is not None else self.get_plan(task.task_id))
        assert plan is not None
        return plan

    # -- execution ---------------------------------------------------------
    def execute(self, task_id: str, driver: str = DRIVER_OFFLINE,
                *, tamper: Optional[dict[int, str]] = None) -> Task:
        task = self._require_task(task_id)
        if task.status != TaskStatus.CONFIRMED:
            raise GateError(
                f"task {task_id} is {task.status.value}; execution "
                f"requires a CONFIRMED plan — an unconfirmed plan "
                f"cannot execute")
        if task.driver is not None:
            raise GateError(
                f"task {task_id} already started execution with "
                f"driver '{task.driver}'")
        plan = self._bound_plan(task)
        # Staleness: the plan was derived from records as they stood
        # at plan time (its preconditions). If any of those records
        # changed since, executing would write on a basis that no
        # longer exists — refuse before anything runs, name the
        # records, and park the task in STALE for a re-plan.
        changed = stale_records(self.store, plan)
        if changed:
            task.status = TaskStatus.STALE
            task.stale_records = changed
            self._save(task)
            self._audit(task_id, task.operator, "execution_stale", {
                "plan_version": plan.version,
                "changed_records": changed})
            raise GateError(
                f"task {task_id} is stale: {', '.join(changed)} "
                f"changed since plan v{plan.version} was derived; "
                f"execution refused — re-plan against the records "
                f"as they now stand")
        task.driver = driver
        self._save(task)
        if driver == DRIVER_LIVE:
            # The agent runs in the operator's browser; the bridge
            # reports each step back. Verification runs when the
            # final step lands (see report_step).
            self._audit(task_id, task.operator, "execution_started", {
                "driver": driver, "plan_version": plan.version,
                "steps": len(plan.steps)})
            return task
        if driver != DRIVER_OFFLINE:
            raise GateError(f"unknown driver '{driver}'")
        runner = OfflineDriver(tamper=tamper)
        outcomes: list[StepOutcome] = []
        created_ids: dict[int, str] = {}
        for step in plan.steps:
            outcome = runner.run_step(self.store, task, plan, step)
            outcomes.append(outcome)
            if outcome.after.get("id"):
                created_ids[step.seq] = str(outcome.after["id"])
            self._audit(task_id, driver, "step_executed", {
                "seq": step.seq, "plan_version": plan.version,
                "before": outcome.before, "after": outcome.after,
                "detail": outcome.detail})
        self._audit(task_id, driver, "execution_completed", {
            "driver": driver, "steps": len(outcomes)})
        return self._verify(task, plan, created_ids)

    def report_step(self, task_id: str, seq: int,
                    report: StepReportIn) -> Task:
        """Record one live step report from the browser bridge.

        The report is advisory: it is written to the audit, and when
        the final step lands the shell verifies against the store —
        the report's own `ok` is never treated as evidence."""
        task = self._require_task(task_id)
        if task.status != TaskStatus.CONFIRMED or task.driver != DRIVER_LIVE:
            raise GateError(
                f"task {task_id} is not awaiting live step reports "
                f"(status {task.status.value}, driver {task.driver})")
        plan = self._bound_plan(task)
        if seq not in {s.seq for s in plan.steps}:
            raise GateError(f"plan for task {task_id} has no step {seq}")
        prior = [e for e in self.audit(task_id)
                 if e["event"] == "step_reported"
                 and e["detail"].get("seq") == seq]
        if prior:
            raise GateError(
                f"step {seq} of task {task_id} was already reported")
        self._audit(task_id, DRIVER_LIVE, "step_reported", {
            "seq": seq, "ok": report.ok, "detail": report.detail,
            "plan_version": plan.version})
        reported = {e["detail"].get("seq") for e in self.audit(task_id)
                    if e["event"] == "step_reported"}
        if reported != {s.seq for s in plan.steps}:
            return task
        self._audit(task_id, DRIVER_LIVE, "execution_completed", {
            "driver": DRIVER_LIVE, "steps": len(plan.steps)})
        created_ids: dict[int, str] = {}
        for memo in self.store.list_records("credit_memos"):
            if memo.get("created_for_task") == task_id:
                for step in plan.steps:
                    if step.record_kind == "credit_memos":
                        created_ids[step.seq] = str(memo["id"])
        return self._verify(task, plan, created_ids)

    # -- verification -------------------------------------------------------
    def _verify(self, task: Task, plan: Plan,
                created_ids: dict[int, str]) -> Task:
        result = verify_plan(self.store, plan, created_ids)
        if result.ok:
            task.status = TaskStatus.VERIFIED
            self._save(task)
            self._audit(task.task_id, "verifier", "verification_passed", {
                "plan_version": plan.version, "checked": result.checked})
        else:
            task.status = TaskStatus.DISCREPANCY
            self._save(task)
            self._audit(task.task_id, "verifier", "verification_failed", {
                "plan_version": plan.version, "checked": result.checked,
                "diffs": [d.model_dump(mode="json") for d in result.diffs]})
        return task
