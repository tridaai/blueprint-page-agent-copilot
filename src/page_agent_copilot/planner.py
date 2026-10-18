"""The planner: an operator's words + the records -> a field-level
plan, or a refusal.

Deterministic by design. The governed task grammar covers the
shapes the desk actually asks for — a credit memo against an
invoice, an order or invoice status change, a customer field
update — and, since R2, the multi-record shapes: an order shipped
with its ship date and carrier (three steps, one record), a credit
memo with an invoice-status follow-up (two records), and compound
status changes naming two records in one sentence. Anything
outside the grammar is refused as `no_plan_derivable` rather than
guessed at: a plan may never invent a value. (A model-backed
planner can be added behind this same interface later; the plan it
would have to produce is the same stored artifact, checked by the
same policy.)

Every value in the plan comes from the task text or the record;
every step carries the exact instruction the in-page agent will
execute, compiled from the same fields the verifier diffs. Step
order in the plan is the execution order — ordering lives here,
in the composed list, never in prose.

This module also owns the two operations that work on an existing
plan: capturing its preconditions (the record state it was derived
from) and amending its steps (every amended value passes the same
policy checks a fresh plan would).
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from .config import Policy
from .models import (
    AmendChangeIn,
    Plan,
    PlanChange,
    PlanStep,
    RecordPrecondition,
    StepAction,
    VerificationReport,
    money,
)
from .policy import PlanRefusal, check_credit, check_field_change
from .store import Store

FIELD_LABELS = {
    "name": "Name", "email": "Email", "phone": "Phone",
    "address": "Address", "city": "City", "postcode": "Postcode",
    "credit_limit": "Credit limit", "status": "Status",
    "ship_date": "Ship date", "carrier": "Carrier",
    "due_date": "Due date", "memo": "Memo",
}

PAGE_FOR_KIND = {
    "customers": "Customers",
    "invoices": "Invoices",
    "orders": "Orders",
    "credit_memos": "Invoices",
}

# Operator phrasing -> governed field name.
FIELD_PHRASES = {
    "phone": "phone", "phone number": "phone", "telephone": "phone",
    "email": "email", "email address": "email", "e-mail": "email",
    "address": "address", "street address": "address",
    "city": "city", "town": "city", "postcode": "postcode",
    "zip": "postcode", "postal code": "postcode",
    "name": "name", "credit limit": "credit_limit",
    "bank account": "bank_account", "bank details": "bank_account",
    "card": "card_number", "card number": "card_number",
}

DELETE_RE = re.compile(r"\b(delete|remove|erase|destroy)\b", re.I)
CREDIT_RE = re.compile(r"credit\s+memo", re.I)
INVOICE_RE = re.compile(r"invoice\s+(\d+)", re.I)
AMOUNT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
REASON_START_RE = re.compile(r"reason[:\s]+", re.I)
# One status clause: "mark order 1032 as shipped". A task may carry
# several (a compound plan); finditer collects them in text order.
STATUS_CLAUSE_RE = re.compile(
    r"(?:mark|set)\s+(order|invoice)\s+(\d+)\s+"
    r"(?:as|to|status\s+to)\s+([a-z]+)", re.I)
# The trailing follow-up on a credit memo task: "..., $120 and mark
# invoice 4471 as paid".
AND_INVOICE_STATUS_RE = re.compile(
    r"\band\s+(?:mark|set)\s+invoice\s+(\d+)\s+"
    r"(?:as|to|status\s+to)\s+([a-z]+)", re.I)
# Shipment with its details in one breath: "mark order 1032 as
# shipped, ship date 2026-10-20, carrier DHL" — three SET steps on
# one record, in the governed order status -> ship_date -> carrier.
SHIP_DETAIL_RE = re.compile(
    r"mark\s+order\s+(\d+)\s+as\s+shipped\s*,\s*ship\s+date\s+"
    r"(\d{4}-\d{2}-\d{2})\s*,\s*carrier\s+([A-Za-z][A-Za-z0-9 .&'-]*?)\s*$",
    re.I)
CUSTOMER_UPDATE_RE = re.compile(
    r"update\s+customer\s+(\d+)\s+([a-z ]+?)\s+to\s+(.+)$", re.I | re.S)


def _set_step(seq: int, kind: str, record_id: str, field: str,
              value: str) -> PlanStep:
    label = FIELD_LABELS.get(field, field)
    page = PAGE_FOR_KIND[kind]
    return PlanStep(
        seq=seq, action=StepAction.SET, page=page, record_kind=kind,
        record_id=record_id, field=field, value=value,
        instruction=(
            f"Open the {page} screen, open record {record_id}, set the "
            f"field labelled '{label}' to '{value}', and save the form."))


def _credit_step(seq: int, fields: dict[str, str]) -> PlanStep:
    """The credit-memo CREATE step, compiled from its field set.
    Planning and amendment both build it here, so an amended step's
    instruction is compiled exactly the way a planned step's is."""
    return PlanStep(
        seq=seq, action=StepAction.CREATE, page="Invoices",
        record_kind="credit_memos", fields=dict(fields),
        instruction=(
            f"Open the Invoices screen, open invoice "
            f"{fields['invoice_id']}, open the credit memo form, set "
            f"'Reason' to '{fields['reason']}' and 'Amount' to "
            f"'{fields['amount']}', and submit the form to issue the "
            f"credit memo."))


