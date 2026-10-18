"""The spoken form: a plan rendered for the ear, and the
transcript envelope normalised for the grammar (round 13).

Accessibility here is a view and a door, never a second shell.
An operator who works by voice still works through the same
planner, the same gate, and the same verification — what changes
is the shape of the text at the edges:

- **The spoken form of a plan** is a deterministic rendering of
  the stored plan, produced in code at the moment of asking:
  steps in plan order, figures spelled as words, dates spoken,
  field names in their plain labels. It is a *view* — asking for
  it stores nothing and changes nothing, and its hash is taken
  over the rendering, so a read-back confirmation can bind the
  confirmer to the exact words they heard (see
  `service.confirm_readback`). Blueprint #11's channel discipline
  is the pattern: the stored artifact keeps its digits; the
  spoken form exists only where it is served.
- **Transcript intake** normalises the envelope a speech
  recogniser wraps a dictation in — a speaker label at the head
  of a line ("Operator: ..."), a timestamp marker — and hands
  the cleaned text to the ordinary intake, where the governed
  grammar applies unchanged. The shell does not grow a speech
  recogniser, and it does not convert spelled-out numbers:
  record ids and amounts must arrive as digits, because a word
  converter guessing at "forty four seventy one" could plan
  against the wrong record. A transcript whose numbers are
  words is underivable text, refused like any other — the
  honest boundary, stated in the README.
"""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

from .models import Plan, PlanStep, StepAction
from .planner import FIELD_LABELS

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "thirteen",
    "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen",
]
_TENS = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty",
    "seventy", "eighty", "ninety",
]
_ORDINALS = [
    "", "first", "second", "third", "fourth", "fifth", "sixth",
    "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
    "thirteenth", "fourteenth", "fifteenth", "sixteenth",
    "seventeenth", "eighteenth", "nineteenth", "twentieth",
    "twenty-first", "twenty-second", "twenty-third",
    "twenty-fourth", "twenty-fifth", "twenty-sixth",
    "twenty-seventh", "twenty-eighth", "twenty-ninth",
    "thirtieth", "thirty-first",
]
_MONTHS = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November",
    "December",
]

_DATE_FIELDS = frozenset({"ship_date", "due_date"})
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def number_words(value: int) -> str:
    """An integer as words: 120 -> "one hundred and twenty".
    Bounded to the figures a plan carries (amounts, limits,
    counts); anything larger falls back to its digits."""
    if value < 0 or value > 999_999:
        return str(value)
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, rest = divmod(value, 10)
        return _TENS[tens] + (f"-{_ONES[rest]}" if rest else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        head = f"{_ONES[hundreds]} hundred"
        return head + (f" and {number_words(rest)}" if rest else "")
    thousands, rest = divmod(value, 1000)
    head = f"{number_words(thousands)} thousand"
    if not rest:
        return head
    joiner = " and " if rest < 100 else " "
    return head + joiner + number_words(rest)


def amount_words(value: str) -> str:
    """A money figure as words, without inventing a currency:
    "120.00" -> "one hundred and twenty"; "120.50" -> "one
    hundred and twenty point five zero". The plan carries bare
    amounts, so the spoken form spells the figure itself — the
    fractional part digit by digit, the way a statement is read
    aloud — and names no currency the record does not."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    whole = int(amount)
    cents = int((amount - whole) * 100)
    words = number_words(whole)
    if cents:
        digits = " ".join(_ONES[int(d)] for d in f"{cents:02d}")
        words += f" point {digits}"
    return words


def date_words(value: str) -> str:
    """An ISO date spoken: "2026-10-20" -> "the twentieth of
    October, two thousand and twenty-six". A value that is not
    an ISO date is returned as it stands."""
    match = _ISO_DATE_RE.match(str(value).strip())
    if not match:
        return str(value)
    year, month, day = (int(match.group(1)), int(match.group(2)),
                        int(match.group(3)))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return str(value)
    return (f"the {_ORDINALS[day]} of {_MONTHS[month]}, "
            f"{number_words(year)}")


def _value_words(step: PlanStep) -> str:
    """A SET step's planned value, spoken: dates as dates,
    a credit limit as a figure, everything else as it stands."""
    value = step.value or ""
    if step.field in _DATE_FIELDS or _ISO_DATE_RE.match(value):
        return date_words(value)
    if step.field == "credit_limit":
        return amount_words(value)
    return value


def _step_words(step: PlanStep) -> str:
    if step.action == StepAction.CREATE:
        fields = step.fields
        parts = [
            f"On the {step.page} page: issue a credit memo "
            f"against invoice {fields.get('invoice_id', '')}.",
            f"Reason: {fields.get('reason', '')}.",
            f"Amount: {amount_words(fields.get('amount', ''))}.",
        ]
        if fields.get("status"):
            parts.append(f"Status: {fields['status']}.")
        return " ".join(parts)
    label = FIELD_LABELS.get(step.field or "", step.field or "")
    kind = step.record_kind[:-1]  # customers -> customer
    return (f"On the {step.page} page: set the {label.lower()} "
            f"of {kind} {step.record_id} to {_value_words(step)}.")


def spoken_plan(plan: Plan) -> str:
    """The plan, for the ear: what it is, the pages it spans,
    each step in order with its figures spelled, and a closing
    line naming exactly what a confirmation approves. Derived
    from the stored plan on every call — the stored plan is
    never touched."""
    count = len(plan.steps)
    step_word = "step" if count == 1 else "steps"
    pages = plan.pages
    if len(pages) > 1:
        span = (f"It spans {number_words(len(pages))} pages: "
                + ", then ".join(pages) + ".")
    elif pages:
        span = f"It works on the {pages[0]} page."
    else:
        span = ""
    lines = [
        f"Plan version {plan.version} for task {plan.task_id}: "
        f"{number_words(count)} {step_word}. {span}".strip(),
    ]
    for step in plan.steps:
        lines.append(f"Step {step.seq}. {_step_words(step)}")
    lines.append(
        f"End of plan. Confirming approves exactly these "
        f"{number_words(count)} {step_word}.")
    return "\n".join(lines)


def spoken_hash(plan: Plan) -> str:
    """The hash a read-back binds to: SHA-256 over the spoken
    form of this exact plan version. Amend the plan and the
    words change, so the hash changes — a token issued against
    the old words cannot confirm the new ones."""
    return hashlib.sha256(
        spoken_plan(plan).encode("utf-8")).hexdigest()


# ------------------------------------------------------- transcripts

# A speaker label at the head of a transcript line: a governed
# list of speaker words, or a capitalised name of up to two
# words, always closed by a colon. "Operator: raise a credit
# memo ..." and "S. Hale: confirmed" both lose their labels;
# task text itself — which never opens with a capitalised name
# and a colon — keeps every word.
_LABEL_RE = re.compile(
    r"^(?:speaker\s*\d+|operator|supervisor|agent|customer|system"
    r"|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*:\s+", re.I)
_STAMP_RE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")


def normalize_transcript(text: str) -> str:
    """A dictated task, unwrapped: timestamp markers and speaker
    labels stripped line by line, the lines joined into the one
    statement the grammar reads, whitespace collapsed. Nothing
    else is rewritten — the words that reach the planner are the
    operator's own, which is what makes the plan a transcript
    produces identical to the plan the same words typed produce.
    """
    lines = []
    for raw in str(text).splitlines():
        line = raw.strip()
        line = _STAMP_RE.sub("", line)
        line = _LABEL_RE.sub("", line)
        if line:
            lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()
