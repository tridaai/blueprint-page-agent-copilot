"""Record mutations: the one way OrderDesk state changes.

The console's form endpoints, the offline driver, and (through the
form the browser submits) the in-page agent all change state through
these two functions — so whatever ran a step, the resulting state is
the same shape, and the verifier's diff means the same thing.
"""

from __future__ import annotations

from .store import Store


def apply_field_change(store: Store, kind: str, record_id: str,
                       field: str, value: str) -> tuple[dict, dict]:
    """Set one field on one record. Returns (before, after) field
    dicts for the audit trail."""
    record = store.get_record(kind, record_id)
    if record is None:
        raise KeyError(f"{kind} record {record_id} does not exist")
    before = {field: str(record.get(field, ""))}
    record[field] = value
    store.upsert_record(kind, record)
    return before, {field: value}


def create_credit_memo(store: Store, fields: dict[str, str],
                       task_id: str | None = None) -> dict:
    """Create a credit memo record from a governed field set. The id
    is minted here, in code — never by the agent. The memo names
    its customer as well as its invoice: the customer is read from
    the invoice it is issued against, here at creation, so the
    linkage is a fact on the record that verification can check —
    a memo issued against the wrong invoice names the wrong
    customer, and the check catches it by name."""
    existing = store.list_records("credit_memos")
    seq = 5001 + len(existing)
    invoice = store.get_record("invoices", fields["invoice_id"])
    record = {
        "id": f"CM-{seq}",
        "invoice_id": fields["invoice_id"],
        "customer_id": invoice["customer_id"] if invoice else "",
        "amount": fields["amount"],
        "reason": fields["reason"],
        "status": fields.get("status", "issued"),
        "created_for_task": task_id or "",
    }
    store.upsert_record("credit_memos", record)
    return record