def _step_summary(step: PlanStep) -> str:
    if step.action == StepAction.CREATE:
        f = step.fields
        return (f"Issue a credit memo of {f['amount']} against invoice "
                f"{f['invoice_id']} (reason: {f['reason']})")
    return (f"Set {step.record_kind[:-1]} {step.record_id} "
            f"{step.field} to '{step.value}'")


def _summarize(steps: list[PlanStep]) -> str:
    return "; ".join(_step_summary(s) for s in steps)


# Public alias: the service re-summarizes a plan after an amendment
# from exactly the steps the amendment produced.
summarize_steps = _summarize


def _require_record(store: Store, kind: str, record_id: str) -> dict:
    record = store.get_record(kind, record_id)
    if record is None:
        raise PlanRefusal(
            "record_not_found",
            f"refused: {kind[:-1]} {record_id} does not exist; a plan "
            f"is only derived from records that are there")
    return record


def _credit_reason(text: str, amt: re.Match) -> str:
    """The credit memo's reason: the words between "reason" and
    the amount the plan credits. Slicing to that amount — rather
    than stopping at the first $-figure — keeps a figure named
    inside the reason ("refund of $50 overcharge") in the reason,
    where it is data. When the amount precedes the reason in the
    sentence, the reason runs to the end of the text."""
    start = REASON_START_RE.search(text)
    if start is None:
        return ""
    if amt.start() >= start.end():
        raw = text[start.end():amt.start()]
    else:
        raw = text[start.end():]
    return raw.strip().strip(",").strip()


def _plan_credit_memo(text: str, store: Store, policy: Policy,
                      task_id: str) -> Plan:
    inv = INVOICE_RE.search(text)
    if not inv:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: a credit memo task must name its invoice "
            "(e.g. 'invoice 4471')")
    invoice_id = inv.group(1)
    _require_record(store, "invoices", invoice_id)
    amounts = list(AMOUNT_RE.finditer(text))
    if not amounts:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: a credit memo task must state its amount "
            "(e.g. '$120'); the planner never invents an amount")
    # The amount is the task's LAST figure. Every governed shape
    # states it after the reason ("reason damaged goods, $120"),
    # so a figure named inside the reason ("refund of $50
    # overcharge, $120") is reason text — data about the credit,
    # never the amount. Reading the first figure instead let the
    # reason silently redirect the money: the round-10 red team
    # found that text planning a $50 memo under a stated $120 (and
    # a stated $900 slipping under the cap behind a $50).
    amt = amounts[-1]
    amount = money(amt.group(1).replace(",", ""))
    check_credit(amount, policy)
    reason = _credit_reason(text, amt)
    if not reason:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: a credit memo task must state its reason "
            "(e.g. 'reason damaged goods'); the planner never invents one")
    fields = {
        "invoice_id": invoice_id,
        "amount": str(amount),
        "reason": reason,
        "status": "issued",
    }
    steps = [_credit_step(1, fields)]
    # The desk's compound ask: the memo, and then the invoice is
    # settled in the same task ("... and mark invoice 4471 as
    # paid"). The follow-up is a step on a second record, planned
    # and policy-checked like any other, sequenced after the memo.
    tail = AND_INVOICE_STATUS_RE.search(text)
    if tail:
        follow_id, follow_value = tail.group(1), tail.group(2).lower()
        _require_record(store, "invoices", follow_id)
        check_field_change("invoices", "status", follow_value)
        steps.append(_set_step(2, "invoices", follow_id, "status",
                               follow_value))
    return Plan(
        task_id=task_id,
        task_type=("credit_memo" if len(steps) == 1
                   else "credit_memo_and_invoice_status"),
        summary=_summarize(steps),
        steps=steps)


