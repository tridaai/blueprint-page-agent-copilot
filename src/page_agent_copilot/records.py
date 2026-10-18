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
    is minted here, in code — never by the agent."""
    existing = store.list_records("credit_memos")
    seq = 5001 + len(existing)
    record = {
        "id": f"CM-{seq}",
        "invoice_id": fields["invoice_id"],
        "amount": fields["amount"],
        "reason": fields["reason"],
        "status": fields.get("status", "issued"),
        "created_for_task": task_id or "",
    }
    store.upsert_record("credit_memos", record)
    return record
