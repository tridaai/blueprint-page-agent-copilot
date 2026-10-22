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

Since round 14 there is one outcome between a plan and a refusal:
the text matches a governed shape in more than one way and the
choice is one the records can offer — a record described by its
status that matches several records, a credit amount the task
omits where the records hold a governed candidate set, a status
word that prefixes two governed values. Derivation then raises
`PlanAmbiguous` carrying options computed from the records, and
the service parks the task in AWAITING_CLARIFICATION until an
operator picks one; derivation re-runs with the choice pinned.
The classes are defined in code (below, "clarification"), the
question is composed in code from the options, and a value the
text states outright is never re-asked. Everything that is not
one of these classes refuses exactly as it always did.

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
    INVOICE_STATUSES,
    ORDER_STATUSES,
    AmendChangeIn,
    Plan,
    PlanChange,
    PlanStep,
    RecordPrecondition,
    StepAction,
    VerificationReport,
    money,
)
from .policy import (
    PlanAmbiguous,
    PlanRefusal,
    check_credit,
    check_field_change,
)
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
# The cross-page tail (round 12): "... and update the customer's
# email to creditnotes@harbourview.example". The customer is never
# named in the clause — the plan takes the invoice's customer from
# the invoice record, so a credit task can only ever touch the
# customer the credit belongs to.
AND_CUSTOMER_UPDATE_RE = re.compile(
    r"\band\s+update\s+(?:the\s+)?customer(?:'s)?\s+([a-z ]+?)"
    r"\s+to\s+(.+)$", re.I | re.S)
# A customer update asked for inside a credit task in any other
# shape — a named customer, a missing value — matches this and no
# governed tail: refused, never silently dropped.
AND_CUSTOMER_LOOSE_RE = re.compile(
    r"\band\s+update\s+(?:the\s+)?customers?\b", re.I)
# Shipment with its details in one breath: "mark order 1032 as
# shipped, ship date 2026-10-20, carrier DHL" — three SET steps on
# one record, in the governed order status -> ship_date -> carrier.
SHIP_DETAIL_RE = re.compile(
    r"mark\s+order\s+(\d+)\s+as\s+shipped\s*,\s*ship\s+date\s+"
    r"(\d{4}-\d{2}-\d{2})\s*,\s*carrier\s+([A-Za-z][A-Za-z0-9 .&'-]*?)\s*$",
    re.I)
# A ship date or carrier stated in any other shape — a date that is
# not YYYY-MM-DD ("ship date tomorrow"), details out of order —
# matches this and not the governed shape: refused, never planned
# as the bare status change with the details quietly dropped.
SHIP_DETAIL_LOOSE_RE = re.compile(r"\b(?:ship\s+date|carrier)\b", re.I)
CUSTOMER_UPDATE_RE = re.compile(
    r"update\s+customer\s+(\d+)\s+([a-z ]+?)\s+to\s+(.+)$", re.I | re.S)
# A record described by a governed status word instead of named
# by its number (round 14): "raise a credit memo for the open
# invoice". The descriptor word is drawn from the governed status
# set itself, so a description can only ever match records by a
# status they can actually carry.
CREDIT_DESCRIPTOR_RE = re.compile(
    r"\bthe\s+(" + "|".join(sorted(INVOICE_STATUSES)) +
    r")\s+invoice\b", re.I)
# The same description inside a status clause: "mark the packed
# order as shipped". Governed for the clause's record only — a
# descriptor clause stands alone in its task (mixing it with a
# clause that names a record by number is refused whole).
DESCRIPTOR_CLAUSE_RE = re.compile(
    r"(?:mark|set)\s+the\s+([a-z]+)\s+(order|invoice)\s+"
    r"(?:as|to|status\s+to)\s+([a-z]+)", re.I)
# The most options one clarification may carry. A choice between
# more records than this is not a clarification — the task states
# the record by its number instead.
MAX_CLARIFICATION_OPTIONS = 4


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


