"""The CLI and the DB behind it (`init`, `agent`, `profile`, `scenario`, `plugin`, migrations), against a throwaway DB."""

import json
import os
import socket
import sqlite3
import sys
from contextlib import closing

import pytest
from dotenv import dotenv_values

from livetest.cli import main
from livetest.platform import keys, llm
from livetest.runtime import session, store, tokens
from livetest.runtime.models import Agent, Plugin, Profile, Scenario


@pytest.fixture
def path(tmp_path):
    return tmp_path / "sub" / "livetest.db"  # parent doesn't exist yet: connect() makes it


@pytest.fixture
def run(path):
    return lambda *a: main(["--db", str(path), *a])


@pytest.fixture
def reachable(monkeypatch):
    """Every LLM answers, so `plugin add` saves without a server. Records (base_url, model, key) per check."""
    seen = []

    async def check(*a):
        seen.append(a)
        return 0.25

    monkeypatch.setattr(llm, "check", check)
    return seen


@pytest.fixture(autouse=True)
def got_token(monkeypatch):
    """Every token source answers, so `agent add` saves without a network. Records connect()'s kwargs per check."""
    seen = []

    def connect(agent, room, profile=None, **kw):
        seen.append(kw)
        if agent.url and not (kw.get("key") and kw.get("secret")):
            raise ValueError("no API key and secret")
        return "wss://x", "token"

    async def joins(*a, **kw):
        return 1.5  # the agent joined

    monkeypatch.setattr(tokens, "connect", connect)
    monkeypatch.setattr(session, "joins", joins)
    return seen


@pytest.fixture
def dead_url():
    with socket.socket() as s:  # a port nothing listens on once closed
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}/v1"


def saved(path, kind):
    """{name: (number, model)} as stored."""
    with closing(store.connect(path)) as conn:
        return {m.name: (n, m) for n, m in store.rows(conn, kind)}


def active(path):
    """{kind: plugin name} of the active plugins."""
    with closing(store.connect(path)) as conn:
        return {k: p.name for k, p in store.active(conn).items()}


def test_init_creates_db(run, path):
    assert run("init") == 0
    assert path.exists()


def test_agent_add_list_rm(run, path):
    assert run("agent", "add", "--name", "baymax", "--sandbox", "sb-1") == 0
    assert run("agent", "add", "--name", "baymax", "--sandbox", "sb-2") == 2  # name taken
    assert saved(path, Agent)["baymax"][1].sandbox == "sb-1"
    assert run("agent", "list") == 0
    assert run("agent", "remove", "--name", "baymax", "-y") == 0
    assert run("agent", "rm", "--name", "baymax", "-y") == 2  # alias works; already gone


def test_agent_add_gets_a_token_first(run, path, got_token, monkeypatch):
    assert run("agent", "add", "--name", "tok") == 0 and got_token == []  # token at run time: nothing to check

    def fails(error):
        def connect(*a, **kw):
            raise error
        return connect

    monkeypatch.setattr(tokens, "connect", fails(ConnectionError("unreachable")))
    assert run("agent", "add", "--name", "a", "--endpoint", "http://x/token") == 3
    monkeypatch.setattr(tokens, "connect", fails(ValueError("endpoint refused")))
    assert run("agent", "add", "--name", "a", "--sandbox", "sb") == 2
    monkeypatch.setattr(tokens, "connect", lambda *a, **kw: ("wss://x", "token"))

    async def never(*a, **kw):
        raise ConnectionError("the agent never joined")

    monkeypatch.setattr(session, "joins", never)  # got a token, but nobody came
    assert run("agent", "add", "--name", "a", "--sandbox", "sb") == 3
    assert list(saved(path, Agent)) == ["tok"]


def test_agent_add_prompt_picks_the_source(run, path, monkeypatch):
    answers = iter(["s", "sb-1", "e", "https://example.com/token", "u", "wss://lk.example.com", "t", ""])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _: "typed")
    for _ in range(4):
        assert run("agent", "add") == 0
    a = {n: m for n, (_, m) in saved(path, Agent).items()}
    assert (a["s"].sandbox, a["e"].endpoint, a["u"].url) == ("sb-1", "https://example.com/token", "wss://lk.example.com")
    assert a["t"].model_dump(exclude_none=True) == {"name": "t"}  # Enter: token at run time


