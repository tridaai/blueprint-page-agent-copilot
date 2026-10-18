"""Plan diffs: what changed between two versions of a task's plan.

Steps are matched by seq (a plan's steps are never renumbered —
an amendment rebuilds a step in place, a re-plan composes afresh).
For each step present on either side the diff reports it as added,
removed, or changed, with the field-level changes in the plan's own
terms: a SET step diffs on its target and value; a CREATE step
diffs field by field over its field set. Two versions with no step
differences produce an empty diff — the function never invents a
change to have something to say.
"""

from __future__ import annotations

from .models import Plan, PlanChange, PlanDiff, PlanStep, StepDiff


def _step_changes(old: PlanStep, new: PlanStep) -> list[PlanChange]:
    changes: list[PlanChange] = []
    if old.action != new.action:
        changes.append(PlanChange(
            seq=old.seq, field="action",
            old=old.action.value, new=new.action.value))
        return changes  # different shapes; deeper diffing is noise
    # The step's identity: where it acts. A retargeted step is a
    # change an operator must see, so it is diffed, not hidden.
    for label, before, after in (
            ("record", f"{old.record_kind} {old.record_id or ''}".strip(),
             f"{new.record_kind} {new.record_id or ''}".strip()),
            ("page", old.page, new.page)):
        if before != after:
            changes.append(PlanChange(
                seq=old.seq, field=label, old=before, new=after))
    if old.action.value == "set":
        if old.field != new.field:
            changes.append(PlanChange(
                seq=old.seq, field="field", old=old.field, new=new.field))
        if old.value != new.value:
            changes.append(PlanChange(
                seq=old.seq, field=old.field or "value",
                old=old.value, new=new.value))
    else:  # create — field by field over the union of field names
        for name in sorted(set(old.fields) | set(new.fields)):
            before, after = old.fields.get(name), new.fields.get(name)
            if before != after:
                changes.append(PlanChange(
                    seq=old.seq, field=name, old=before, new=after))
    return changes


def diff_plans(old: Plan, new: Plan) -> PlanDiff:
    """The diff from plan version `old` to plan version `new`."""
    old_by_seq = {s.seq: s for s in old.steps}
    new_by_seq = {s.seq: s for s in new.steps}
    step_diffs: list[StepDiff] = []
    for seq in sorted(set(old_by_seq) | set(new_by_seq)):
        before, after = old_by_seq.get(seq), new_by_seq.get(seq)
        if before is None:
            step_diffs.append(StepDiff(seq=seq, change="added"))
        elif after is None:
            step_diffs.append(StepDiff(seq=seq, change="removed"))
        else:
            changes = _step_changes(before, after)
            if changes:
                step_diffs.append(StepDiff(
                    seq=seq, change="changed", changes=changes))
    return PlanDiff(task_id=old.task_id, from_version=old.version,
                    to_version=new.version, step_diffs=step_diffs)