# ------------------------------------------------- clarification
#
# Round 14: the ambiguity classes, defined here and nowhere else.
# Derivation raises PlanAmbiguous only where the text matches a
# governed shape in more than one way AND the records offer the
# choice — the options are computed from the records at the
# moment of asking, so every value a plan later carries traces
# to the task text, a named record field, or the recorded
# answer. Everywhere else, derivation refuses exactly as before:
# a clarification is never a backdoor around `no_plan_derivable`.

def _fmt_money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _record_label(store: Store, kind: str, record: dict) -> str:
    """How one record reads as a clarification option: its id,
    plus the record fields that distinguish it from the other
    matches — an amount and a date and a customer for an invoice;
    the goods and the total for an order."""
    if kind == "invoices":
        customer = store.get_record(
            "customers", str(record.get("customer_id", "")))
        who = (customer.get("name") if customer
               else record.get("customer_id"))
        return (f"invoice {record['id']} — "
                f"{_fmt_money(money(str(record['amount'])))}, due "
                f"{record.get('due_date', '')}, for {who}")
    return (f"order {record['id']} — {record.get('items', '')}, "
            f"{_fmt_money(money(str(record.get('total', '0'))))}")


def _resolve_record(store: Store, key: str, kind: str, word: str,
                    matches: list[dict],
                    resolutions: dict[str, str]) -> str:
    """Resolve a record described by a governed status word to
    one record id — or raise the clarification that asks.

    `matches` are the records of `kind` carrying status `word`,
    in id order. Zero matches is the ordinary `record_not_found`
    refusal: the description names nothing that is there. Exactly
    one match resolves directly — the choice is derivable from
    the records, so there is nothing to ask. Two to four matches
    raise PlanAmbiguous with the matches as options. More than
    that is refused: a choice that wide is stated by number.

    A pinned answer (`resolutions[key]`) is honoured only while
    it still stands among the fresh matches. A pin the records
    have moved out from under is asked about again with the
    fresh options — never honoured stale, and never silently
    replaced by another record."""
    if not matches:
        raise PlanRefusal(
            "record_not_found",
            f"refused: no {kind[:-1]} with status '{word}' exists; "
            f"a plan is only derived from records that are there")
    ids = [str(m["id"]) for m in matches]
    pin = resolutions.get(key)
    if pin is not None and pin in ids:
        return pin
    if pin is None and len(matches) == 1:
        return ids[0]
    if len(matches) > MAX_CLARIFICATION_OPTIONS:
        raise PlanRefusal(
            "no_plan_derivable",
            f"refused: 'the {word} {kind[:-1]}' matches "
            f"{len(matches)} {kind} — too many to offer as a "
            f"clarification; name the {kind[:-1]} by its number")
    labels = [_record_label(store, kind, m) for m in matches]
    question = (
        f"Which {kind[:-1]} did you mean by 'the {word} "
        f"{kind[:-1]}'? {len(matches)} {kind} currently carry "
        f"status '{word}': " + "; ".join(labels) + ".")
    raise PlanAmbiguous(
        "ambiguous_record", key, question,
        [{"value": rid, "label": label}
         for rid, label in zip(ids, labels)])


def _resolve_status_value(kind: str, record_id: str, raw: str,
                          resolutions: dict[str, str]) -> str:
    """Resolve a status value the task stated. A governed value
    passes through. A value that is a strict prefix of two or
    more governed values for the kind ("p" for an order: placed
    or packed) raises the clarification that asks which one —
    the choice is between governed values, computed from the
    governed set. A value matching zero governed values, or
    prefixing exactly one, is returned as stated and refused by
    `check_field_change` as `value_not_governed`, exactly as
    before: a unique prefix is not a choice, and clarification
    is not a spelling corrector."""
    governed = ORDER_STATUSES if kind == "orders" else INVOICE_STATUSES
    if raw in governed:
        return raw
    matches = sorted(s for s in governed if s.startswith(raw))
    if len(matches) >= 2:
        key = f"status_value:{kind}:{record_id}"
        pin = resolutions.get(key)
        if pin in matches:
            return pin
        question = (
            f"'{raw}' could be {' or '.join(matches)} — which "
            f"status should {kind[:-1]} {record_id} be set to?")
        raise PlanAmbiguous(
            "ambiguous_status", key, question,
            [{"value": m, "label": m} for m in matches])
    return raw


