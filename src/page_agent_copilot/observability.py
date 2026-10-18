"""Observability: the trace, the taxonomy, and the daily digest.

Three reads over the records the shell already keeps — the task,
its plan versions, its execution attempts, its verification
report, and its audit trail. Nothing here writes, counts into a
counter, or asks a driver anything: each read is a fold over
stored records, computed at the moment of asking, so it cannot
drift from what it summarises (the discipline the operator
report and the batch report already follow).

- **The trace** (`build_trace`) reconstructs one task's
  execution timeline: the attempts in order, each attempt's
  events with navigations interleaved where they happened,
  every step's before/after values, and the ending. Durations
  are the attempt's wall clock, read from the started/finished
  stamps the attempt record itself carries. Per-step durations
  are NOT shown: neither driver records when a step started or
  landed — only the bridge's reports and the offline driver's
  outcomes exist, and neither carries a clock — so the trace
  shows no per-step timing rather than inventing one.
- **The taxonomy** (`classify_ending`) names how a task ended,
  or that it has not: ONE function, a documented rule order,
  first match wins. The label rides the task list and detail
  reads as a derived field — never a stored counter, so a
  label can never disagree with the records it was folded
  from.
- **The digest fold** (`fold_digest`) summarises one day: the
  tasks that arrived that day, their endings and taxonomy
  counts, the credits their verification reports prove, the
  clarifications asked and answered, the budget exhaustions,
  and the discrepancies open vs accepted — and, since round 18,
  the day's dual-control tasks and those still awaiting their
  second confirmer, folded from the per-task dual-control
  views the caller passes in. The service stores the fold as
  a dated snapshot record (see `service.py`); the fold itself
  is pure.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Optional

from .models import (
    ExecutionAttempt,
    Plan,
    Task,
    TaskStatus,
    VerificationReport,
)

# The audit events that record a bridge protocol refusal (round
# 12). A protocol refusal never ends an execution — the attempt
# stays running and the bridge may navigate and report again —
# so it is not an ending class in the taxonomy. It is counted
# in the digest as an event, which is what it is.
PROTOCOL_REFUSAL_EVENTS = ("navigation_refused", "step_report_refused")


# ---------------------------------------------------------------- taxonomy

def _refusal_code(task: Task,
                  audit_entries: list[dict]) -> Optional[str]:
    """The code of the task's plan refusal: the last
    `plan_refused` entry on its trail carries it. The fallback
    — the prefix of the reason stored on the task — covers a
    trail written before the entry carried codes."""
    for entry in reversed(audit_entries):
        if entry.get("event") == "plan_refused":
            code = (entry.get("detail") or {}).get("code")
            if code:
                return str(code)
    if task.refusal_reason and ":" in task.refusal_reason:
        return task.refusal_reason.split(":", 1)[0].strip()
    return None


def _failure_detail(task: Task,
                    attempts: list[ExecutionAttempt]) -> dict:
    """How the task's execution failed, from its final attempt:
    the subreason is the budget when the shell stopped that
    attempt at a governed ceiling, and the driver otherwise —
    the failed step's outcome carries the driver's error
    verbatim, mirrored on the task. Protocol refusals are
    absent here by design: they never fail an attempt (round
    12 leaves it running), so no failed attempt is ever owed
    to one."""
    last = attempts[-1] if attempts else None
    return {
        "subreason": ("budget_exhausted"
                      if last is not None and last.budget_exhausted
                      else "driver_error"),
        "attempt": last.number if last is not None else None,
        "failed_step": task.failed_step,
        "error": task.failure_error,
    }


def _diff_detail(report: Optional[VerificationReport]) -> dict:
    """A discrepancy's differences, from its stored report:
    every diff verbatim (field failures and linkage failures
    alike — a linkage failure lands in the diffs under its
    field name, as the verifier writes them), and the linkage
    checks that failed, named separately so a reader can tell
    a wrong value from a broken link."""
    if report is None:
        return {"diffs": [], "linkage_failures": []}
    return {
        "diffs": [d.model_dump(mode="json") for d in report.diffs],
        "linkage_failures": [
            c.check for c in report.linkage_checks
            if c.verdict != "match"],
    }


def classify_ending(task: Task, audit_entries: list[dict],
                    attempts: list[ExecutionAttempt],
                    report: Optional[VerificationReport]) -> dict:
    """The one taxonomy: how this task ended, or that it has
    not. Rules run in this order and the first match wins:

    1. VERIFIED → `verified` (the success the taxonomy is
       measured against; it rides along so every task carries
       exactly one label).
    2. REFUSED whose refusal code is `clarification_exhausted`
       → `clarification_exhausted`: the task was not refused
       for what it asked, but for the questions it took to ask
       it — a different ending, named separately.
    3. REFUSED → `refused_at_plan`, with the governing code:
       policy, role, or planner — the code says which.
    4. DISCREPANCY → `discrepancy`, its differences named from
       the stored report (open: it still owes an exit).
    5. DISCREPANCY_ACCEPTED → `discrepancy_accepted`: the same
       differences, closed by a supervisor's recorded
       acceptance — never rewritten to verified.
    6. STALE → `stale`, the changed records named.
    7. EXECUTION_FAILED → `execution_failed`, subreason
       `budget_exhausted` or `driver_error` (see
       `_failure_detail`).
    8. ABANDONED → `abandoned`: a failed execution an operator
       closed; the failure's detail rides along, and so do
       who closed it and why.
    9. REJECTED → `rejected`: a human refused the plan — a
       decision about the plan, not a policy refusal of the
       task.
    10. Anything else (DRAFT, AWAITING_CLARIFICATION,
        PLANNED, CONFIRMED) has not ended → `in_progress`,
        with the standing status named.
    """
    status = task.status
    if status == TaskStatus.VERIFIED:
        return {"label": "verified", "detail": {}}
    if status == TaskStatus.REFUSED:
        code = _refusal_code(task, audit_entries)
        if code == "clarification_exhausted":
            return {"label": "clarification_exhausted", "detail": {
                "code": code,
                "questions": len(task.clarifications),
                "reason": task.refusal_reason}}
        return {"label": "refused_at_plan", "detail": {
            "code": code, "reason": task.refusal_reason}}
    if status == TaskStatus.DISCREPANCY:
        return {"label": "discrepancy",
                "detail": _diff_detail(report)}
    if status == TaskStatus.DISCREPANCY_ACCEPTED:
        detail = _diff_detail(report)
        detail.update({
            "accepted_by": task.deviation_accepted_by,
            "reason": task.deviation_reason})
        return {"label": "discrepancy_accepted", "detail": detail}
    if status == TaskStatus.STALE:
        return {"label": "stale",
                "detail": {"records": list(task.stale_records)}}
    if status == TaskStatus.EXECUTION_FAILED:
        return {"label": "execution_failed",
                "detail": _failure_detail(task, attempts)}
    if status == TaskStatus.ABANDONED:
        detail = _failure_detail(task, attempts)
        detail.update({"abandoned_by": task.abandoned_by,
                       "reason": task.abandon_reason})
        return {"label": "abandoned", "detail": detail}
    if status == TaskStatus.REJECTED:
        return {"label": "rejected", "detail": {}}
    return {"label": "in_progress",
            "detail": {"status": status.value}}


# ------------------------------------------------------------------- trace

def _duration_seconds(attempt: ExecutionAttempt) -> Optional[float]:
    """The attempt's wall clock, from its own stamps: finished
    minus started. A running attempt has not finished, so it
    has no duration yet — None, not a number measured against
    a clock the record does not carry."""
    if not attempt.started_at or not attempt.finished_at:
        return None
    try:
        started = datetime.fromisoformat(attempt.started_at)
        finished = datetime.fromisoformat(attempt.finished_at)
    except ValueError:
        return None
    return (finished - started).total_seconds()


def _attempt_events(attempt: ExecutionAttempt) -> list[dict]:
    """One attempt's events in the order they happened: its
    steps as recorded, with each navigation placed before the
    step it precedes — the order the attempt record implies
    (a navigation names the step it made way for). A
    navigation naming no recorded step — the attempt failed
    before the step it crossed for — keeps its place at the
    end, in recorded order."""
    events: list[dict] = []
    navigations = list(attempt.navigations)
    for outcome in attempt.steps:
        owed = [n for n in navigations if n.seq == outcome.seq]
        navigations = [n for n in navigations if n.seq != outcome.seq]
        for nav in owed:
            events.append({
                "type": "navigation", "seq": nav.seq,
                "from_page": nav.from_page, "to_page": nav.to_page,
                "driver": nav.driver})
        events.append({
            "type": "step", "seq": outcome.seq, "ok": outcome.ok,
            "driver": outcome.driver,
            "before": outcome.before, "after": outcome.after,
            "detail": outcome.detail})
    for nav in navigations:
        events.append({
            "type": "navigation", "seq": nav.seq,
            "from_page": nav.from_page, "to_page": nav.to_page,
            "driver": nav.driver})
    return events


def build_trace(task: Task, plan: Optional[Plan],
                attempts: list[ExecutionAttempt],
                report: Optional[VerificationReport],
                ending: dict) -> dict:
    """The task's execution timeline, folded from its records:
    the attempts in number order — driver, executor, mode, the
    budget each ran under, its events, its failure and the
    state it left standing — and the ending: the taxonomy's
    verdict, plus the verification report's summary where a
    verification ran. Steps carry their recorded before/after
    values and no timings (see the module docstring)."""
    return {
        "task_id": task.task_id,
        "status": task.status.value,
        "plan_version": plan.version if plan is not None else None,
        "ending": ending,
        "attempts": [{
            "number": a.number,
            "mode": a.mode,
            "driver": a.driver,
            "executor": a.executor,
            "plan_version": a.plan_version,
            "outcome": a.outcome,
            "started_at": a.started_at,
            "finished_at": a.finished_at,
            "duration_seconds": _duration_seconds(a),
            "budget": {
                "step_ceiling": a.step_ceiling,
                "seconds_ceiling": a.seconds_ceiling,
                "steps_consumed": len(a.steps),
                "budget_exhausted": a.budget_exhausted,
                "exhaustion_reason": a.exhaustion_reason},
            "events": _attempt_events(a),
            "failed_seq": a.failed_seq,
            "error": a.error,
            "state_lines": list(a.state_lines),
        } for a in attempts],
        "verification": None if report is None else {
            "ok": report.ok,
            "checked": report.checked,
            "plan_version": report.plan_version,
            "driver": report.driver,
            "read_path": report.read_path,
            "verified_at": report.verified_at,
            "diffs": [d.model_dump(mode="json")
                      for d in report.diffs]},
    }


# ------------------------------------------------------------------ digest

def fold_digest(day: str, tasks: list[Task],
                attempts_by_task: dict[str, list[ExecutionAttempt]],
                reports_by_task: dict[
                    str, Optional[VerificationReport]],
                audits_by_task: dict[str, list[dict]],
                dual_by_task: Optional[dict[str, dict]] = None
                ) -> dict:
    """One day's fold. The caller scopes `tasks` to the day
    (the tasks created on it, in the service's reading) and
    supplies each task's attempts, stored verification report,
    audit trail, and dual-control standing (round 18 — the
    assessment is computed from the plan, which the fold does
    not hold, so the caller passes the derived view in, the way
    it passes the attempts); everything below is counted from
    those records and nothing else.

    The credited total follows the batch report's rule exactly:
    summed from the VERIFIED tasks' stored verification
    reports — the credit-memo `amount` checks the re-read
    passed — never from plans, so a task in DISCREPANCY
    contributes nothing whatever its plan promised."""
    endings = {
        t.task_id: classify_ending(
            t, audits_by_task.get(t.task_id, []),
            attempts_by_task.get(t.task_id, []),
            reports_by_task.get(t.task_id))
        for t in tasks}
    credited: list[dict] = []
    total = Decimal("0.00")
    for task in tasks:
        if task.status != TaskStatus.VERIFIED:
            continue
        report = reports_by_task.get(task.task_id)
        if report is None or not report.ok:
            continue
        task_total = Decimal("0.00")
        for check in report.field_checks:
            if (check.record_kind == "credit_memos"
                    and check.field == "amount"
                    and check.verdict == "match"
                    and check.actual is not None):
                task_total += Decimal(check.actual)
        if task_total > 0:
            credited.append({"task_id": task.task_id,
                             "credited": str(task_total)})
            total += task_total
    clarifications = [c for t in tasks for c in t.clarifications]
    exhausted = [a for t in tasks
                 for a in attempts_by_task.get(t.task_id, [])
                 if a.budget_exhausted]
    return {
        "date": day,
        "tasks_total": len(tasks),
        "counts_by_status": dict(sorted(Counter(
            t.status.value for t in tasks).items())),
        "counts_by_taxonomy": dict(sorted(Counter(
            e["label"] for e in endings.values()).items())),
        "refusals_by_code": dict(sorted(Counter(
            e["detail"].get("code") for e in endings.values()
            if e["label"] == "refused_at_plan"
            and e["detail"].get("code")).items())),
        "total_credited": str(total),
        "credited": credited,
        "clarifications": {
            "asked": len(clarifications),
            "answered": sum(1 for c in clarifications
                            if c.answer is not None),
            "awaiting": sum(
                1 for t in tasks
                if t.status == TaskStatus.AWAITING_CLARIFICATION)},
        "budget_exhaustions": {
            "count": len(exhausted),
            "task_ids": sorted({a.task_id for a in exhausted})},
        "discrepancies": {
            "open": [t.task_id for t in tasks
                     if t.status == TaskStatus.DISCREPANCY],
            "accepted": [t.task_id for t in tasks
                         if t.status
                         == TaskStatus.DISCREPANCY_ACCEPTED]},
        "executions": {
            "attempts_run": sum(
                len(attempts_by_task.get(t.task_id, []))
                for t in tasks),
            "steps_consumed": sum(
                len(a.steps) for t in tasks
                for a in attempts_by_task.get(t.task_id, []))},
        "protocol_refusals": sum(
            1 for t in tasks
            for e in audits_by_task.get(t.task_id, [])
            if e.get("event") in PROTOCOL_REFUSAL_EVENTS),
        # Dual control (round 18): the day's plans that stand
        # under dual control, and of those the ones still
        # awaiting their second confirmer — folded from the
        # caller's per-task views, never from a stored counter.
        "dual_control": {
            "required": [t.task_id for t in tasks
                         if (dual_by_task or {}).get(
                             t.task_id, {}).get("required")],
            "awaiting_second_confirmer": [
                t.task_id for t in tasks
                if (dual_by_task or {}).get(
                    t.task_id, {}).get(
                        "awaiting_second_confirmer")]},
    }
