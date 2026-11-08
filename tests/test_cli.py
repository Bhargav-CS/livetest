"""The CLI and the DB behind it (`init`, `agent`, `profile`, `scenario`, migrations), against a throwaway DB."""

import json
import sqlite3
import sys
from contextlib import closing

import pytest

from livetest.cli import main
from livetest.runtime import store
from livetest.runtime.models import Agent, Profile, Scenario


@pytest.fixture
def path(tmp_path):
    return tmp_path / "sub" / "livetest.db"  # parent doesn't exist yet: connect() makes it


@pytest.fixture
def run(path):
    return lambda *a: main(["--db", str(path), *a])


def saved(path, kind):
    """{name: (number, model)} as stored."""
    with closing(store.connect(path)) as conn:
        return {m.name: (n, m) for n, m in store.rows(conn, kind)}


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
    for group in ("agent", "profile", "scenario"):
        assert run(group) == 0 and "actions" in capsys.readouterr().out
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
