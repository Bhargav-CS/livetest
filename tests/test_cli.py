"""The CLI and the DB behind it (`init`, `agent`, `profile`, `scenario`, `plugin`, `run`, migrations), against a throwaway DB."""

import json
import os
import socket
import sqlite3
import sys
from contextlib import closing

import pytest
from dotenv import dotenv_values

from livetest.cli import main
from livetest.platform import keys, llm, stt, tts
from livetest.runtime import runner, session, store, tokens
from livetest.runtime.models import Agent, Plugin, Profile, Scenario


@pytest.fixture
def path(tmp_path):
    return tmp_path / "sub" / "livetest.db"  # parent doesn't exist yet: connect() makes it


@pytest.fixture
def run(path):
    return lambda *a: main(["--db", str(path), *a])


@pytest.fixture
def reachable(monkeypatch):
    """Every LLM, STT and TTS answers, so `plugin add` saves without a server. Records (base_url, model, key, ...) per check."""
    seen = []

    async def check(*a):
        seen.append(a)
        return 0.25

    for kind in (llm, stt, tts):
        monkeypatch.setattr(kind, "check", check)
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
    for group in ("agent", "profile", "scenario", "plugin", "run"):
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
    assert run("scenario", "add", "--scenario", "s") == 2  # no prompt and nobody to ask
    assert run("agent", "add", "--name", "tok") == 0  # no connection: token at run time
    assert saved(path, Agent)["tok"][1].sandbox is None