def _amount_candidates(store: Store,
                       invoice: dict) -> list[tuple[str, str]]:
    """The governed candidate set for a credit task that states
    no amount: amounts the records themselves offer for this
    invoice — the invoice's own amount, and the amount of each
    credit memo already issued against it — each labelled with
    where it came from. Distinct values, the invoice's amount
    first, memos in record order. Nothing here is computed from
    anything but the records: no instalment schedule exists on
    these records, so no instalment amounts are offered."""
    candidates: dict[str, str] = {}
    full = money(str(invoice["amount"]))
    candidates[str(full)] = (
        f"the invoice's full amount, {_fmt_money(full)}")
    for memo in store.list_records("credit_memos"):
        if str(memo.get("invoice_id")) != str(invoice["id"]):
            continue
        amount = money(str(memo["amount"]))
        candidates.setdefault(
            str(amount),
            f"the amount of credit memo {memo['id']}, "
            f"{_fmt_money(amount)}")
    return list(candidates.items())


def _credit_reason(text: str, amt: re.Match | None) -> str:
    """The credit memo's reason: the words between "reason" and
    the amount the plan credits. Slicing to that amount — rather
    than stopping at the first $-figure — keeps a figure named
    inside the reason ("refund of $50 overcharge") in the reason,
    where it is data. When the amount precedes the reason in the
    sentence, the reason runs to the end of the text.

    When no figure in the text pins the amount — it came from a
    clarification answer (round 14) — the reason runs to the
    first governed tail clause instead, so a tail spoken after
    the reason ("... and mark invoice 4472 as paid") stays a
    tail and is never swallowed into the reason's words."""
    start = REASON_START_RE.search(text)
    if start is None:
        return ""
    if amt is not None and amt.start() >= start.end():
        raw = text[start.end():amt.start()]
    else:
        end = len(text)
        for tail_re in (AND_INVOICE_STATUS_RE,
                        AND_CUSTOMER_UPDATE_RE,
                        AND_CUSTOMER_LOOSE_RE):
            m = tail_re.search(text, start.end())
            if m:
                end = min(end, m.start())
        raw = text[start.end():end]
    return raw.strip().strip(",").strip()


