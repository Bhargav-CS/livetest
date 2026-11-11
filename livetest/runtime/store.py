"""livetest's tables and the queries on them. CLI and UI both call these; nothing else touches SQL."""

import json
import sqlite3
import time
from importlib.metadata import version
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
    # 6. A run's tests are queued up front, so `--test N` exists before it starts. Results' status gains
    #    queued | stopping (asked to stop, ends at its next check) | stopped. A run's own status comes from its tests.
    """
    CREATE TABLE runs (
        id         TEXT PRIMARY KEY,  -- `run start --run`, or the next free number
        agent      TEXT NOT NULL,
        profile    TEXT,              -- its name; each result has it as JSON
        version    TEXT NOT NULL,     -- livetest's
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    ALTER TABLE results ADD COLUMN run TEXT REFERENCES runs (id) ON DELETE CASCADE;
    ALTER TABLE results ADD COLUMN test INTEGER;  -- 1..N in its run
    """,
    # 7. What a test used, as counts (no cost in v0.1): checks.usage's {"agent": [...], "tester": [...]}, JSON.
    "ALTER TABLE results ADD COLUMN usage TEXT;",
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


def _dicts(cur: sqlite3.Cursor) -> list[dict]:
    return [dict(zip([c[0] for c in cur.description], row)) for row in cur]


def start_run(conn: sqlite3.Connection, run: str, agent: str, profile: Profile | None,
              tests: list[tuple[Scenario, str]]) -> list[int]:
    """Save a run and queue its tests, (scenario, room) each; returns their result ids. IntegrityError if `run` is taken."""
    with conn:
        conn.execute("INSERT INTO runs (id, agent, profile, version) VALUES (?, ?, ?, ?)",
                     (run, agent, profile and profile.name, version("livetest")))
        return [conn.execute("INSERT INTO results (scenario, profile, room, run, test, status) VALUES (?, ?, ?, ?, ?, 'queued')",
                             (s.model_dump_json(), profile and profile.model_dump_json(), room, run, n)).lastrowid
                for n, (s, room) in enumerate(tests, 1)]


def next_run_id(conn: sqlite3.Connection) -> str:
    """One more than the biggest all-digits run id."""
    return str(conn.execute("SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0) + 1 FROM runs WHERE id NOT GLOB '*[^0-9]*'").fetchone()[0])


def runs(conn: sqlite3.Connection, run: str | None = None) -> list[dict]:
    """Every run, oldest first, with its status and test counts; or just `run` ('latest' is the newest)."""
    # ponytail: a killed process (Ctrl+C, crash) leaves its tests 'running', so the run too; add a heartbeat if that bites
    return _dicts(conn.execute(
        "SELECT r.*, CASE WHEN SUM(t.status IN ('queued', 'running')) THEN 'running' "
        "WHEN SUM(t.status = 'stopping') THEN 'stopping' WHEN SUM(t.status = 'stopped') THEN 'stopped' ELSE 'done' END "
        "AS status, COUNT(t.id) AS tests, COUNT(t.finished_at) AS finished, COALESCE(SUM(t.passed), 0) AS passed, "
        "COUNT(t.passed) AS checked FROM runs r LEFT JOIN results t ON t.run = r.id "
        "WHERE ? IS NULL OR r.id = ? OR (? = 'latest' AND r.rowid = (SELECT MAX(rowid) FROM runs)) "
        "GROUP BY r.id ORDER BY r.rowid", (run, run, run)))


def results(conn: sqlite3.Connection, run: str) -> list[dict]:
    """A run's tests in order, each with its agent's error count: errors are reported, not checked."""
    return _dicts(conn.execute("SELECT r.*, (SELECT COUNT(*) FROM events e WHERE e.result = r.id AND e.source = 'agent' "
                               "AND e.type = 'error') AS agent_errors FROM results r WHERE run = ? ORDER BY test", (run,)))


def begin(conn: sqlite3.Connection, result: int) -> bool:
    """queued → running. False if it was stopped while queued."""
    with conn:
        return conn.execute("UPDATE results SET status = 'running', started_at = CURRENT_TIMESTAMP "
                            "WHERE id = ? AND status = 'queued'", (result,)).rowcount > 0


def stop(conn: sqlite3.Connection, run: str, test: int | None = None) -> int:
    """Queued tests never start; running ones end at their next check (runner). Returns how many it stopped."""
    with conn:
        return conn.execute("UPDATE results SET status = IIF(status = 'queued', 'stopped', 'stopping'), "
                            "ended = 'stopped by user', finished_at = IIF(status = 'queued', CURRENT_TIMESTAMP, NULL) "
                            "WHERE run = ? AND status IN ('queued', 'running') AND (? IS NULL OR test = ?)",
                            (run, test, test)).rowcount


def event(conn: sqlite3.Connection, result: int, source: str, type: str, data, ts: float | None = None) -> None:
    """Committed at once, so the UI and `run logs` see it live."""
    with conn:
        conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                     (result, time.time() if ts is None else ts, source, type, json.dumps(data)))


def finish_result(conn: sqlite3.Connection, result: int, status: str, ended: str | None,
                  checks: list[dict] | None = None, usage: dict | None = None) -> None:
    passed, checks = (None, None) if checks is None else (all(c["passed"] for c in checks), json.dumps(checks))
    with conn:
        conn.execute("UPDATE results SET status = ?, ended = ?, passed = ?, checks = ?, usage = ?, "
                     "finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (status, ended, passed, checks, usage and json.dumps(usage), result))


def result(conn: sqlite3.Connection, result: int) -> dict:
    return _dicts(conn.execute("SELECT * FROM results WHERE id = ?", (result,)))[0]


def events(conn: sqlite3.Connection, result: int) -> list[tuple[float, str, str, object]]:
    """(ts, source, type, data) in the order they happened."""
    found = conn.execute("SELECT ts, source, type, data FROM events WHERE result = ? ORDER BY ts, rowid", (result,))
    return [(ts, source, t, json.loads(data)) for ts, source, t, data in found]


def run_events(conn: sqlite3.Connection, run: str, test: int | None = None, after: int = 0) -> list[tuple]:
    """(rowid, test, ts, source, type, data) of a run's events past rowid `after`, as written: follow a live run."""
    found = conn.execute("SELECT e.rowid, r.test, e.ts, e.source, e.type, e.data FROM events e JOIN results r ON r.id = e.result "
                         "WHERE r.run = ? AND (? IS NULL OR r.test = ?) AND e.rowid > ? ORDER BY e.rowid",
                         (run, test, test, after))
    return [(*row[:5], json.loads(row[5])) for row in found]


def active(conn: sqlite3.Connection) -> dict[str, Plugin]:
    """The active plugin of each kind, e.g. {"llm": Plugin(...)}."""
    found = conn.execute("SELECT a.kind, p.name, p.data FROM active_plugins a JOIN plugins p ON p.id = a.plugin")
    return {kind: Plugin(name=name, **json.loads(data)) for kind, name, data in found}
