-- 0001_core (SQLite): the governed shell's tables.
-- OrderDesk records live as JSON documents keyed by (kind, id): the
-- console, the planner and the verifier all read the same document,
-- so there is one source of truth to diff.
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL,
    id   TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, id)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    data    TEXT NOT NULL,
    PRIMARY KEY (task_id, version)
);

CREATE TABLE IF NOT EXISTS audit_entries (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    data    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_entries (task_id, id);