def _plan_status(text: str, store: Store, task_id: str) -> Plan:
    # Shipment with details: three governed steps on the one record,
    # in the order the desk performs them.
    ship = SHIP_DETAIL_RE.search(text)
    if ship:
        order_id, ship_date, carrier = (
            ship.group(1), ship.group(2), ship.group(3).strip())
        _require_record(store, "orders", order_id)
        steps = []
        for seq, (field, value) in enumerate(
                [("status", "shipped"), ("ship_date", ship_date),
                 ("carrier", carrier)], start=1):
            check_field_change("orders", field, value)
            steps.append(_set_step(seq, "orders", order_id, field, value))
        return Plan(task_id=task_id, task_type="order_ship_details",
                    summary=_summarize(steps), steps=steps)
    # One status clause is the R1 shape; several compose into one
    # multi-record plan, steps in the order the clauses were spoken.
    clauses = [(m.group(1).lower() + "s", m.group(2),
                m.group(3).lower())
               for m in STATUS_CLAUSE_RE.finditer(text)]
    if not clauses:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: no governed task shape matched this text; the "
            "planner derives plans only for credit memos, order and "
            "invoice status changes, and customer field updates")
    seen: set[tuple[str, str]] = set()
    steps = []
    for seq, (kind, record_id, value) in enumerate(clauses, start=1):
        if (kind, record_id) in seen:
            raise PlanRefusal(
                "no_plan_derivable",
                f"refused: this task names {kind[:-1]} {record_id} "
                f"twice; a plan sets each record's status once — "
                f"split it into two tasks")
        seen.add((kind, record_id))
        _require_record(store, kind, record_id)
        check_field_change(kind, "status", value)
        steps.append(_set_step(seq, kind, record_id, "status", value))
    return Plan(
        task_id=task_id,
        task_type=(f"{clauses[0][0][:-1]}_status" if len(steps) == 1
                   else "status_changes"),
        summary=_summarize(steps),
        steps=steps)


def _plan_customer_update(text: str, store: Store, task_id: str) -> Plan:
    m = CUSTOMER_UPDATE_RE.search(text)
    if not m:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: no governed task shape matched this text; the "
            "planner derives plans only for credit memos, order and "
            "invoice status changes, and customer field updates")
    customer_id, phrase, value = m.group(1), m.group(2).strip().lower(), m.group(3).strip()
    field = FIELD_PHRASES.get(phrase)
    if field is None:
        raise PlanRefusal(
            "no_plan_derivable",
            f"refused: '{phrase}' is not a field the planner can name; "
            f"the planner never guesses which field was meant")
    _require_record(store, "customers", customer_id)
    check_field_change("customers", field, value)
    step = _set_step(1, "customers", customer_id, field, value)
    return Plan(
        task_id=task_id, task_type="customer_update",
        summary=f"Set customer {customer_id} {field} to '{value}'",
        steps=[step])


def derive_plan(text: str, store: Store, policy: Policy,
                task_id: str) -> Plan:
    """The planner's one entry point. Raises PlanRefusal."""
    if DELETE_RE.search(text):
        raise PlanRefusal(
            "delete_refused",
            "refused: the copilot does not delete records; deletes are "
            "refused at plan time, not gated")
    if CREDIT_RE.search(text):
        return _plan_credit_memo(text, store, policy, task_id)
    if SHIP_DETAIL_RE.search(text) or STATUS_CLAUSE_RE.search(text):
        return _plan_status(text, store, task_id)
    if CUSTOMER_UPDATE_RE.search(text):
        return _plan_customer_update(text, store, task_id)
    raise PlanRefusal(
        "no_plan_derivable",
        "refused: no governed task shape matched this text; the "
        "planner derives plans only for credit memos, order and "
        "invoice status changes, and customer field updates")


# ------------------------------------------------- corrective plans