def test_ctrl_c_exits_quietly(run, path, monkeypatch, capsys):
    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", interrupt)
    assert run("agent", "add") == 130
    assert "Traceback" not in capsys.readouterr().err


def test_agent_keys(run, path, got_token, monkeypatch):
    env = path.parent / ".env"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("getpass.getpass", lambda _: pytest.fail("a script must never be asked for a key"))
    assert run("agent", "add", "--name", "dev-agent", "--url", "ws://lk:7880") == 2  # no keys, nobody to ask
    monkeypatch.setenv("LIVETEST_AGENT_KEY_DEV_AGENT", "k-env")  # CI sets them as env vars
    monkeypatch.setenv("LIVETEST_AGENT_SECRET_DEV_AGENT", "s-env")
    assert run("agent", "add", "--name", "dev-agent", "--url", "ws://lk:7880") == 0
    assert got_token[-1] == {"key": "k-env", "secret": "s-env"} and not env.exists()

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    typed = iter(["k-typed", "s-typed"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(typed))
    assert run("agent", "add", "--name", "prod", "--url", "wss://prod.livekit.cloud") == 0
    assert got_token[-1] == {"key": "k-typed", "secret": "s-typed"}
    assert dotenv_values(env) == {"LIVETEST_AGENT_KEY_PROD": "k-typed", "LIVETEST_AGENT_SECRET_PROD": "s-typed"}
    assert not any(b"s-typed" in f.read_bytes() for f in path.parent.iterdir() if f != env)  # DB, WAL
    assert run("agent", "add", "--name", "dev.agent", "--sandbox", "sb") == 2  # would share dev-agent's keys
    assert run("agent", "rm", "--name", "prod", "-y") == 0
    assert dotenv_values(env) == {}  # its keys go with it


def test_remove_needs_confirmation(run, path, monkeypatch):
    run("agent", "add", "--name", "keep", "--sandbox", "sb")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run("agent", "remove", "--name", "keep") == 2  # script without -y: refuse
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run("agent", "remove", "--name", "keep") == 0  # person said no
    assert "keep" in saved(path, Agent)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert run("agent", "remove", "--name", "keep") == 0
    assert saved(path, Agent) == {}


def test_no_action_prints_help_without_touching_db(run, path, capsys):
    assert run() == 0 and "get started" in capsys.readouterr().out
    for group in ("agent", "profile", "scenario", "plugin"):
        assert run(group) == 0 and "actions" in capsys.readouterr().out
    run("agent")
    assert "remove an agent" in capsys.readouterr().out
    assert not path.parent.exists()


def test_one_connection_mode_only(run, path):
    assert run("agent", "add", "--name", "two", "--sandbox", "a", "--url", "ws://x") == 2
    assert saved(path, Agent) == {}


def test_no_terminal_never_prompts(run, path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run("agent", "add") == 2  # no name and nobody to ask
    assert run("scenario", "add", "--scenario", "s") == 2  # no goal and nobody to ask
    assert run("agent", "add", "--name", "tok") == 0  # no connection: token at run time
    assert saved(path, Agent)["tok"][1].sandbox is None


def test_terminal_prompts_for_missing(run, path, monkeypatch):
    answers = iter(["asked", "sb-pasted", "booking", "Move my appointment"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert run("agent", "add") == 0
    assert saved(path, Agent)["asked"][1].sandbox == "sb-pasted"
    assert run("scenario", "add") == 0
    assert saved(path, Scenario)["booking"][1].goal == "Move my appointment"


def test_profile_add_list_rm(run, path, capsys):
    assert run("profile", "add", "--profile", "pro-user", "--participant-name", "Priya",
               "--attribute", "plan=pro", "--attribute", "seats=42",
               "--metadata", '{"user_id": 42}', "--room-metadata", "acme",
               "--dispatch-metadata", '{"job": 1}') == 0
    p = saved(path, Profile)["pro-user"][1]
    assert p.participant.identity == "tester"
    assert p.participant.attributes == {"plan": "pro", "seats": "42"}  # attributes stay strings
    assert p.participant.metadata == {"user_id": 42}  # JSON object kept as an object
    assert p.room.metadata == "acme"  # anything else is a plain string
    assert p.dispatch.metadata == {"job": 1}
    assert run("profile", "add", "--profile", "pro-user") == 2  # name taken
    capsys.readouterr()
    assert run("profile", "list", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["participant"]["name"] == "Priya"
    assert run("profile", "rm", "--profile", "pro-user", "-y") == 0
    assert saved(path, Profile) == {}


def test_scenario_flags(run, path):
    assert run("scenario", "add", "--scenario", "reschedule", "--goal", "Move Tuesday to Friday",
               "--tag", "booking", "--persona", "mood=impatient", "--persona", "interrupts=true",
               "--tool-called", "reschedule", "day=friday", "slot=2", "--tool-not-called", "cancel_booking",
               "--no-tool-errors", "--p99-ms", "1500") == 0
    s = saved(path, Scenario)["reschedule"][1]
    assert s.tags == ["booking"]
    assert s.persona == {"mood": "impatient", "interrupts": True}  # values read as JSON when they parse
    assert (s.max_turns, s.max_minutes) == (10, 5)
    assert [c.type for c in s.expect] == ["tool_called", "tool_not_called", "no_tool_errors", "latency"]
    assert s.expect[0].args == {"day": "friday", "slot": 2}
    assert (s.expect[3].p95_ms, s.expect[3].p99_ms) == (None, 1500)


def test_scenario_numbers_never_reused_and_tags(run, path, capsys):
    for name, tag in [("a", "x"), ("b", "y"), ("c", "x")]:
        assert run("scenario", "add", "--scenario", name, "--goal", "g", "--tag", tag) == 0
    assert run("scenario", "rm", "--scenario", "c", "-y") == 0
    assert run("scenario", "add", "--scenario", "d", "--goal", "g", "--tag", "x") == 0
    assert {name: n for name, (n, _) in saved(path, Scenario).items()} == {"a": 1, "b": 2, "d": 4}
    capsys.readouterr()
    assert run("scenario", "list", "--tag", "x", "--json") == 0
    assert [(s["number"], s["name"]) for s in json.loads(capsys.readouterr().out)] == [(1, "a"), (4, "d")]


@pytest.mark.parametrize("argv", [
    ["scenario", "add", "--scenario", "1-20", "--goal", "g"],  # looks like a selection
    ["profile", "add", "--profile", "my profile"],  # space
    ["profile", "add", "--profile", "p", "--attribute", "plan"],  # not key=value
    ["scenario", "add", "--scenario", "s", "--goal", "g", "--max-turns", "0"],
    ["scenario", "add", "--scenario", "s", "--goal", "g", "--p95-ms", "0"],
])
def test_bad_input_exits_2(run, path, argv):
    assert run(*argv) == 2
    assert saved(path, Profile) == {} and saved(path, Scenario) == {}


def test_old_db_upgrades_and_keeps_agents(run, path):
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as old:  # a DB from before migrations: user_version 0
        old.executescript(store.MIGRATIONS[0] + "INSERT INTO agents (name, data) VALUES ('old', '{}');")
    assert run("scenario", "add", "--scenario", "s", "--goal", "g") == 0
    assert "old" in saved(path, Agent)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(store.MIGRATIONS)


def test_newer_db_refused(run, path):
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 999")
    assert run("agent", "list") == 2


def test_plugin_add_use_list_rm(run, path, reachable, capsys):
    assert run("plugin", "add", "--plugin", "local", "--base-url", "http://localhost:8000/v1", "--model", "qwen") == 0
    assert run("plugin", "add", "--plugin", "groq", "--provider", "groq", "--model", "gpt-oss") == 0
    assert saved(path, Plugin)["groq"][1].base_url == llm.PRESETS["groq"]
    assert active(path) == {"llm": "local"}  # the first of its kind is active
    assert run("plugin", "use", "--plugin", "groq") == 0
    assert active(path) == {"llm": "groq"}  # one active per kind
    capsys.readouterr()
    assert run("plugin", "list", "--json") == 0
    assert {p["name"]: p["active"] for p in json.loads(capsys.readouterr().out)} == {"local": False, "groq": True}
    assert run("plugin", "use", "--plugin", "nope") == 2
    assert run("plugin", "rm", "--plugin", "groq", "-y") == 0
    assert active(path) == {}  # removing the active one leaves none
    assert "local" in saved(path, Plugin)


def test_plugin_unreachable_saves_nothing(run, path, monkeypatch, dead_url, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda _: "sk-typed")
    assert run("plugin", "add", "--plugin", "dead", "--base-url", dead_url, "--model", "m") == 3  # real call, nothing mocked
    assert capsys.readouterr().err.startswith("error:")
    assert saved(path, Plugin) == {} and not (path.parent / ".env").exists()


def test_plugin_keys(run, path, reachable, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda _: "sk-secret-123")
    assert run("plugin", "add", "--plugin", "my-groq", "--provider", "groq", "--model", "m") == 0
    env = path.parent / ".env"  # next to the DB
    assert dotenv_values(env) == {"LIVETEST_KEY_MY_GROQ": "sk-secret-123"}
    assert reachable[-1][2] == "sk-secret-123"  # the check used the typed key
    if os.name == "posix":
        assert env.stat().st_mode & 0o777 == 0o600
    assert not any(b"sk-secret-123" in f.read_bytes() for f in path.parent.iterdir() if f != env)  # DB, WAL
    capsys.readouterr()
    assert run("plugin", "list") == 0
    out = capsys.readouterr().out
    assert "key: set" in out and "sk-secret" not in out
    monkeypatch.setenv("LIVETEST_KEY_MY_GROQ", "from-env")
    assert keys.get(env, "LIVETEST_KEY_MY_GROQ") == "from-env"  # a real env var beats the file
    assert run("plugin", "rm", "--plugin", "my-groq", "-y") == 0
    assert dotenv_values(env) == {}  # its key goes with it


def test_plugin_names_sharing_a_key_refused(run, path, reachable):
    assert run("plugin", "add", "--plugin", "a-b", "--base-url", "http://x/v1", "--model", "m") == 0
    assert run("plugin", "add", "--plugin", "a-b", "--base-url", "http://x/v1", "--model", "m") == 2  # taken
    assert run("plugin", "add", "--plugin", "a_b", "--base-url", "http://x/v1", "--model", "m") == 2  # same key var
    assert len(reachable) == 1  # refused before calling the LLM


def test_plugin_add_prompts_or_fails(run, path, reachable, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("getpass.getpass", lambda _: pytest.fail("a script must never be asked for a key"))
    assert run("plugin", "add", "--plugin", "p", "--model", "m") == 2  # no URL, nobody to ask
    assert run("plugin", "add", "--plugin", "p", "--provider", "groq") == 2  # no model
    assert saved(path, Plugin) == {}
    answers = iter(["asked", "gemini", "gemini-flash"])  # name, provider-or-URL, model
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _: "")  # Enter: no key
    assert run("plugin", "add") == 0
    assert saved(path, Plugin)["asked"][1].base_url == llm.PRESETS["gemini"]
    assert not (path.parent / ".env").exists()


def test_init_offers_an_agent_then_a_tester_llm(run, path, reachable, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert run("init") == 0 and active(path) == {} and saved(path, Agent) == {}  # scripts are told, not asked
    assert "agent add -h" in capsys.readouterr().out
    answers = iter(["y", "ALIVE", "sb-1",  # agent offer, name, source
                    "y", "local", "http://localhost:8000/v1", "qwen"])  # LLM offer, name, URL, model
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _: "")
    assert run("init") == 0
    assert saved(path, Agent)["ALIVE"][1].sandbox == "sb-1" and active(path) == {"llm": "local"}
    assert run("init") == 0  # has both now: asks nothing (answers are used up)
