"""livetest's tables and the queries on them. CLI and UI both call these; nothing else touches SQL."""

import json
import sqlite3
import time
from pathlib import Path

from pydantic import BaseModel

from livetest.platform import db
from livetest.runtime.models import Agent, Plugin, Profile, Scenario

# Append only: each step runs once per DB, in order. Never edit a released step; add a new one.
MIGRATIONS = [
    # 1. Was the whole schema before migrations, hence IF NOT EXISTS.
    """
    CREATE TABLE IF NOT EXISTS agents (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        data       TEXT NOT NULL,  -- the model as JSON, minus name (same in every table)
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 2. AUTOINCREMENT: a scenario's id is its global number, never reused after a remove.
    """
    CREATE TABLE profiles (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        data       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE scenarios (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL UNIQUE,
        data       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 3. The primary key keeps one active plugin per kind; removing a plugin clears it.
    """
    CREATE TABLE plugins (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE,
        data       TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE active_plugins (
        kind   TEXT PRIMARY KEY,
        plugin INTEGER NOT NULL REFERENCES plugins (id) ON DELETE CASCADE
    );
    """,
    # 4. One row per test; everything that happened in it is an event, written as it arrives. Runs come in module 7.
    """
    CREATE TABLE results (
        id          INTEGER PRIMARY KEY,
        scenario    TEXT NOT NULL,  -- JSON, as it was when the test ran
        profile     TEXT,           -- JSON; NULL = plain participant
        room        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'running',  -- running | done | never_joined | no_session_host | error
        ended       TEXT,           -- done: max_turns | max_minutes | agent_left; otherwise the error
        started_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT
    );
    CREATE TABLE events (
        result INTEGER NOT NULL REFERENCES results (id) ON DELETE CASCADE,
        ts     REAL NOT NULL,  -- the tester's receive time (unix s), so it lines up with the recording
        source TEXT NOT NULL,  -- agent (session host) | tester | log
        type   TEXT NOT NULL,  -- the event's name; a log line's level
        data   TEXT NOT NULL   -- JSON
    );
    CREATE INDEX events_by_result ON events (result, ts);
    """,
    # 5. The verdict next to the status, written together, so run lists and pass rates need no JSON.
    """
    ALTER TABLE results ADD COLUMN passed INTEGER;  -- 1 | 0; NULL = not checked (status isn't done)
    ALTER TABLE results ADD COLUMN checks TEXT;     -- JSON: checks.evaluate's outcomes
    """,
]

TABLES = {Agent: "agents", Profile: "profiles", Scenario: "scenarios", Plugin: "plugins"}


def connect(path: Path = db.DEFAULT_PATH) -> sqlite3.Connection:
    return db.connect(path, MIGRATIONS)


def add(conn: sqlite3.Connection, model: BaseModel) -> int:
    """Save an agent, profile or scenario; returns its number. Raises sqlite3.IntegrityError if the name is taken."""
    with conn:
        return conn.execute(
            f"INSERT INTO {TABLES[type(model)]} (name, data) VALUES (?, ?)",
            (model.name, model.model_dump_json(exclude={"name"}, exclude_none=True)),
        ).lastrowid


def rows[M: BaseModel](conn: sqlite3.Connection, kind: type[M]) -> list[tuple[int, M]]:
    """(number, model) pairs, oldest first."""
    found = conn.execute(f"SELECT id, name, data FROM {TABLES[kind]} ORDER BY id")
    return [(n, kind(name=name, **json.loads(data))) for n, name, data in found]


def remove(conn: sqlite3.Connection, kind: type[BaseModel], name: str) -> bool:
    """Returns False if there was no such name."""
    with conn:
        return conn.execute(f"DELETE FROM {TABLES[kind]} WHERE name = ?", (name,)).rowcount > 0


def use(conn: sqlite3.Connection, name: str) -> bool:
    """Make a plugin the active one of its kind. Returns False if there's no such plugin."""
    with conn:
        return conn.execute("INSERT OR REPLACE INTO active_plugins (kind, plugin) "
                            "SELECT json_extract(data, '$.kind'), id FROM plugins WHERE name = ?", (name,)).rowcount > 0


def start_result(conn: sqlite3.Connection, scenario: Scenario, profile: Profile | None, room: str) -> int:
    with conn:
        return conn.execute("INSERT INTO results (scenario, profile, room) VALUES (?, ?, ?)",
                            (scenario.model_dump_json(), profile and profile.model_dump_json(), room)).lastrowid


def event(conn: sqlite3.Connection, result: int, source: str, type: str, data, ts: float | None = None) -> None:
    """Committed at once, so the UI and `run logs` see it live."""
    with conn:
        conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                     (result, time.time() if ts is None else ts, source, type, json.dumps(data)))


def finish_result(conn: sqlite3.Connection, result: int, status: str, ended: str | None,
                  checks: list[dict] | None = None) -> None:
    passed, checks = (None, None) if checks is None else (all(c["passed"] for c in checks), json.dumps(checks))
    with conn:
        conn.execute("UPDATE results SET status = ?, ended = ?, passed = ?, checks = ?, finished_at = CURRENT_TIMESTAMP "
                     "WHERE id = ?", (status, ended, passed, checks, result))


def result(conn: sqlite3.Connection, result: int) -> dict:
    cur = conn.execute("SELECT * FROM results WHERE id = ?", (result,))
    return dict(zip([c[0] for c in cur.description], cur.fetchone()))


def events(conn: sqlite3.Connection, result: int) -> list[tuple[float, str, str, object]]:
    """(ts, source, type, data) in the order they happened."""
    found = conn.execute("SELECT ts, source, type, data FROM events WHERE result = ? ORDER BY ts, rowid", (result,))
    return [(ts, source, t, json.loads(data)) for ts, source, t, data in found]


def active(conn: sqlite3.Connection) -> dict[str, Plugin]:
    """The active plugin of each kind, e.g. {"llm": Plugin(...)}."""
    found = conn.execute("SELECT a.kind, p.name, p.data FROM active_plugins a JOIN plugins p ON p.id = a.plugin")
    return {kind: Plugin(name=name, **json.loads(data)) for kind, name, data in found}
