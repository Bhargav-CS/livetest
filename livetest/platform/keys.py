"""API keys in a .env file, readable only by the user and never in the DB. A real env var beats the file (CI)."""

import os
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key


def get(path: Path, var: str) -> str | None:
    return os.environ.get(var) or dotenv_values(path).get(var)


def save(path: Path, var: str, value: str) -> None:
    set_key(path, var, value)
    os.chmod(path, 0o600)  # ponytail: only the read-only bit on Windows; there the user-only home folder protects it


def remove(path: Path, var: str) -> None:
    if var in dotenv_values(path):  # unset_key warns on stderr about a missing key
        unset_key(path, var)