def build_corrective_steps(plan: Plan,
                           report: VerificationReport) -> list[PlanStep]:
    """Derive the steps of a corrective plan from a verification
    report: only the steps whose checks failed, carrying the
    parent plan's values — the corrective closes the named
    differences, nothing else.

    What a plan can close, and what it cannot:

    - A SET step whose field check (or execution-record check)
      failed is re-run as planned: setting the field to the
      planned value is a governed act.
    - A CREATE step whose record was never created is re-issued
      as planned.
    - A CREATE step whose record WAS created with wrong values
      cannot be closed by any plan: a credit memo is issued whole
      and is never edited by a plan, and issuing a second one
      would leave two memos where the plan wanted one fact. The
      derivation refuses (`corrective_cannot_close`), naming the
      step and the fields — the discrepancy's remaining exit is a
      supervisor accepting the deviation, on the record.
    - A linkage failure on a field no plan may set (a touched
      record's `customer_id` no longer naming an existing customer)
      is likewise unclosable by a plan, and refuses the same way.

    Raises PlanRefusal; nothing is built halfway."""
    field_failed: dict[int, list[str]] = {}
    for check in report.field_checks:
        if check.verdict != "match":
            field_failed.setdefault(check.seq, []).append(check.field)
    linkage_failed: dict[int, list] = {}
    for check in report.linkage_checks:
        if check.verdict != "match":
            linkage_failed.setdefault(check.seq, []).append(check)

    steps: list[PlanStep] = []
    for step in plan.steps:
        failed_fields = field_failed.get(step.seq, [])
        failed_links = linkage_failed.get(step.seq, [])
        if not failed_fields and not failed_links:
            continue
        if step.action == StepAction.SET:
            blockers = [c for c in failed_links
                        if c.check == "customer_link"]
            if blockers:
                raise PlanRefusal(
                    "corrective_cannot_close",
                    f"refused: step {step.seq}'s linkage failure "
                    f"names '{blockers[0].field}' on "
                    f"{step.record_kind} {step.record_id}, a field "
                    f"no plan may set; no corrective plan can close "
                    f"it")
            steps.append(_set_step(
                len(steps) + 1, step.record_kind,
                step.record_id or "", step.field or "",
                step.value or ""))
            continue
        # CREATE: closable only if the record was never created.
        created = any(
            c.seq == step.seq and c.record_id != "(not created)"
            for c in report.field_checks)
        if created:
            names = failed_fields + [c.field for c in failed_links]
            raise PlanRefusal(
                "corrective_cannot_close",
                f"refused: step {step.seq} created "
                f"{step.record_kind[:-1]} with differing fields "
                f"({', '.join(names)}); a created record is never "
                f"edited by a plan, so no corrective plan can close "
                f"this difference")
        steps.append(_credit_step(len(steps) + 1, dict(step.fields)))
    if not steps:
        raise PlanRefusal(
            "corrective_cannot_close",
            "refused: the verification report names no difference "
            "a corrective plan can close")
    return steps


# ------------------------------------------------------- preconditions

def record_hash(record: dict) -> str:
    """A short content hash of a record as it stands. The canonical
    JSON (sorted keys) means the same record hashes the same on
    every store implementation."""
    blob = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _step_targets(step: PlanStep) -> list[tuple[str, str]]:
    """The existing records a step's derivation rests on: a SET
    step's record; for a CREATE, the record the create is derived
    from (a credit memo rests on its invoice)."""
    if step.action == StepAction.SET and step.record_id:
        return [(step.record_kind, step.record_id)]
    if step.action == StepAction.CREATE:
        invoice_id = step.fields.get("invoice_id")
        if invoice_id:
            return [("invoices", invoice_id)]
    return []


# Public alias: the service's resume check asks which records a
# step rests on, to re-check exactly those preconditions.
step_targets = _step_targets


def capture_preconditions(store: Store,
                          plan: Plan) -> list[RecordPrecondition]:
    """Hash every record the plan touches, as it stands now. Called
    when a plan version is created (intake, amendment, re-plan), so
    each version carries the basis it was actually derived from."""
    seen: dict[tuple[str, str], RecordPrecondition] = {}
    for step in plan.steps:
        for kind, record_id in _step_targets(step):
            if (kind, record_id) in seen:
                continue
            record = store.get_record(kind, record_id)
            seen[(kind, record_id)] = RecordPrecondition(
                record_kind=kind, record_id=record_id,
                hash=record_hash(record) if record else "missing")
    return list(seen.values())


