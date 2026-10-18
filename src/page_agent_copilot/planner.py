"""The planner: an operator's words + the records -> a field-level
plan, or a refusal.

Deterministic by design in R1. The governed task grammar covers the
shapes the desk actually asks for — a credit memo against an
invoice, an order or invoice status change, a customer field update
— and anything outside it is refused as `no_plan_derivable` rather
than guessed at: a plan may never invent a value. (A model-backed
planner can be added behind this same interface later; the plan it
would have to produce is the same stored artifact, checked by the
same policy.)

Every value in the plan comes from the task text or the record;
every step carries the exact instruction the in-page agent will
execute, compiled from the same fields the verifier diffs.
"""

from __future__ import annotations

import re
from decimal import Decimal

from .config import Policy
from .models import Plan, PlanStep, StepAction, money
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
REASON_RE = re.compile(r"reason[:\s]+(.+?)(?=,?\s*\$|,?\s*$)", re.I | re.S)
ORDER_STATUS_RE = re.compile(
    r"(?:mark|set)\s+order\s+(\d+)\s+(?:as|to|status\s+to)\s+([a-z]+)", re.I)
INVOICE_STATUS_RE = re.compile(
    r"(?:mark|set)\s+invoice\s+(\d+)\s+(?:as|to|status\s+to)\s+([a-z]+)", re.I)
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


def _require_record(store: Store, kind: str, record_id: str) -> dict:
    record = store.get_record(kind, record_id)
    if record is None:
        raise PlanRefusal(
            "record_not_found",
            f"refused: {kind[:-1]} {record_id} does not exist; a plan "
            f"is only derived from records that are there")
    return record


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
    amt = AMOUNT_RE.search(text)
    if not amt:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: a credit memo task must state its amount "
            "(e.g. '$120'); the planner never invents an amount")
    amount = money(amt.group(1).replace(",", ""))
    check_credit(amount, policy)
    rsn = REASON_RE.search(text)
    reason = rsn.group(1).strip().strip(",").strip() if rsn else ""
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
    step = PlanStep(
        seq=1, action=StepAction.CREATE, page="Invoices",
        record_kind="credit_memos", fields=fields,
        instruction=(
            f"Open the Invoices screen, open invoice {invoice_id}, open "
            f"the credit memo form, set 'Reason' to '{reason}' and "
            f"'Amount' to '{amount}', and submit the form to issue the "
            f"credit memo."))
    return Plan(
        task_id=task_id, task_type="credit_memo",
        summary=f"Issue a credit memo of {amount} against invoice "
                f"{invoice_id} (reason: {reason})",
        steps=[step])


def _plan_status(text: str, store: Store, task_id: str) -> Plan:
    m = ORDER_STATUS_RE.search(text)
    kind = "orders"
    if not m:
        m = INVOICE_STATUS_RE.search(text)
        kind = "invoices"
    if not m:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: no governed task shape matched this text; the "
            "planner derives plans only for credit memos, order and "
            "invoice status changes, and customer field updates")
    record_id, value = m.group(1), m.group(2).lower()
    _require_record(store, kind, record_id)
    check_field_change(kind, "status", value)
    step = _set_step(1, kind, record_id, "status", value)
    return Plan(
        task_id=task_id,
        task_type=f"{kind[:-1]}_status",
        summary=f"Set {kind[:-1]} {record_id} status to '{value}'",
        steps=[step])


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
    if ORDER_STATUS_RE.search(text) or INVOICE_STATUS_RE.search(text):
        return _plan_status(text, store, task_id)
    if CUSTOMER_UPDATE_RE.search(text):
        return _plan_customer_update(text, store, task_id)
    raise PlanRefusal(
        "no_plan_derivable",
        "refused: no governed task shape matched this text; the "
        "planner derives plans only for credit memos, order and "
        "invoice status changes, and customer field updates")
