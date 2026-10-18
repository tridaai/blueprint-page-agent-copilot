"""Audit exports: the trail, chained so tampering shows.

An audit trail that can be quietly edited after the fact is a
story, not evidence. An export takes one scope's trail — one
task's, one batch's (its own scope entries plus its member
tasks' trails, in the trail's true order), or one governance
scope (roles, templates) — and produces a single ordered
document in which every entry carries a SHA-256 **chain hash**
computed over the previous entry's hash and this entry's
canonical content. Edit an entry, delete one, swap two, or cut
the tail, and re-walking the chain finds the first place it
breaks: `verify_export` names that entry's position.

The chain covers the trail **as it stood at export**. The
export act itself is recorded in the trail afterwards (the
service writes `audit_exported`), so an export document never
contains its own export entry — a second export of the same
scope includes the first export's entry, chained like any
other, and verifies the same way.

This module is pure: it holds no store and makes no decisions.
The service gathers a scope's entries in canonical order (the
audit reads are chronological by construction on every backend
— append order in memory, the audit table's row id in SQL),
supplies the exporter and the export time (the service's shared
clock, `_now()` — UTC, one-second granularity, the same stamp
every record in the shell carries), and records the act. The
functions here chain, render, and verify.

Verification is over the JSON document. The CSV rendering
carries the same entries in the same order with the same
hashes — a human-readable copy of the document, not a second
thing to verify.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json

EXPORT_FORMAT = "page-agent-copilot/audit-export"
EXPORT_VERSION = 1

# The hash the chain starts from: no entry precedes the first,
# and the genesis is a constant, so an export of an empty trail
# still has a well-defined final hash — its own entry count (0)
# and this hash are what its header names.
GENESIS_HASH = "0" * 64

# The fields of an audit entry the chain covers — exactly the
# fields the trail stores, nothing added, nothing dropped. The
# chain fields themselves (`chain_index`, `prev_hash`,
# `chain_hash`) ride beside the entry in the document, never
# inside the content the hash is computed over.
_ENTRY_FIELDS = ("task_id", "ts", "actor", "event", "detail")


def canonical_entry(entry: dict) -> str:
    """One audit entry as canonical text: the stored fields
    only, keys sorted, no incidental whitespace. Two readers
    with the same entry produce the same string, byte for
    byte — that is what makes a hash recomputed later (by the
    verifier, by a third party) comparable to the one the
    export wrote."""
    body = {field: entry.get(field) for field in _ENTRY_FIELDS}
    return json.dumps(body, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def _chain_hash(prev_hash: str, entry: dict) -> str:
    payload = f"{prev_hash}\n{canonical_entry(entry)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_export(scope: dict, exporter: str, exported_at: str,
                 entries: list[dict]) -> dict:
    """Chain `entries` (already in the trail's true order) into
    an export document. The header names the scope, the
    exporter, and the export time the caller supplies, and —
    once the walk is done — the entry count and the final
    chain hash, the two facts that make a truncated copy
    detectable."""
    chained: list[dict] = []
    prev = GENESIS_HASH
    for index, entry in enumerate(entries, start=1):
        digest = _chain_hash(prev, entry)
        row = {field: entry.get(field) for field in _ENTRY_FIELDS}
        row["chain_index"] = index
        row["prev_hash"] = prev
        row["chain_hash"] = digest
        chained.append(row)
        prev = digest
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "scope": scope,
        "exporter": exporter,
        "exported_at": exported_at,
        "entry_count": len(chained),
        "final_hash": prev,
        "entries": chained,
    }


def render_csv(document: dict) -> str:
    """The same export, as CSV: one row per entry, in chain
    order, carrying the entry's fields and its chain hashes.
    The header names the export it renders (scope, exporter,
    time, count, final hash) as `#` comment lines above the
    column row, so the rendering keeps the document's
    provenance without inventing columns the entries do not
    have."""
    scope = document.get("scope") or {}
    scope_label = scope.get("kind", "")
    if scope.get("id"):
        scope_label = f"{scope_label}:{scope['id']}"
    out = io.StringIO()
    out.write(f"# {EXPORT_FORMAT} v{EXPORT_VERSION}\n")
    out.write(f"# scope: {scope_label}\n")
    out.write(f"# exporter: {document.get('exporter', '')}\n")
    out.write(f"# exported_at: {document.get('exported_at', '')}\n")
    out.write(f"# entry_count: {document.get('entry_count', '')}\n")
    out.write(f"# final_hash: {document.get('final_hash', '')}\n")
    writer = csv.writer(out)
    writer.writerow(["chain_index", "scope", "ts", "actor",
                     "event", "detail", "prev_hash", "chain_hash"])
    for entry in document.get("entries") or []:
        writer.writerow([
            entry.get("chain_index", ""),
            entry.get("task_id", ""),
            entry.get("ts", ""),
            entry.get("actor", ""),
            entry.get("event", ""),
            json.dumps(entry.get("detail") or {}, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False),
            entry.get("prev_hash", ""),
            entry.get("chain_hash", ""),
        ])
    return out.getvalue()


def verify_export(document: dict) -> dict:
    """Re-walk an export document and grade its chain.

    Returns {"ok": True, "entries": N, "final_hash": H} when
    every entry's stored hashes match a fresh computation, in
    order, and the header's count and final hash agree with
    the walk. Otherwise {"ok": False, "broken_at": i,
    "reason": ...} where `broken_at` is the 1-based chain
    position of the first entry whose content, link, or index
    fails — or, when every present entry checks out but the
    header names more entries than the document holds (a
    truncated tail), the position of the first missing entry.
    A document too malformed to walk reports `broken_at` None
    with the reason."""
    if not isinstance(document, dict):
        return {"ok": False, "broken_at": None,
                "reason": "the export is not a document"}
    if document.get("format") != EXPORT_FORMAT:
        return {"ok": False, "broken_at": None,
                "reason": "the document does not name the "
                          "audit-export format"}
    entries = document.get("entries")
    if not isinstance(entries, list):
        return {"ok": False, "broken_at": None,
                "reason": "the document carries no entries list"}
    prev = GENESIS_HASH
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            return {"ok": False, "broken_at": index,
                    "reason": f"entry {index} is not an entry"}
        if entry.get("chain_index") != index:
            return {"ok": False, "broken_at": index,
                    "reason": f"entry {index} carries chain "
                              f"index {entry.get('chain_index')!r}; "
                              f"an entry was moved, dropped, or "
                              f"renumbered"}
        if entry.get("prev_hash") != prev:
            return {"ok": False, "broken_at": index,
                    "reason": f"entry {index} does not link to "
                              f"the entry before it; the chain "
                              f"breaks here"}
        digest = _chain_hash(prev, entry)
        if entry.get("chain_hash") != digest:
            return {"ok": False, "broken_at": index,
                    "reason": f"entry {index}'s content does not "
                              f"match its chain hash; the entry "
                              f"was edited after export"}
        prev = digest
    declared = document.get("entry_count")
    if declared != len(entries):
        if isinstance(declared, int) and declared > len(entries):
            return {"ok": False, "broken_at": len(entries) + 1,
                    "reason": f"the header names {declared} "
                              f"entries but the document holds "
                              f"{len(entries)}; the tail was cut "
                              f"— the first missing entry is "
                              f"{len(entries) + 1}"}
        return {"ok": False, "broken_at": len(entries) or None,
                "reason": f"the header names {declared!r} entries "
                          f"but the document holds "
                          f"{len(entries)}"}
    if document.get("final_hash") != prev:
        return {"ok": False, "broken_at": len(entries) or None,
                "reason": "the header's final hash does not "
                          "match the chain's last hash"}
    return {"ok": True, "entries": len(entries),
            "final_hash": prev}