def _plan_credit_memo(text: str, store: Store, policy: Policy,
                      task_id: str,
                      resolutions: dict[str, str]) -> Plan:
    inv = INVOICE_RE.search(text)
    desc = CREDIT_DESCRIPTOR_RE.search(text)
    if inv and (desc is None or inv.start() <= desc.start()):
        invoice_id = inv.group(1)
    elif desc:
        # The invoice may instead be described by a governed
        # status word — "the open invoice" (round 14). The
        # description resolves against the records: one match
        # derives directly, several ask, none refuses. The
        # credit's own reference is whichever reference the text
        # states FIRST: a settle tail spoken later in the same
        # task ("... and mark invoice 4472 as paid") also names
        # an invoice by number, and that number is the tail's,
        # never the credit's.
        word = desc.group(1).lower()
        matches = sorted(
            (r for r in store.list_records("invoices")
             if r.get("status") == word),
            key=lambda r: str(r["id"]))
        invoice_id = _resolve_record(
            store, "credit_invoice", "invoices", word, matches,
            resolutions)
    else:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: a credit memo task must name its invoice "
            "(e.g. 'invoice 4471') or describe it by a governed "
            "status (e.g. 'the open invoice')")
    invoice = _require_record(store, "invoices", invoice_id)
    amounts = list(AMOUNT_RE.finditer(text))
    amt: re.Match | None = None
    if amounts:
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
    else:
        # No amount stated. The planner still never invents one —
        # but the records may offer a governed candidate set for
        # this invoice (its own amount; the amounts of memos
        # already issued against it), and then the choice is the
        # operator's to make, asked as a clarification (round 14).
        # A pinned answer is honoured while it stands among the
        # fresh candidates. Without an offerable set — a single
        # candidate, or too many — the refusal stands exactly as
        # before: clarification is not a backdoor.
        candidates = _amount_candidates(store, invoice)
        pin = resolutions.get("credit_amount")
        if pin is not None and pin in {v for v, _ in candidates}:
            amount = money(pin)
        elif 2 <= len(candidates) <= MAX_CLARIFICATION_OPTIONS:
            question = (
                f"How much should the credit memo for invoice "
                f"{invoice_id} be? The task states no amount. The "
                f"records offer: "
                + "; ".join(label for _, label in candidates) + ".")
            raise PlanAmbiguous(
                "ambiguous_amount", "credit_amount", question,
                [{"value": v, "label": label}
                 for v, label in candidates])
        else:
            raise PlanRefusal(
                "no_plan_derivable",
                "refused: a credit memo task must state its amount "
                "(e.g. '$120'); the planner never invents an "
                "amount, and the records offer no set of candidate "
                "amounts for this invoice to clarify between")
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
    # The desk's compound asks ride on the memo as governed tail
    # clauses, sequenced after it in the order they were spoken:
    # the invoice settled in the same task ("... and mark invoice
    # 4471 as paid") — a step on a second record — and, round 12,
    # the invoice's customer updated in the same task ("... and
    # update the customer's email to ...") — a step on a second
    # record kind and a second console page, the plan's one
    # genuinely cross-page shape. Each tail is planned and
    # policy-checked like any other step.
    tails: list[tuple[int, str, PlanStep]] = []
    tail = AND_INVOICE_STATUS_RE.search(text)
    if tail:
        follow_id = tail.group(1)
        _require_record(store, "invoices", follow_id)
        follow_value = _resolve_status_value(
            "invoices", follow_id, tail.group(2).lower(), resolutions)
        check_field_change("invoices", "status", follow_value)
        tails.append((tail.start(), "invoice_status",
                      _set_step(0, "invoices", follow_id, "status",
                                follow_value)))
    cust = AND_CUSTOMER_UPDATE_RE.search(text)
    if cust:
        phrase = " ".join(cust.group(1).lower().split())
        field = FIELD_PHRASES.get(phrase)
        if field is None:
            raise PlanRefusal(
                "no_plan_derivable",
                f"refused: '{phrase}' is not a field the planner "
                f"can name; the planner never guesses which field "
                f"was meant")
        # The value runs to the end of the task, or to a settle
        # clause spoken after it — never through one.
        value_end = len(text)
        if tail and tail.start() > cust.start():
            value_end = tail.start()
        value = text[cust.start(2):value_end].strip().rstrip(",").strip()
        if not value:
            raise PlanRefusal(
                "no_plan_derivable",
                "refused: the customer update names no value; the "
                "planner never invents one")
        customer_id = invoice.get("customer_id")
        if not customer_id:
            raise PlanRefusal(
                "no_plan_derivable",
                f"refused: invoice {invoice_id} names no customer; "
                f"there is no customer record for the credit to "
                f"update")
        _require_record(store, "customers", str(customer_id))
        check_field_change("customers", field, value)
        tails.append((cust.start(), "customer_update",
                      _set_step(0, "customers", str(customer_id),
                                field, value)))
    elif AND_CUSTOMER_LOOSE_RE.search(text):
        # A customer update was asked for in a shape the grammar
        # does not govern — a named customer, a missing value.
        # Refuse it whole; dropping the clause quietly would plan
        # less than the operator asked for.
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: a credit task updates the invoice's customer "
            "only in the governed form \"and update the customer's "
            "<field> to <value>\" — the customer is the invoice's "
            "customer, read from the record, never a customer named "
            "in the text")
    tails.sort(key=lambda t: t[0])
    for seq, (_, _, step) in enumerate(tails, start=2):
        step.seq = seq
        steps.append(step)
    type_suffix = "".join(f"_and_{name}" for _, name, _ in tails)
    return Plan(
        task_id=task_id,
        task_type=f"credit_memo{type_suffix}",
        summary=_summarize(steps),
        steps=steps)


