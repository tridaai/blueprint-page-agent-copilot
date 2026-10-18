"""The store port and its implementations.

Everything the shell persists goes through this port: OrderDesk
records, tasks, versioned plans, and the audit trail. Three
implementations share the port — in-memory (tests), SQLite (the
local default), and PostgreSQL (the production profile) — and the
conformance suite holds all three to the same behaviour.

Migrations are numbered SQL files, PG dialect under `migrations/`
and SQLite under `migrations/sqlite/`, applied in order at startup
by the runner here and recorded in `schema_migrations`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional, Protocol

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _migration_files(kind: str) -> list[Path]:
    folder = MIGRATIONS_DIR / "sqlite" if kind == "sqlite" else MIGRATIONS_DIR
    return sorted(folder.glob("[0-9]*.sql"))


class Store(Protocol):
    def seed_if_empty(self, records: dict[str, list[dict]]) -> bool: ...
    def get_record(self, kind: str, record_id: str) -> Optional[dict]: ...
    def list_records(self, kind: str) -> list[dict]: ...
    def upsert_record(self, kind: str, record: dict) -> None: ...
    def put_task(self, task: dict) -> None: ...
    def get_task(self, task_id: str) -> Optional[dict]: ...
    def list_tasks(self) -> list[dict]: ...
    def put_plan(self, plan: dict) -> None: ...
    def get_plan(self, task_id: str, version: int) -> Optional[dict]: ...
    def latest_plan(self, task_id: str) -> Optional[dict]: ...
    def add_audit(self, entry: dict) -> None: ...
    def list_audit(self, task_id: str) -> list[dict]: ...
    def list_all_audit(self) -> list[dict]: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict] = {}
        self._tasks: dict[str, dict] = {}
        self._plans: dict[tuple[str, int], dict] = {}
        self._audit: list[dict] = []
        self._lock = threading.Lock()

    def seed_if_empty(self, records: dict[str, list[dict]]) -> bool:
        with self._lock:
            if self._records:
                return False
            for kind, rows in records.items():
                for row in rows:
                    self._records[(kind, str(row["id"]))] = dict(row)
            return True

    def get_record(self, kind: str, record_id: str) -> Optional[dict]:
        row = self._records.get((kind, str(record_id)))
        return dict(row) if row else None

    def list_records(self, kind: str) -> list[dict]:
        return [dict(r) for (k, _), r in sorted(self._records.items()) if k == kind]

    def upsert_record(self, kind: str, record: dict) -> None:
        with self._lock:
            self._records[(kind, str(record["id"]))] = dict(record)

    def put_task(self, task: dict) -> None:
        with self._lock:
            self._tasks[task["task_id"]] = dict(task)

    def get_task(self, task_id: str) -> Optional[dict]:
        task = self._tasks.get(task_id)
        return dict(task) if task else None

    def list_tasks(self) -> list[dict]:
        return [dict(t) for t in self._tasks.values()]

    def put_plan(self, plan: dict) -> None:
        with self._lock:
            self._plans[(plan["task_id"], int(plan["version"]))] = dict(plan)

    def get_plan(self, task_id: str, version: int) -> Optional[dict]:
        plan = self._plans.get((task_id, int(version)))
        return dict(plan) if plan else None

    def latest_plan(self, task_id: str) -> Optional[dict]:
        versions = [v for (t, v) in self._plans if t == task_id]
        if not versions:
            return None
        return self.get_plan(task_id, max(versions))

    def add_audit(self, entry: dict) -> None:
        with self._lock:
            self._audit.append(dict(entry))

    def list_audit(self, task_id: str) -> list[dict]:
        return [dict(e) for e in self._audit if e["task_id"] == task_id]

    def list_all_audit(self) -> list[dict]:
        return [dict(e) for e in self._audit]


class SqlStore:
    """SQLite and PostgreSQL behind one implementation; the dialects
    differ only in placeholder style and the connection factory."""

    def __init__(self, kind: str, target: str) -> None:
        self.kind = kind
        self.target = target
        self._ph = "?" if kind == "sqlite" else "%s"
        self._lock = threading.Lock()
        if kind == "sqlite":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        self._run_migrations()

    # -- connection -------------------------------------------------
    def _connect(self):
        if self.kind == "sqlite":
            conn = sqlite3.connect(self.target)
            conn.row_factory = sqlite3.Row
            return conn
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.target, row_factory=dict_row)

    def _run_migrations(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT)")
            applied = {
                row["version"] for row in
                conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for path in _migration_files(self.kind):
                version = path.stem
                if version in applied:
                    continue
                sql = path.read_text()
                if self.kind == "sqlite":
                    conn.executescript(sql)
                else:
                    # psycopg runs one statement per call.
                    for statement in sql.split(";\n"):
                        statement = statement.strip()
                        if statement:
                            conn.execute(statement)
                conn.execute(
                    f"INSERT INTO schema_migrations (version, applied_at) "
                    f"VALUES ({self._ph}, {self._ph})",
                    (version, "startup"))
            conn.commit()

    # -- helpers ------------------------------------------------------
    def _one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _write(self, sql: str, params: tuple = ()) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(sql, params)
            conn.commit()

    # -- records ------------------------------------------------------
    def seed_if_empty(self, records: dict[str, list[dict]]) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()
            if row["n"]:
                return False
            for kind, rows in records.items():
                for record in rows:
                    conn.execute(
                        f"INSERT INTO records (kind, id, data) VALUES "
                        f"({self._ph}, {self._ph}, {self._ph})",
                        (kind, str(record["id"]), json.dumps(record)))
            conn.commit()
            return True

    def get_record(self, kind: str, record_id: str) -> Optional[dict]:
        row = self._one(
            f"SELECT data FROM records WHERE kind = {self._ph} AND id = {self._ph}",
            (kind, str(record_id)))
        return json.loads(row["data"]) if row else None

    def list_records(self, kind: str) -> list[dict]:
        rows = self._all(
            f"SELECT data FROM records WHERE kind = {self._ph} ORDER BY id",
            (kind,))
        return [json.loads(r["data"]) for r in rows]

    def upsert_record(self, kind: str, record: dict) -> None:
        sql = (
            f"INSERT INTO records (kind, id, data) VALUES "
            f"({self._ph}, {self._ph}, {self._ph}) "
            f"ON CONFLICT (kind, id) DO UPDATE SET data = EXCLUDED.data"
            if self.kind == "postgres" else
            f"INSERT OR REPLACE INTO records (kind, id, data) VALUES "
            f"({self._ph}, {self._ph}, {self._ph})")
        self._write(sql, (kind, str(record["id"]), json.dumps(record)))

    # -- tasks --------------------------------------------------------
    def put_task(self, task: dict) -> None:
        sql = (
            f"INSERT INTO tasks (task_id, data) VALUES ({self._ph}, {self._ph}) "
            f"ON CONFLICT (task_id) DO UPDATE SET data = EXCLUDED.data"
            if self.kind == "postgres" else
            f"INSERT OR REPLACE INTO tasks (task_id, data) VALUES "
            f"({self._ph}, {self._ph})")
        self._write(sql, (task["task_id"], json.dumps(task)))

    def get_task(self, task_id: str) -> Optional[dict]:
        row = self._one(
            f"SELECT data FROM tasks WHERE task_id = {self._ph}", (task_id,))
        return json.loads(row["data"]) if row else None

    def list_tasks(self) -> list[dict]:
        rows = self._all("SELECT data FROM tasks ORDER BY task_id")
        return [json.loads(r["data"]) for r in rows]

    # -- plans ----------------------------------------------------------
    def put_plan(self, plan: dict) -> None:
        sql = (
            f"INSERT INTO plans (task_id, version, data) VALUES "
            f"({self._ph}, {self._ph}, {self._ph}) "
            f"ON CONFLICT (task_id, version) DO UPDATE SET data = EXCLUDED.data"
            if self.kind == "postgres" else
            f"INSERT OR REPLACE INTO plans (task_id, version, data) VALUES "
            f"({self._ph}, {self._ph}, {self._ph})")
        self._write(sql, (plan["task_id"], int(plan["version"]), json.dumps(plan)))

    def get_plan(self, task_id: str, version: int) -> Optional[dict]:
        row = self._one(
            f"SELECT data FROM plans WHERE task_id = {self._ph} "
            f"AND version = {self._ph}",
            (task_id, int(version)))
        return json.loads(row["data"]) if row else None

    def latest_plan(self, task_id: str) -> Optional[dict]:
        row = self._one(
            f"SELECT data FROM plans WHERE task_id = {self._ph} "
            f"ORDER BY version DESC LIMIT 1",
            (task_id,))
        return json.loads(row["data"]) if row else None

    # -- audit ----------------------------------------------------------
    def add_audit(self, entry: dict) -> None:
        self._write(
            f"INSERT INTO audit_entries (task_id, data) VALUES "
            f"({self._ph}, {self._ph})",
            (entry["task_id"], json.dumps(entry)))

    def list_audit(self, task_id: str) -> list[dict]:
        rows = self._all(
            f"SELECT data FROM audit_entries WHERE task_id = {self._ph} "
            f"ORDER BY id",
            (task_id,))
        return [json.loads(r["data"]) for r in rows]

    def list_all_audit(self) -> list[dict]:
        rows = self._all(
            "SELECT data FROM audit_entries ORDER BY id")
        return [json.loads(r["data"]) for r in rows]


def build_store(settings) -> Store:
    if settings.store_kind == "postgres":
        return SqlStore("postgres", settings.store_target)
    return SqlStore("sqlite", settings.store_target)
