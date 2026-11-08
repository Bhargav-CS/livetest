"""Opening SQLite the way livetest needs it. Knows nothing about what's stored: runtime owns the tables."""

import sqlite3
from pathlib import Path

DEFAULT_PATH = Path.home() / ".livetest" / "livetest.db"


def connect(path: Path = DEFAULT_PATH, migrations: list[str] = []) -> sqlite3.Connection:
    """Open the DB, creating the folder and file on first use and running the migrations it hasn't had.

    `PRAGMA user_version` counts the steps done; step N runs once, in one transaction with its version bump.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")  # the UI can read while a run writes
    done = db.execute("PRAGMA user_version").fetchone()[0]
    if done > len(migrations):
        db.close()
        raise sqlite3.DatabaseError(f"{path} was made by a newer livetest; upgrade livetest")
    for n, step in enumerate(migrations[done:], done + 1):
        db.executescript(f"BEGIN; {step} PRAGMA user_version = {n}; COMMIT;")
    return db
