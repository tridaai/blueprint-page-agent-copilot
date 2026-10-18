"""Plan-time policy: the refusals that happen before any human is
asked to confirm anything.

A refused task never becomes a plan, so no confirmation can ever
approve it. The three R1 rules:

1. **No deletes.** A task that asks to delete or remove a record is
   refused. The copilot changes fields and issues credit memos; it
   does not destroy records.
2. **Credit cap.** A credit memo above the governed cap
   (`Policy.credit_cap`) is refused at plan time.
3. **No payment fields.** A plan may never touch a payment field
   (bank account, card, IBAN, ...) on any record — refused whatever
   the task says.

Plus the allowlist discipline: only governed fields may be planned,
and status fields only take governed values.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .config import Policy
from .models import (
    INVOICE_STATUSES,
    ORDER_STATUSES,
    PAYMENT_FIELDS,
    PLANNABLE_FIELDS,
)


class PlanRefusal(Exception):
    """A task that policy will not let become a plan."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PlanAmbiguous(Exception):
    """Derivation cannot finish: the text matches a governed
    shape in more than one way and the choice is one the records
    can offer (round 14). Not a refusal — the task parks in
    AWAITING_CLARIFICATION carrying these options, and derivation
    re-runs with the chosen value pinned under `key`.

    The options are computed from the records at the moment of
    asking — never invented, never model-written — and the
    question is composed in code from them. A pinned value is
    re-validated against the freshly computed options on every
    re-derivation: the records may have moved, and a pin that no
    longer stands is asked about again, never honoured stale."""

    def __init__(self, code: str, key: str, question: str,
                 options: list[dict]) -> None:
        super().__init__(question)
        self.code = code
        self.key = key
        self.question = question
        self.options = options


def check_field_change(kind: str, field: str, value: str) -> None:
    """Validate one planned field change. Raises PlanRefusal."""
    if field in PAYMENT_FIELDS:
        raise PlanRefusal(
            "payment_field",
            f"refused: '{field}' is a payment field; plans never touch "
            f"payment fields")
    allowed = PLANNABLE_FIELDS.get(kind, frozenset())
    if field not in allowed:
        raise PlanRefusal(
            "field_not_plannable",
            f"refused: '{field}' on {kind} is not a field the copilot "
            f"may plan changes to")
    if kind == "orders" and field == "status" and value not in ORDER_STATUSES:
        raise PlanRefusal(
            "value_not_governed",
            f"refused: order status '{value}' is not a governed status "
            f"({', '.join(sorted(ORDER_STATUSES))})")
    if kind == "invoices" and field == "status" and value not in INVOICE_STATUSES:
        raise PlanRefusal(
            "value_not_governed",
            f"refused: invoice status '{value}' is not a governed status "
            f"({', '.join(sorted(INVOICE_STATUSES))})")
    if kind == "customers" and field == "credit_limit":
        try:
            if Decimal(value) < 0:
                raise PlanRefusal(
                    "value_not_governed",
                    "refused: a credit limit cannot be negative")
        except InvalidOperation:
            raise PlanRefusal(
                "value_not_governed",
                f"refused: credit limit '{value}' is not a number")


def check_credit(amount: Decimal, policy: Policy) -> None:
    """Validate a planned credit memo amount against the cap."""
    if amount <= 0:
        raise PlanRefusal(
            "credit_not_positive",
            f"refused: a credit memo of {amount} is not a positive amount")
    if amount > policy.credit_cap:
        raise PlanRefusal(
            "credit_above_cap",
            f"refused: credit memo of {amount} exceeds the governed cap "
            f"of {policy.credit_cap}; credits above the cap are refused "
            f"at plan time, not gated")