def stale_records(store: Store, plan: Plan) -> list[str]:
    """The plan's precondition records that no longer hash to what
    the plan was derived from — named "kind id", in plan order.
    An empty list means the plan's basis is intact."""
    changed: list[str] = []
    for pre in plan.preconditions:
        record = store.get_record(pre.record_kind, pre.record_id)
        current = record_hash(record) if record else "missing"
        if current != pre.hash:
            changed.append(f"{pre.record_kind} {pre.record_id}")
    return changed


# --------------------------------------------------------- amendments

def _check_amended_create_field(field: str, value: str,
                                  policy: Policy, store: Store) -> str:
    """Validate one amended credit-memo field with the same checks
    planning applies, and return the value in the form the planner
    itself would have stored. Raises PlanRefusal."""
    if field == "amount":
        try:
            amount = money(value)
        except (InvalidOperation, ValueError):
            raise PlanRefusal(
                "value_not_governed",
                f"refused: credit memo amount '{value}' is not an "
                f"amount")
        check_credit(amount, policy)
        return str(amount)
    if field == "invoice_id":
        _require_record(store, "invoices", value)
        return value
    if field == "reason":
        if not value.strip():
            raise PlanRefusal(
                "value_not_governed",
                "refused: a credit memo reason cannot be amended to "
                "empty; the plan never carries a blank reason")
        return value
    if field == "status":
        if value != "issued":
            raise PlanRefusal(
                "value_not_governed",
                f"refused: a credit memo's status is governed — a "
                f"plan creates it as 'issued', and '{value}' is not "
                f"available to an amendment")
        return value
    raise PlanRefusal(
        "field_not_plannable",
        f"refused: '{field}' is not a field of the credit memo step")


def amend_steps(plan: Plan, changes: list[AmendChangeIn],
                policy: Policy,
                store: Store) -> tuple[list[PlanStep], list[PlanChange]]:
    """Apply amendment changes to a plan's steps.

    Every new value passes the same policy checks planning applies
    (check_field_change for SET steps; the credit checks for a
    memo's fields). Steps are rebuilt through the same builders the
    planner uses, so an amended step's instruction is compiled from
    its amended fields. Returns (new steps, recorded changes);
    a change that restates the existing value records nothing.
    Raises PlanRefusal — the plan is left untouched."""
    steps = [s.model_copy(deep=True) for s in plan.steps]

    def replace(step: PlanStep) -> None:
        for i, existing in enumerate(steps):
            if existing.seq == step.seq:
                steps[i] = step
                return

    recorded: list[PlanChange] = []
    for change in changes:
        step = next((s for s in steps if s.seq == change.seq), None)
        if step is None:
            raise PlanRefusal(
                "step_not_found",
                f"refused: plan v{plan.version} has no step "
                f"{change.seq} to amend")
        if step.action == StepAction.SET:
            if change.fields:
                raise PlanRefusal(
                    "invalid_amendment",
                    f"refused: step {change.seq} sets one field "
                    f"('{step.field}'); amend it with a value, not "
                    f"a field set")
            if change.value is None:
                raise PlanRefusal(
                    "invalid_amendment",
                    f"refused: the amendment to step {change.seq} "
                    f"carries no new value")
            check_field_change(step.record_kind, step.field or "",
                               change.value)
            if change.value != step.value:
                recorded.append(PlanChange(
                    seq=step.seq, field=step.field or "",
                    old=step.value, new=change.value))
                replace(_set_step(step.seq, step.record_kind,
                                  step.record_id or "",
                                  step.field or "", change.value))
        else:  # CREATE — the record is amended field by field
            if change.value is not None:
                raise PlanRefusal(
                    "invalid_amendment",
                    f"refused: step {change.seq} creates a record "
                    f"as a whole; amend it with fields, not a "
                    f"single value")
            if not change.fields:
                raise PlanRefusal(
                    "invalid_amendment",
                    f"refused: the amendment to step {change.seq} "
                    f"carries no field changes")
            new_fields = dict(step.fields)
            for fname, fvalue in change.fields.items():
                if fname not in step.fields:
                    raise PlanRefusal(
                        "field_not_plannable",
                        f"refused: '{fname}' is not a field of the "
                        f"credit memo step {change.seq}")
                checked = _check_amended_create_field(
                    fname, fvalue, policy, store)
                if checked != step.fields[fname]:
                    recorded.append(PlanChange(
                        seq=step.seq, field=fname,
                        old=step.fields[fname], new=checked))
                    new_fields[fname] = checked
            replace(_credit_step(step.seq, new_fields))
    return steps, recorded
