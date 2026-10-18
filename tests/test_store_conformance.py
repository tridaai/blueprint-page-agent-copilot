"""One behaviour contract, every store implementation.

The PostgreSQL leg is env-gated (COPILOT_TEST_DATABASE_URL) — the
local stack is not always up; SQLite and in-memory always run.
"""

import os

import pytest

from page_agent_copilot.dataset import seed_records
from page_agent_copilot.store import InMemoryStore, SqlStore


def exercise_store(store) -> None:
    assert store.seed_if_empty(seed_records()) is True
    assert store.seed_if_empty(seed_records()) is False  # idempotent

    customer = store.get_record("customers", "2041")
    assert customer["name"] == "Harbourview Hotels Ltd"
    assert len(store.list_records("invoices")) == 6
    assert store.get_record("customers", "9999") is None

    customer["phone"] = "555-7777"
    store.upsert_record("customers", customer)
    assert store.get_record("customers", "2041")["phone"] == "555-7777"

    store.put_task({"task_id": "TASK-T1", "status": "PLANNED"})
    assert store.get_task("TASK-T1")["status"] == "PLANNED"
    store.put_task({"task_id": "TASK-T1", "status": "CONFIRMED"})
    assert store.get_task("TASK-T1")["status"] == "CONFIRMED"

    store.put_plan({"task_id": "TASK-T1", "version": 1, "steps": []})
    store.put_plan({"task_id": "TASK-T1", "version": 2, "steps": [1]})
    assert store.get_plan("TASK-T1", 1)["steps"] == []
    assert store.latest_plan("TASK-T1")["version"] == 2
    assert store.latest_plan("TASK-NONE") is None

    store.add_audit({"task_id": "TASK-T1", "event": "a"})
    store.add_audit({"task_id": "TASK-T1", "event": "b"})
    store.add_audit({"task_id": "TASK-T2", "event": "c"})
    assert [e["event"] for e in store.list_audit("TASK-T1")] == ["a", "b"]
    assert [e["event"] for e in store.list_all_audit()] == ["a", "b", "c"]


def test_in_memory_store():
    exercise_store(InMemoryStore())


def test_sqlite_store(tmp_path):
    exercise_store(SqlStore("sqlite", str(tmp_path / "copilot.db")))


def _database_identity(dsn: str) -> tuple:
    """(host, port, dbname) a DSN points at, with libpq's defaults."""
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    return (info.get("host", "localhost"), str(info.get("port", "5432")),
            info.get("dbname"))


def _fresh_postgres(dsn: str) -> None:
    """Empty the scratch database the conformance leg runs against.

    The contract below asserts exact contents (a first seed, a fixed
    record count, an audit trail of exactly three entries), so it
    needs a provably empty store — a database a previous run already
    exercised fails it on leftovers, not on behaviour. Dropping every
    public table makes the store's own migrations rebuild the schema
    on construction, so the leg still exercises the full contract
    against a real Postgres.

    The wipe only ever touches the database COPILOT_TEST_DATABASE_URL
    names. If that turns out to be the same database the app itself
    is configured with (DATABASE_URL), refuse loudly instead: wiping
    the application's own database from a test would be a disaster
    dressed as hygiene.
    """
    import psycopg

    app_url = os.environ.get("DATABASE_URL")
    if app_url:
        try:
            same = _database_identity(app_url) == _database_identity(dsn)
        except Exception:
            same = False  # not a Postgres DSN — cannot name this database
        if same:
            raise RuntimeError(
                "COPILOT_TEST_DATABASE_URL points at the application's "
                "own DATABASE_URL database; refusing to wipe it. Point "
                "COPILOT_TEST_DATABASE_URL at a scratch database.")
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
        for table in tables:
            conn.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


@pytest.mark.skipif(
    not os.environ.get("COPILOT_TEST_DATABASE_URL"),
    reason="COPILOT_TEST_DATABASE_URL not set (Postgres stack down)")
def test_postgres_store():
    dsn = os.environ["COPILOT_TEST_DATABASE_URL"]
    _fresh_postgres(dsn)
    exercise_store(SqlStore("postgres", dsn))
