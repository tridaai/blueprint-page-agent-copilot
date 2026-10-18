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


def test_in_memory_store():
    exercise_store(InMemoryStore())


def test_sqlite_store(tmp_path):
    exercise_store(SqlStore("sqlite", str(tmp_path / "copilot.db")))


@pytest.mark.skipif(
    not os.environ.get("COPILOT_TEST_DATABASE_URL"),
    reason="COPILOT_TEST_DATABASE_URL not set (Postgres stack down)")
def test_postgres_store():
    exercise_store(SqlStore("postgres",
                            os.environ["COPILOT_TEST_DATABASE_URL"]))
