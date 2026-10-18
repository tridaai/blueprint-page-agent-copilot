"""Execution drivers: two drivers, one step interface.

A plan step is executed by exactly one of:

- **offline** — the deterministic offline driver in this module. It
  applies each step to the console's state through the same record
  mutations the console's own forms use. It is the driver tests and
  evals run, and the fallback when no browser session is available.
  It is labelled "offline" in every audit entry it produces; it is
  not PageAgent and never claims to be.
- **page-agent-live** — the real `page-agent` package running in the
  operator's browser on the console page. The browser bridge
  (static/copilot.js) compiles each step's `instruction`, calls
  `agent.execute(...)`, and reports the outcome back; the actual
  state change lands when the form the agent filled is submitted
  through the console's own endpoints. The shell records those
  reports as StepOutcomes through the same path the offline driver
  uses — the shell cannot tell which driver ran a step except by the
  recorded driver name — and verification diffs the store either
  way.

Both paths produce StepOutcome records; audit and verification
consume them identically. A step can also fail — an element the
live agent cannot find, a form the console rejects, a bridge that
times out — and then the outcome carries `ok` False and the
driver's error verbatim. The shell stops the execution at that
step; what happens next (resume, restart, abandon) is the
service's decision, recorded like everything else.
"""

from __future__ import annotations

from typing import Optional, Protocol

from .models import Plan, PlanStep, StepAction, StepOutcome, Task
from .records import apply_field_change, create_credit_memo
from .store import Store

DRIVER_OFFLINE = "offline"
DRIVER_LIVE = "page-agent-live"


class StepDriver(Protocol):
    name: str

    def run_step(self, store: Store, task: Task, plan: Plan,
                 step: PlanStep) -> StepOutcome: ...


class OfflineDriver:
    """The deterministic offline driver.

    `tamper` is a fault-injection hook for the eval pack only: a map
    of step seq -> wrong value to write instead of the planned one,
    so the evals can prove the verifier catches a wrong write and
    reports DISCREPANCY rather than success. Nothing in the running
    service ever passes it.

    `fail` is the companion hook: a map of step seq -> the error
    the step fails with, verbatim (an element not found, a form
    rejected, a bridge timeout — the failures a live driver meets
    in a real console). A failed step writes nothing and returns an
    outcome with `ok` False and the error as its `detail`; the
    shell stops the execution there and records the failure.
    Nothing in the running service passes this either.
    """

    name = DRIVER_OFFLINE

    def __init__(self, tamper: Optional[dict[int, str]] = None,
                 fail: Optional[dict[int, str]] = None) -> None:
        self._tamper = tamper or {}
        self._fail = fail or {}

    def run_step(self, store: Store, task: Task, plan: Plan,
                 step: PlanStep) -> StepOutcome:
        if step.seq in self._fail:
            before: dict = {}
            if step.action == StepAction.SET:
                record = store.get_record(
                    step.record_kind, step.record_id or "")
                if record is not None and step.field:
                    before = {
                        step.field: str(record.get(step.field, ""))}
            return StepOutcome(
                seq=step.seq, driver=self.name, ok=False,
                before=before, after={},
                detail=self._fail[step.seq])
        if step.action == StepAction.SET:
            value = self._tamper.get(step.seq, step.value or "")
            before, after = apply_field_change(
                store, step.record_kind, step.record_id or "",
                step.field or "", value)
            return StepOutcome(
                seq=step.seq, driver=self.name, ok=True,
                before=before, after=after,
                detail=f"set {step.record_kind} {step.record_id}."
                       f"{step.field} to '{value}'")
        record = create_credit_memo(store, step.fields, task.task_id)
        return StepOutcome(
            seq=step.seq, driver=self.name, ok=True,
            before={}, after=dict(record),
            detail=f"created credit memo {record['id']} for invoice "
                   f"{step.fields.get('invoice_id')}")
