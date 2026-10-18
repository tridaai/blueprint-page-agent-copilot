"""The console read surface: the reads OrderDesk is served from.

Every record a human sees in the console arrives through these
functions — the API's console GET endpoints serve exactly what
they return — and, since the verification-depth round, the
verifier reads through them too.

Why a separate surface, when the store is right there? Because
verification must not trust the path that did the work. The
mutation path (`records.py`, used by the console's form saves and
by the offline driver) writes through the store; the bridge
reports what it claims it did. The verifier enters through here
instead: the read functions a human's browser loads, returning
records in their served (JSON) shape. The store underneath is the
same single source of truth — that is deliberate, and it is the
honest boundary of the independence: one store, two paths in, so
a mutation-path bug or a lying bridge report cannot certify
itself. What the verifier diffs is what a human would see.
"""

from __future__ import annotations

import json
from typing import Optional

from .store import Store


def _served(row: Optional[dict]) -> Optional[dict]:
    """A record in the shape the console serves it: a JSON round
    trip, so the reader diffs what a browser would render, not an
    internal dict that happens to share the store."""
    if row is None:
        return None
    return json.loads(json.dumps(row))


class ConsoleReads:
    """Read-only views over the store, one per thing the console
    shows. No method here mutates anything."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, kind: str, record_id: str) -> Optional[dict]:
        return _served(self._store.get_record(kind, record_id))

    def list(self, kind: str) -> list[dict]:
        return [_served(r) for r in self._store.list_records(kind)]

    # -- the console's own derivations -------------------------------
    def memos_for_invoice(self, invoice_id: str) -> list[dict]:
        """The credit memos an invoice's screen lists: the console
        derives the list by scanning the memos for its invoice id
        (see openInvoice in console.js). Verification uses the
        same rule to ask whether an invoice reflects a memo."""
        return [m for m in self.list("credit_memos")
                if m.get("invoice_id") == invoice_id]

    def memos_for_task(self, task_id: str) -> list[dict]:
        """The memos stamped as created for a task — how the shell
        finds what a live execution created."""
        return [m for m in self.list("credit_memos")
                if m.get("created_for_task") == task_id]
