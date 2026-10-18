"""The governed task shell: intake -> plan -> confirm -> execute ->
verify, with the audit written as it goes.

The state machine is enforced here, in code, against the store —
not by the API layer and never by the agent:

    DRAFT -> PLANNED -> CONFIRMED -> VERIFIED | DISCREPANCY
    DRAFT -> REFUSED            (plan-time policy refusal)

Executing an unconfirmed plan raises GateError (the API maps it to
HTTP 409). A task whose post-state does not match its approved plan
exactly lands in DISCREPANCY with the differing fields named; no
path reports it as a success.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

from .config import Settings
from .drivers import DRIVER_LIVE, DRIVER_OFFLINE, OfflineDriver
from .models import (
    AuditEntry,
    Plan,
    StepOutcome,
    StepReportIn,
    Task,
    TaskStatus,
)
from .planner import derive_plan
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

    def get_plan(self, task_id: str) -> Optional[Plan]:
        raw = self.store.latest_plan(task_id)
        return Plan(**raw) if raw else None

    def _require_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise GateError(f"task {task_id} does not exist")
        return task

    def _save(self, task: Task) -> Task:
        task.updated_at = _now()
        self.store.put_task(task.model_dump(mode="json"))
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
        self._save(task)
        self._audit(task_id, operator, "plan_confirmed", {
            "plan_version": task.plan_version})
        return task

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
        plan = self.get_plan(task_id)
        assert plan is not None
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
        plan = self.get_plan(task_id)
        assert plan is not None
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