def test_terminal_prompts_for_missing(run, path, monkeypatch):
    answers = iter(["asked", "sb-pasted", "booking", "Move my appointment"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert run("agent", "add") == 0
    assert saved(path, Agent)["asked"][1].sandbox == "sb-pasted"
    assert run("scenario", "add") == 0
    assert saved(path, Scenario)["booking"][1].prompt == "Move my appointment"


def test_profile_add_list_rm(run, path, capsys):
    assert run("profile", "add", "--profile", "pro-user", "--participant-name", "Priya",
               "--attribute", "plan=pro", "--attribute", "seats = 42",  # spaces around = are trimmed, as in the UI
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
    prompt = "You are an impatient customer. Move your Tuesday appointment to Friday. Reply with one short turn."
    assert run("scenario", "add", "--scenario", "reschedule", "--prompt", prompt, "--tag", "booking",
               "--tool-called", "reschedule", "day=friday", "slot=2", "--tool-not-called", "cancel_booking",
               "--no-tool-errors", "--p99-ms", "1500") == 0
    s = saved(path, Scenario)["reschedule"][1]
    assert s.tags == ["booking"]
    assert s.prompt == prompt  # what the tester's LLM gets, as written
    assert (s.max_turns, s.max_minutes) == (10, 5)
    assert [c.type for c in s.expect] == ["tool_called", "tool_not_called", "no_tool_errors", "latency"]
    assert s.expect[0].args == {"day": "friday", "slot": 2}
    assert (s.expect[3].p95_ms, s.expect[3].p99_ms) == (None, 1500)


def test_scenario_numbers_never_reused_and_tags(run, path, capsys):
    for name, tag in [("a", "x"), ("b", "y"), ("c", "x")]:
        assert run("scenario", "add", "--scenario", name, "--prompt", "p", "--tag", tag) == 0
    assert run("scenario", "rm", "--scenario", "c", "-y") == 0
    assert run("scenario", "add", "--scenario", "d", "--prompt", "p", "--tag", "x") == 0
    assert {name: n for name, (n, _) in saved(path, Scenario).items()} == {"a": 1, "b": 2, "d": 4}
    capsys.readouterr()
    assert run("scenario", "list", "--tag", "x", "--json") == 0
    assert [(s["number"], s["name"]) for s in json.loads(capsys.readouterr().out)] == [(1, "a"), (4, "d")]


@pytest.mark.parametrize("argv", [
    ["scenario", "add", "--scenario", "1-20", "--prompt", "p"],  # looks like a selection
    ["profile", "add", "--profile", "my profile"],  # space
    ["profile", "add", "--profile", "p", "--attribute", "plan"],  # not key=value
    ["scenario", "add", "--scenario", "s", "--prompt", "p", "--max-turns", "0"],
    ["scenario", "add", "--scenario", "s", "--prompt", "p", "--p95-ms", "0"],
])
def test_bad_input_exits_2(run, path, argv):
    assert run(*argv) == 2
    assert saved(path, Profile) == {} and saved(path, Scenario) == {}


def test_newer_db_refused(run, path):
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 999")
    assert run("agent", "list") == 2


def test_plugin_add_use_list_rm(run, path, reachable, capsys):
    assert run("plugin", "add", "--kind", "llm", "--plugin", "local", "--base-url", "http://localhost:8000/v1", "--model", "qwen") == 0
    assert run("plugin", "add", "--kind", "llm", "--plugin", "groq", "--provider", "groq", "--model", "gpt-oss") == 0
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


@pytest.mark.parametrize("kind", ["llm", "stt", "tts"])
def test_plugin_unreachable_saves_nothing(run, path, monkeypatch, dead_url, capsys, kind):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda _: "sk-typed")
    assert run("plugin", "add", "--kind", kind, "--plugin", "dead", "--base-url", dead_url, "--model", "m") == 3  # real call
    assert capsys.readouterr().err.startswith("error:")
    assert saved(path, Plugin) == {} and not (path.parent / ".env").exists()


def test_voice_plugins(run, path, reachable):
    assert run("plugin", "add", "--kind", "stt", "--plugin", "moonshine", "--base-url", "http://h:9092/v1",
               "--model", "moonshine-base") == 0
    assert run("plugin", "add", "--kind", "tts", "--plugin", "kokoro", "--base-url", "http://h:8881/v1",
               "--model", "kokoro-7m", "--voice-id", "af_msa") == 0
    assert reachable[-1] == ("http://h:8881/v1", "kokoro-7m", None, "af_msa")  # checked with its voice
    assert saved(path, Plugin)["kokoro"][1].voice_id == "af_msa"
    assert active(path) == {"stt": "moonshine", "tts": "kokoro"}  # the first of each kind
    assert run("plugin", "add", "--kind", "stt", "--plugin", "s2", "--base-url", "http://h/v1", "--model", "m",
               "--voice-id", "x") == 2  # only a TTS has a voice
    assert "s2" not in saved(path, Plugin)


def test_plugin_keys(run, path, reachable, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda _: "sk-secret-123")
    assert run("plugin", "add", "--kind", "llm", "--plugin", "my-groq", "--provider", "groq", "--model", "m") == 0
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
    assert run("plugin", "add", "--kind", "llm", "--plugin", "a-b", "--base-url", "http://x/v1", "--model", "m") == 0
    assert run("plugin", "add", "--kind", "llm", "--plugin", "a-b", "--base-url", "http://x/v1", "--model", "m") == 2  # taken
    assert run("plugin", "add", "--kind", "llm", "--plugin", "a_b", "--base-url", "http://x/v1", "--model", "m") == 2  # same key var
    assert len(reachable) == 1  # refused before calling the LLM


def test_plugin_add_prompts_or_fails(run, path, reachable, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("getpass.getpass", lambda _: pytest.fail("a script must never be asked for a key"))
    assert run("plugin", "add", "--kind", "llm", "--plugin", "p", "--model", "m") == 2  # no URL, nobody to ask
    assert run("plugin", "add", "--kind", "llm", "--plugin", "p", "--provider", "groq") == 2  # no model
    assert run("plugin", "add", "--plugin", "p", "--provider", "groq", "--model", "m") == 2  # no kind
    assert saved(path, Plugin) == {}
    answers = iter(["llm", "asked", "gemini", "gemini-flash"])  # kind, name, provider-or-URL, model
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda _: "")  # Enter: no key
    assert run("plugin", "add") == 0
    assert saved(path, Plugin)["asked"][1].base_url == llm.PRESETS["gemini"]
    assert not (path.parent / ".env").exists()


def test_init_says_whats_next(run, path, capsys):
    """The install script runs init: it asks nothing and starts nothing, and names what's still missing."""
    def said():
        assert run("init") == 0
        return capsys.readouterr().out
    first = said()
    assert first.startswith(f"livetest is set up: {path}\n\nNext, use the web UI:\n  livetest ui show\nor the terminal:\n")
    assert [line.split()[1] for line in first.splitlines()[5:]] == ["agent", "plugin", "scenario", "run"]
    with closing(store.connect(path)) as db:
        store.add(db, Agent(name="bot", sandbox="sb"))
        store.add(db, Plugin(name="p", base_url="http://x/v1", model="m"))
        store.use(db, "p")
    assert [line.split()[1] for line in said().splitlines()[5:]] == ["scenario", "run"]  # done steps drop out
    with closing(store.connect(path)) as db:
        store.add(db, Scenario(name="s", prompt="p"))
        store.start_run(db, "1", "bot", None, [])
    assert said().endswith("Everything is set up. See your runs: livetest ui show, or livetest run list\n")


@pytest.fixture
def ready(run, reachable):
    """An agent, scenarios 1-3 and a tester LLM: enough for `run start`."""
    assert run("agent", "add", "--name", "bot", "--sandbox", "sb") == 0
    for name in ("a", "b", "c"):
        assert run("scenario", "add", "--scenario", name, "--prompt", "p") == 0
    assert run("plugin", "add", "--kind", "llm", "--plugin", "p", "--base-url", "http://x/v1", "--model", "m") == 0


def finishes(monkeypatch, *outcomes):
    """runner.run ends each test with the next (status, passed); no LiveKit. Records each run's parallel."""
    seen = []

    async def fake(db, tests, connect, tester, parallel, key, secret, voice=None, recordings=None, opening=None):
        seen.append((parallel, recordings) if voice else parallel)
        for (result, _, room), (status, passed) in zip(tests, outcomes):
            assert room.startswith("livetest-") and store.begin(db, result)
            store.finish_result(db, result, status, "max_turns", None if passed is None else [{"check": "c", "passed": passed}],
                                USAGE)

    monkeypatch.setattr(runner, "run", fake)
    return seen


USAGE = {"agent": [{"kind": "llm", "provider": "api.groq.com", "model": "m", "input_tokens": 900, "input_cached_tokens": 512,
                    "output_tokens": 40}, {"kind": "stt", "provider": "Deepgram", "model": "nova-3", "audio_duration": 2.5}],
         "tester": [{"kind": "llm", "provider": "x", "model": "m", "input_tokens": 250, "input_cached_tokens": 0, "output_tokens": 12}]}


def test_run_shows_usage(run, ready, monkeypatch, capsys):
    """Counts, no cost: each test's LLM tokens, then the run's totals; every model's counts in --json."""
    finishes(monkeypatch, ("done", True), ("done", True))
    capsys.readouterr()
    assert run("run", "start", "--name", "bot", "--scenario", "1-2", "-y") == 0
    out = capsys.readouterr().out
    assert out.count("tokens as reported by LiveKit: agent 900 in (512 cached) / 40 out, tester 250 in / 12 out") == 2
    assert "tokens as reported by LiveKit: agent 1800 in (1024 cached) / 80 out, tester 500 in / 24 out" in out
    assert json.loads(listed(run, capsys, "--run", "latest")[0]["usage"]) == USAGE


def listed(run, capsys, *flags):
    capsys.readouterr()
    assert run("run", "list", "--json", *flags) == 0
    return json.loads(capsys.readouterr().out)


def test_run_start_checks_tester_plugins(run, ready, monkeypatch, capsys):
    """A tester LLM that stopped answering since `plugin add` fails the run before anything is saved."""
    async def dead(*a):
        raise ConnectionError("refused")

    monkeypatch.setattr(llm, "check", dead)
    capsys.readouterr()
    assert run("run", "start", "--name", "bot", "--scenario", "1", "-y") == 3
    assert "m at http://x/v1 didn't answer (refused); check the URL, model and key" in capsys.readouterr().err
    assert listed(run, capsys) == []


def test_run_start_and_list(run, ready, monkeypatch, capsys):
    seen = finishes(monkeypatch, *[("done", True)] * 3)
    assert run("run", "start", "--name", "bot", "--scenario", "1-3", "-y") == 0
    assert run("run", "start", "--name", "bot", "--scenario", "ax2", "c", "--run", "nightly-1", "--parallel", "1", "-y") == 0
    assert seen == [4, 1]  # parallel defaults to 4
    assert run("run", "start", "--name", "bot", "--scenario", "1", "-y") == 0
    runs = listed(run, capsys)
    assert [(r["id"], r["agent"], r["status"], r["tests"], r["passed"]) for r in runs] == [
        ("1", "bot", "done", 3, 3), ("nightly-1", "bot", "done", 3, 3), ("2", "bot", "done", 1, 1)]  # next free number
    tests = listed(run, capsys, "--run", "nightly-1")
    assert [(t["test"], json.loads(t["scenario"])["name"]) for t in tests] == [(1, "a"), (2, "a"), (3, "c")]
    assert listed(run, capsys, "--run", "latest")[0]["run"] == "2"
    assert run("run", "list") == 0 and "nightly-1" in capsys.readouterr().out


@pytest.mark.parametrize("outcomes, code", [
    ([("done", True)], 0),
    ([("done", True), ("done", False)], 1),
    ([("error", None)], 1),
    ([("stopped", None), ("done", True)], 0),  # stopped tests count for neither
    ([("never_joined", None), ("done", False)], 3),  # nothing tested beats a fail
    ([("no_session_host", None)], 3),
])
def test_run_exit_codes(run, ready, monkeypatch, capsys, outcomes, code):
    finishes(monkeypatch, *outcomes)
    assert run("run", "start", "--name", "bot", "--scenario", f"1-{len(outcomes)}", "-y") == code
    out = capsys.readouterr().out
    if ("error", None) in outcomes:  # says where the details are and where to report a bug
        assert "run logs --run 1 --test 1" in out and "errors.log" in out and "github.com" in out


def test_run_start_refuses(run, path, ready, monkeypatch, capsys):
    finishes(monkeypatch, ("done", True))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    start = lambda *a: run("run", "start", "--name", "bot", "--scenario", "1", *a)
    assert start() == 2  # a script without -y
    capsys.readouterr()
    assert start("--run", "nightly 1", "-y") == 2 and "nightly-1" in capsys.readouterr().err
    assert start("--parallel", "0", "-y") == 2
    assert start("--llm", "nope", "-y") == 2
    assert run("run", "start", "--name", "nobody", "--scenario", "1", "-y") == 2
    assert run("run", "start", "--name", "bot", "--scenario", "99", "-y") == 2
    assert run("run", "start", "--name", "bot", "-y") == 2  # no selection: nothing runs by accident
    assert run("agent", "add", "--name", "tok") == 0  # no token source
    capsys.readouterr()
    assert run("run", "start", "--name", "tok", "--scenario", "1-2", "--token", "t", "-y") == 2
    err = capsys.readouterr().err  # the next steps, as commands
    assert "runs one test" in err and "livetest agent add --name tok --sandbox" in err
    assert listed(run, capsys) == []
    assert start("--run", "r1", "-y") == 0 and start("--run", "r1", "-y") == 2  # taken


def test_run_start_voice(run, path, ready, monkeypatch, capsys):
    """--voice needs a tester STT and TTS; it records each test unless --no-recordings."""
    seen = finishes(monkeypatch, ("done", True))
    start = lambda *a: run("run", "start", "--name", "bot", "--scenario", "1", "--voice", "-y", *a)
    capsys.readouterr()
    assert start() == 2 and "plugin add --kind stt" in capsys.readouterr().err
    assert run("plugin", "add", "--kind", "stt", "--plugin", "s", "--base-url", "http://x/v1", "--model", "m") == 0
    capsys.readouterr()
    assert start() == 2 and "plugin add --kind tts" in capsys.readouterr().err
    assert run("plugin", "add", "--kind", "tts", "--plugin", "t", "--base-url", "http://x/v1", "--model", "m") == 0
    assert start("--stt", "nope") == 2 and start("--tts", "nope") == 2
    assert listed(run, capsys) == []  # refused before anything was saved
    assert start() == 0
    folder = path.parent / "recordings" / "run-1"
    assert "voice mode" in (out := capsys.readouterr().out) and f"recording: on ({folder}" in out
    assert start("--no-recordings") == 0
    assert seen == [(4, folder), (4, None)]
    assert [r["mode"] for r in listed(run, capsys)] == ["voice", "voice"]
    assert run("run", "start", "--name", "bot", "--scenario", "1", "-y") == 0  # text is the default
    assert listed(run, capsys)[-1]["mode"] == "text"


def test_run_start_opening(run, ready, monkeypatch, capsys):
    """Left out, the agent opens; with text, the tester says exactly that. The tester never writes its own opening."""
    got = []

    async def fake(db, tests, *a, opening=None, **kw):
        got.append(opening)
        for result, _, _ in tests:
            store.begin(db, result)
            store.finish_result(db, result, "done", "max_turns", [{"check": "c", "passed": True}])

    monkeypatch.setattr(runner, "run", fake)
    start = lambda *a: run("run", "start", "--name", "bot", "--scenario", "1", "-y", *a)
    capsys.readouterr()
    assert start() == 0 and "opens: agent" in capsys.readouterr().out
    with pytest.raises(SystemExit):  # argparse: the flag needs its text
        start("--start-conversation")
    assert start("--start-conversation", "Hi, I need to reschedule") == 0
    assert 'opens: tester ("Hi, I need to reschedule")' in capsys.readouterr().out
    assert got == [None, "Hi, I need to reschedule"]


def test_run_start_fails_fast_on_tokens(run, ready, monkeypatch, capsys):
    finishes(monkeypatch, ("done", True))

    def refuse(*a, **kw):
        raise ValueError("the token doesn't match the profile")

    monkeypatch.setattr(tokens, "connect", refuse)
    assert run("run", "start", "--name", "bot", "--scenario", "1", "-y") == 2
    assert listed(run, capsys) == []  # checked before anything was saved


def test_run_stop(run, path, capsys):
    s = Scenario(name="s", prompt="p")
    with closing(store.connect(path)) as db:
        first, *_ = store.start_run(db, "r", "bot", None, [(s, "x")] * 3)
        store.begin(db, first)
    assert run("run", "stop", "--run", "latest", "--test", "2", "-y") == 0
    assert run("run", "stop", "--run", "r", "-y") == 0
    assert [t["status"] for t in listed(run, capsys, "--run", "r")] == ["stopping", "stopped", "stopped"]
    assert listed(run, capsys)[0]["status"] == "stopping"  # until test 1 ends: a run's status comes from its tests
    assert run("run", "stop", "--run", "r", "--test", "2", "-y") == 2  # nothing left to stop
    assert run("run", "stop", "--run", "nope", "-y") == 2


def test_run_logs(run, path, capsys):
    s = Scenario(name="s", prompt="p")
    with closing(store.connect(path)) as db:
        one, two = store.start_run(db, "r", "bot", None, [(s, "x"), (s, "y")])
        store.event(db, one, "tester", "tester_turn", {"text": "hello"})
        store.event(db, one, "tester", "tester_heard", {"text": "Hi their"})  # voice: the tester's STT of the agent
        store.event(db, one, "agent", "user_input_transcribed", {"user_input_transcribed": {"transcript": "hel"}})
        store.event(db, one, "agent", "session_usage_updated", {"session_usage_updated": {"usage": {"model_usage": []}}})
        for role, text in (("USER", "hello"), ("ASSISTANT", "Hi there")):
            store.event(db, one, "agent", "conversation_item_added", {"conversation_item_added": {"item": {
                "message": {"role": role, "content": [{"text": text}]}}}})
        store.event(db, two, "log", "INFO", {"logger": "livetest.runner", "message": "agent joined"})
        store.event(db, two, "log", "ERROR", {"logger": "livetest.runner", "message": "test failed"})
        for n in (one, two):  # finished: logs prints and returns instead of following
            store.finish_result(db, n, "done", "max_turns")
    capsys.readouterr()
    assert run("run", "logs", "--run", "r") == 0
    out = capsys.readouterr().out
    assert all(x in out for x in ("tester: hello", "tester_heard: Hi their", "agent_bot_heard: hello", "agent_bot: Hi there",
                                 "agent joined", "test failed"))
    # Partial transcripts repeat the _heard line; usage is cumulative and on the verdict. Both stay in --json and the DB.
    assert "user_input_transcribed" not in out and "session_usage_updated" not in out
    assert run("run", "logs", "--run", "r", "--test", "2", "--level", "WARNING") == 0
    out = capsys.readouterr().out
    assert "test failed" in out and "agent joined" not in out and "hello" not in out
    assert run("run", "logs", "--run", "r", "--json") == 0
    assert [json.loads(line)["type"] for line in capsys.readouterr().out.splitlines()] == [
        "tester_turn", "tester_heard", "user_input_transcribed", "session_usage_updated", "conversation_item_added",
        "conversation_item_added", "INFO", "ERROR"]


def test_last_agent_error_under_a_failed_verdict(run, path, capsys):
    s = Scenario(name="s", prompt="p")
    with closing(store.connect(path)) as db:
        bad, good = store.start_run(db, "r", "bot", None, [(s, "x"), (s, "y")])
        for n in (bad, good):
            for msg in ("429 rate limit", "tts_error: no audio frames\nwere pushed"):
                store.event(db, n, "agent", "error", {"error": {"message": msg}})
        store.finish_result(db, bad, "done", "no_reply", [{"check": "tool_called", "passed": False}])
        store.finish_result(db, good, "done", "max_turns", [{"check": "tool_called", "passed": True}])
    capsys.readouterr()
    assert run("run", "list", "--run", "r") == 0
    out = capsys.readouterr().out
    assert out.count("last agent error: tts_error: no audio frames were pushed") == 1  # the failed one; one line
    assert "429" not in out


def test_output_never_crashes_on_unicode(run, path, monkeypatch):
    """A Windows console (cp1252) can't print every character an agent says; replaced, not a crash."""
    import io

    s = Scenario(name="s", prompt="p")
    with closing(store.connect(path)) as db:
        n, = store.start_run(db, "r", "bot", None, [(s, "x")])
        store.event(db, n, "tester", "tester_heard", {"text": "QuantumLoop\u202fAI since April\u202f2025"})
        store.finish_result(db, n, "done", "max_turns")
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    assert run("run", "logs", "--run", "r") == 0
    out.flush()
    assert b"QuantumLoop?AI" in out.buffer.getvalue()


def test_tester_http_lines_stay_out(run):
    """The tester's own HTTP calls (httpx, under the OpenAI plugin) would be an INFO line per request in every test."""
    import logging

    assert run("agent", "list") == 0  # sets logging up
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


def test_crash_goes_to_errors_log(run, path, monkeypatch, capsys):
    def boom(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "rows", boom)
    assert run("agent", "list") == 1
    err = capsys.readouterr().err
    assert "errors.log" in err and "github.com" in err and "Traceback" not in err  # no traceback without -v
    log = (path.parent / "errors.log").read_text()
    assert "Traceback" in log and "boom" in log
