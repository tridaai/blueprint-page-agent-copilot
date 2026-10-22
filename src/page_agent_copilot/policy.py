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

Round 18 adds the gate's stakes rule at the foot of this module
(`dual_control_assessment`): not a refusal — a plan whose total
credit reaches the governed threshold, or that touches a
high-stakes field, is confirmed by two distinct people, not one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from .config import Policy
from .models import (
    INVOICE_STATUSES,
    ORDER_STATUSES,
    PAYMENT_FIELDS,
    PLANNABLE_FIELDS,
    Plan,
    StepAction,
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
    # A ship date is a calendar date, planned and amended alike: a
    # date-shaped value that names no day ("2026-13-45") would land
    # on the record and verify against itself.
    if kind == "orders" and field == "ship_date":
        try:
            valid = date.fromisoformat(value).isoformat() == value
        except ValueError:
            valid = False
        if not valid:
            raise PlanRefusal(
                "value_not_governed",
                f"refused: ship date '{value}' is not a calendar date "
                f"(YYYY-MM-DD)")
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


# ------------------------------------------------------- dual control
# Round 18. The rules above refuse plans; this one scales the
# gate. A plan is under dual control when its stakes reach the
# governed threshold — measured two ways, either sufficient:
#
# * **Amount.** The plan's total credited amount (every credit
#   memo it creates, summed) reaches `Policy.dual_control_cap`,
#   which defaults to the credit cap itself.
# * **Field.** The plan sets a high-stakes field — one whose
#   change EXTENDS money rather than recording work. That set is
#   exactly one field today: a customer's `credit_limit`, the
#   ceiling on what the business will let that customer owe. It
#   is already the supervisor-only field in the role model (an
#   operator may not even plan it); every other plannable field
#   records operational state (a status, a date, a carrier, a
#   contact detail) and stays single-confirmation work.
#
# Under dual control the confirmation gate needs TWO seats: two
# distinct confirm-capable operators, neither of them the
# planner, each confirming the same plan version. The assessment
# is computed from the plan at confirmation time and on every
# read that shows it — never stored on the plan, so it cannot
# drift from the steps it judges.

DUAL_CONTROL_FIELDS: dict[str, frozenset[str]] = {
    "customers": frozenset({"credit_limit"}),
}


def dual_control_assessment(plan: Plan, policy: Policy) -> dict:
    """Judge one plan's stakes: does confirming it take two
    people? Returns the derived fact every surface shows —
    `required`, the `reasons` (any of "amount", "field"),
    the plan's `total_credited`, the `high_stakes_fields` it
    touches (as "kind.field"), and the `threshold` in force.
    Pure: the same plan under the same policy always judges
    the same, whoever asks."""
    total = Decimal("0.00")
    fields: list[str] = []
    for step in plan.steps:
        if step.action == StepAction.CREATE \
                and step.record_kind == "credit_memos":
            raw = step.fields.get("amount")
            if raw:
                try:
                    total += Decimal(raw)
                except InvalidOperation:
                    pass  # an unplannable amount never got here
        if step.action == StepAction.SET and step.field is not None:
            if step.field in DUAL_CONTROL_FIELDS.get(
                    step.record_kind, frozenset()):
                label = f"{step.record_kind}.{step.field}"
                if label not in fields:
                    fields.append(label)
    reasons: list[str] = []
    if total >= policy.dual_control_cap:
        reasons.append("amount")
    if fields:
        reasons.append("field")
    return {
        "required": bool(reasons),
        "reasons": reasons,
        "total_credited": str(total),
        "high_stakes_fields": fields,
        "threshold": str(policy.dual_control_cap),
    }


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
