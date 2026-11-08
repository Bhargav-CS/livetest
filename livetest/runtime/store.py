"""livetest's tables and the queries on them. CLI and UI both call these; nothing else touches SQL."""

import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from livetest.platform import db
from livetest.runtime.models import Agent, Profile, Scenario

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
]

TABLES = {Agent: "agents", Profile: "profiles", Scenario: "scenarios"}


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