def _plan_status(text: str, store: Store, task_id: str,
                 resolutions: dict[str, str]) -> Plan:
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
    if SHIP_DETAIL_LOOSE_RE.search(text):
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: shipment details are planned only in the "
            "governed form \"mark order <id> as shipped, ship date "
            "YYYY-MM-DD, carrier <name>\"; a plan that dropped the "
            "stated date or carrier would plan less than was asked")
    # A record described by its status instead of named by number:
    # "mark the packed order as shipped" (round 14). The clause
    # stands alone in its task — mixing it with a clause that
    # names a record by number is refused whole, never partially
    # planned. The descriptor word must be a governed status for
    # the kind; the record resolves against the records exactly
    # as the credit descriptor does, and the target value passes
    # the same prefix rule as any other status clause.
    desc = DESCRIPTOR_CLAUSE_RE.search(text)
    if desc:
        if STATUS_CLAUSE_RE.search(text):
            raise PlanRefusal(
                "no_plan_derivable",
                "refused: this task mixes a record described by "
                "its status with a record named by its number; "
                "state them as separate tasks")
        word = desc.group(1).lower()
        kind = desc.group(2).lower() + "s"
        governed = (ORDER_STATUSES if kind == "orders"
                    else INVOICE_STATUSES)
        if word not in governed:
            raise PlanRefusal(
                "no_plan_derivable",
                f"refused: '{word}' is not a governed "
                f"{desc.group(2).lower()} status "
                f"({', '.join(sorted(governed))}); a record can "
                f"only be described by a status it can carry")
        matches = sorted(
            (r for r in store.list_records(kind)
             if r.get("status") == word),
            key=lambda r: str(r["id"]))
        record_id = _resolve_record(
            store, f"status_record:{kind}:{word}", kind, word,
            matches, resolutions)
        _require_record(store, kind, record_id)
        value = _resolve_status_value(
            kind, record_id, desc.group(3).lower(), resolutions)
        check_field_change(kind, "status", value)
        step = _set_step(1, kind, record_id, "status", value)
        return Plan(task_id=task_id,
                    task_type=f"{kind[:-1]}_status",
                    summary=_summarize([step]), steps=[step])
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
        value = _resolve_status_value(kind, record_id, value,
                                      resolutions)
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
                task_id: str,
                resolutions: dict[str, str] | None = None) -> Plan:
    """The planner's one entry point. Raises PlanRefusal — or,
    since round 14, PlanAmbiguous when the text matches a
    governed shape in more than one way and the records offer
    the choice (see the clarification section above).

    `resolutions` carries the operator's recorded clarification
    answers, {ambiguity key: chosen value}; derivation consults
    a pin only at its own ambiguity point, and re-validates it
    against the records as they stand now. Text the task states
    outright always wins over a pin — a pin is only ever asked
    about where the text itself is silent or matches twice."""
    resolutions = resolutions or {}
    if DELETE_RE.search(text):
        raise PlanRefusal(
            "delete_refused",
            "refused: the copilot does not delete records; deletes are "
            "refused at plan time, not gated")
    if CREDIT_RE.search(text):
        plan = _plan_credit_memo(text, store, policy, task_id,
                                 resolutions)
    elif (SHIP_DETAIL_RE.search(text) or STATUS_CLAUSE_RE.search(text)
            or DESCRIPTOR_CLAUSE_RE.search(text)):
        plan = _plan_status(text, store, task_id, resolutions)
    elif CUSTOMER_UPDATE_RE.search(text):
        plan = _plan_customer_update(text, store, task_id)
    else:
        raise PlanRefusal(
            "no_plan_derivable",
            "refused: no governed task shape matched this text; the "
            "planner derives plans only for credit memos, order and "
            "invoice status changes, and customer field updates")
    # The plan-size budget, checked here — the one door every
    # derivation passes — so intake, re-plan, template
    # instantiation, and batch members all face it identically.
    # A plan longer than the ceiling is refused at plan time with
    # both counts named: no human is asked to confirm work the
    # shell would not let run to its end.
    if len(plan.steps) > policy.max_plan_steps:
        raise PlanRefusal(
            "plan_too_large",
            f"refused: this task derives a plan of "
            f"{len(plan.steps)} steps, over the governed ceiling "
            f"of {policy.max_plan_steps} plan steps "
            f"(COPILOT_MAX_PLAN_STEPS); state the work as smaller "
            f"tasks")
    return plan


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
