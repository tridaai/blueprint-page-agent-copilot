"""Verification in code: the plan is the contract, the store is the
evidence.

After execution — by either driver — the shell re-reads the record
state and diffs it against the approved plan field by field. The
agent's own report of what it did is never consulted here. An exact
match is the only path to VERIFIED; every mismatch is named in a
FieldDiff and the task lands in DISCREPANCY.
"""

from __future__ import annotations

from .models import FieldDiff, Plan, StepAction, VerificationResult
from .store import Store


def verify_plan(store: Store, plan: Plan,
                created_ids: dict[int, str]) -> VerificationResult:
    diffs: list[FieldDiff] = []
    checked = 0
    for step in plan.steps:
        if step.action == StepAction.SET:
            checked += 1
            record = store.get_record(step.record_kind, step.record_id or "")
            actual = None
            if record is not None and step.field is not None:
                actual = str(record.get(step.field, ""))
            if actual != step.value:
                diffs.append(FieldDiff(
                    record_kind=step.record_kind,
                    record_id=step.record_id or "",
                    field=step.field or "",
                    planned=step.value, actual=actual))
        elif step.action == StepAction.CREATE:
            created_id = created_ids.get(step.seq)
            record = (store.get_record(step.record_kind, created_id)
                      if created_id else None)
            for field, planned in sorted(step.fields.items()):
                checked += 1
                actual = str(record.get(field, "")) if record else None
                if actual != planned:
                    diffs.append(FieldDiff(
                        record_kind=step.record_kind,
                        record_id=created_id or "(not created)",
                        field=field, planned=planned, actual=actual))
    return VerificationResult(ok=not diffs, diffs=diffs, checked=checked)
