"""Adding, listing and removing what tests are made of: agents, profiles, scenarios and plugins, with their keys in
.env (never the DB). The CLI and the UI both call these.

ValueError: bad input (a taken name, no such thing). ConnectionError: a check didn't answer; nothing was saved.
"""

import asyncio
import sqlite3
import uuid
from pathlib import Path

from pydantic import BaseModel

from livetest.platform import keys, llm, stt, tts
from livetest.runtime import session, store, tokens
from livetest.runtime.models import Agent, Plugin, Scenario, key_var


def label(kind: type) -> str:
    return kind.__name__.lower()  # agent, profile, scenario, plugin: also the CLI group's name


def secret_vars(kind: type, name: str) -> list[str]:
    """The .env vars holding a plugin's key or an agent's API key and secret."""
    return [key_var(name, w) for w in ("AGENT_KEY", "AGENT_SECRET")] if kind is Agent else [key_var(name)]


def find(db: sqlite3.Connection, kind: type, name: str):
    """A saved agent, profile or plugin by name."""
    if found := {m.name: m for _, m in store.rows(db, kind)}.get(name):
        return found
    raise ValueError(f"no {label(kind)} named '{name}'; see: livetest {label(kind)} list")


def free(db: sqlite3.Connection, kind: type, name: str) -> None:
    """Refuse a taken name, or one that would share another's keys in .env (a-b and a_b)."""
    if taken := [m.name for _, m in store.rows(db, kind) if key_var(m.name) == key_var(name)]:
        raise ValueError(f"{label(kind)} '{taken[0]}' already exists" + ("" if taken[0] == name else f"; '{name}' would share its key"))


def save(db: sqlite3.Connection, model: BaseModel) -> int:
    """Its number (scenarios are picked by it)."""
    try:
        return store.add(db, model)
    except sqlite3.IntegrityError:
        raise ValueError(f"{label(type(model))} '{model.name}' already exists") from None


async def check_plugin(env: Path, plugin: Plugin, key: str | None = None) -> float:
    """One streamed call, with `key` or its saved one: seconds to the first token or audio."""
    check = {"llm": llm.check, "stt": stt.check, "tts": tts.check}[plugin.kind]
    try:
        return await check(plugin.base_url, plugin.model, key or keys.get(env, key_var(plugin.name)),
                           *([plugin.voice_id] if plugin.voice_id else []))
    except ConnectionError as e:
        raise ConnectionError(f"{plugin.model} at {plugin.base_url} didn't answer ({e}); check the URL, model and key") from None


async def add_plugin(db: sqlite3.Connection, env: Path, plugin: Plugin, key: str | None = None) -> tuple[float, bool]:
    """One streamed call first, then save it and its key; the first of its kind becomes the active one.
    Returns (seconds to the first token or audio, whether it's active)."""
    free(db, Plugin, plugin.name)
    secs = await check_plugin(env, plugin, key)
    save(db, plugin)
    if key:
        keys.save(env, key_var(plugin.name), key)
    if first := plugin.kind not in store.active(db):
        store.use(db, plugin.name)
    return secs, first


async def add_agent(db: sqlite3.Connection, env: Path, agent: Agent, key: str | None = None,
                    secret: str | None = None) -> float | None:
    """Prove it works first: get a token, join a fresh room and wait for the agent (one short session); then save it,
    and with --url its API key and secret. Returns seconds until it joined; None: no token source, nothing to check."""
    free(db, Agent, agent.name)
    secs = None
    if agent.sandbox or agent.endpoint or agent.url:
        k, s = (given or keys.get(env, v) for given, v in zip((key, secret), secret_vars(Agent, agent.name)))
        # In a thread: a sandbox or endpoint is an HTTP call, and the UI's loop keeps serving meanwhile.
        url, token = await asyncio.get_running_loop().run_in_executor(
            None, lambda: tokens.connect(agent, f"livetest-{uuid.uuid4().hex[:8]}", key=k, secret=s))
        secs = await session.joins(url, token, k, s)
    save(db, agent)
    for var, value in zip(secret_vars(Agent, agent.name), (key, secret)):
        if value:
            keys.save(env, var, value)
    return secs


def remove(db: sqlite3.Connection, env: Path, kind: type, name: str) -> bool:
    """An agent or plugin takes its keys with it. False: no such name."""
    if not store.remove(db, kind, name):
        return False
    if kind in (Agent, Plugin):
        for var in secret_vars(kind, name):
            keys.remove(env, var)
    return True


def listed(db: sqlite3.Connection, env: Path, kind: type) -> list[dict]:
    """What `list --json` prints and the UI shows: scenarios with their number and last score, plugins with whether
    they're active and whether a key is set (never the key)."""
    on = {p.name for p in store.active(db).values()}
    last = store.last_scores(db) if kind is Scenario else {}
    return [({"number": n} if kind is Scenario else {}) | m.model_dump(mode="json")
            | ({"last": last.get(m.name)} if kind is Scenario else {})
            | ({"active": m.name in on, "key": bool(keys.get(env, key_var(m.name)))} if kind is Plugin else {})
            for n, m in store.rows(db, kind)]
