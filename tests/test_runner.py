"""One text-mode test, end to end. The tester LLM is a local fake OpenAI server saying scripted lines (no credits).

Live part: a self-hosted LiveKit server (`podman kube play --replace dev/livekit-server.yaml`) and the stub agent,
which these tests start themselves. Skipped unless set:

    $env:LIVETEST_SELFHOST="ws://<podman machine ip>:7880"; uv run pytest tests/test_runner.py

Voice tests also need a streaming STT and a TTS, OpenAI-compatible; private-ai's Moonshine and Kokoro-7M in dev. The
stub agent uses them too (so they cost nothing):

    $env:LIVETEST_LIVE_STT_URL="http://<host>:9092/v1"; $env:LIVETEST_LIVE_TTS_URL="http://<host>:8881/v1"
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from livetest.platform import llm, stt, tts
from livetest.runtime import runner, store, tokens
from livetest.runtime.models import Agent, Profile, Scenario

URL, KEY, SECRET = os.getenv("LIVETEST_SELFHOST"), "devkey", "secret"  # livekit-server --dev
selfhost = pytest.mark.skipif(not URL, reason="set LIVETEST_SELFHOST=ws://<podman machine ip>:7880")
STT_URL, TTS_URL = os.getenv("LIVETEST_LIVE_STT_URL"), os.getenv("LIVETEST_LIVE_TTS_URL")
voiced = pytest.mark.skipif(not (URL and STT_URL and TTS_URL), reason="set LIVETEST_SELFHOST, LIVETEST_LIVE_STT_URL "
                                                                       "and LIVETEST_LIVE_TTS_URL")
SILENT = Profile(name="silent", dispatch={"metadata": "silent"})  # the stub waits for the tester


@pytest.fixture
def db(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    yield conn
    conn.close()


@pytest.fixture
def tester():
    """`tester(*lines)` gives an LLM that says `lines` in order, streamed in two pieces. `tester.asked` holds each request."""
    lines, asked = deque(), []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            asked.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            text = lines.popleft() if lines else "ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for piece in (text[:2], text[2:]):
                chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "fake",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            usage = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "fake", "choices": [],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}  # include_usage's last chunk
            self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def make(*say):
        lines.extend(say)
        return llm.llm(f"http://127.0.0.1:{srv.server_port}/v1", "fake", None)

    make.asked, make.url = asked, f"http://127.0.0.1:{srv.server_port}/v1"
    yield make
    srv.shutdown()


@pytest.fixture(scope="module")
def stub(tmp_path_factory):
    out = tmp_path_factory.mktemp("stub") / "agent.log"
    env = os.environ | {"LIVEKIT_URL": URL, "LIVEKIT_API_KEY": KEY, "LIVEKIT_API_SECRET": SECRET}
    with open(out, "w") as f:
        p = subprocess.Popen([sys.executable, str(Path(__file__).with_name("stub_agent.py")), "start"],
                             stdout=f, stderr=subprocess.STDOUT, env=env)
    try:
        deadline = time.time() + 60
        while "registered worker" not in out.read_text(errors="replace"):
            assert p.poll() is None and time.time() < deadline, out.read_text(errors="replace")
            time.sleep(0.5)
        yield
    finally:
        p.terminate()
        p.wait(30)


def connect(agent="livetest-stub", profile=None):
    return lambda room: tokens.connect(Agent(name=agent, url=URL), room, profile, key=KEY, secret=SECRET)


def voices():
    return stt.stt(STT_URL, "moonshine-base", None), tts.tts(TTS_URL, "kokoro-7m", None, "af_msa")


def run(db, tester_llm, scenario, agent="livetest-stub", voice=None, recordings=None, opening=None, profile=None):
    """One test: the stub greets and the tester's LLM replies, unless `opening` (`--start-conversation`)."""
    room = f"livetest-{uuid.uuid4().hex[:8]}"
    n, = store.start_run(db, room, agent, None, [(scenario, room)])

    async def go():
        try:
            await runner.run_test(db, n, scenario, room, connect(agent, profile), tester_llm, KEY, SECRET, voice,
                                  recordings, opening)
        finally:
            await tester_llm.aclose()
            for model in voice or ():
                await model.aclose()

    asyncio.run(go())
    return store.result(db, n), store.events(db, n)


def test_selection():
    pool = [(n, Scenario(name=name, prompt="p")) for n, name in [(1, "a"), (2, "b"), (4, "fix2"), (7, "all")]]
    pick = lambda *items: [n for n, _ in runner.select(list(items), pool)]
    assert pick("1") == [1] and pick("b") == [2]
    assert pick("1-4") == [1, 2, 4]  # skips the removed 3
    assert pick("b", "1-2x2") == [2, 1, 2, 1, 2]  # overlaps add up
    assert pick("fix2") == [4] and pick("fix2x2") == [4, 4]  # a whole name is never split into name + xN
    assert pick("all") == [1, 2, 4, 7] and pick("allx2") == [1, 2, 4, 7] * 2  # the keyword beats a scenario named all
    for bad in ("3", "8-9", "nope", "1x0", "x2", "²"):
        with pytest.raises(ValueError):
            runner.select([bad], pool)


def test_run_ids():
    for ok in ("17", "nightly-1", "a.b_c"):
        assert runner.run_id(ok) == ok
    with pytest.raises(ValueError, match="nightly-1"):  # says what to use instead
        runner.run_id("nightly 1")
    for bad in ("", "x" * 65, "latest", "a/b"):
        with pytest.raises(ValueError):
            runner.run_id(bad)


def test_logs_land_on_their_own_test(db):
    s = Scenario(name="s", prompt="p")
    a, b = store.start_run(db, "r", "agent", None, [(s, "ra"), (s, "rb")])
    log = logging.getLogger("livetest.test")
    log.setLevel(logging.INFO)

    async def one(n):
        with runner.logged(db, n):
            await asyncio.sleep(0)  # let the other test's task run in between
            log.info("from %s", n)
            await asyncio.sleep(0)

    async def both():
        await asyncio.gather(one(a), one(b))

    asyncio.run(both())
    log.info("outside any test")
    for n in (a, b):
        assert [(src, t, d["message"]) for _, src, t, d in store.events(db, n)] == [("log", "INFO", f"from {n}")]


@selfhost
def test_max_turns(db, stub, tester, caplog):
    caplog.set_level(logging.INFO)  # what the CLI's logging setup does
    res, evs = run(db, tester("hello", "please use the tool", "thanks"), Scenario(name="s", prompt="p", max_turns=3))
    assert (res["status"], res["ended"], res["room"].startswith("livetest-"), res["passed"]) == ("done", "max_turns", True, 1)
    of = lambda kind: [d for _, _, t, d in evs if t == kind]
    assert [d["text"] for d in of("tester_turn")] == ["hello", "please use the tool", "thanks"]
    info = of("agent_info")[0]
    assert "Stub agent" in info["instructions"] and "lookup" in json.dumps(info["tools"])
    assert len(of("tool_execution_updated")) == 2  # started, ended: the tool's real duration
    # The tester's LLM gets exactly the scenario's prompt and the agent's words: nothing livetest wrote.
    assert tester.asked[0]["messages"] == [{"role": "system", "content": "p"},
                                           {"role": "user", "content": "Hi, how can I help?"}]
    # The stub's LLM is fake, so its usage has no llm (with voice tests on, only its STT); the tester's adds up,
    # counted as LiveKit counts the agent's. The agent's usage is cumulative: stored once, as the test ends.
    usage = json.loads(res["usage"])
    assert "llm" not in {u["kind"] for u in usage["agent"]}
    assert usage["tester"] == [{"kind": "llm", "provider": tester.url.split("/")[2], "model": "fake",
                                "input_tokens": 30, "output_tokens": 6}]
    assert len(of("session_usage_updated")) == 1
    assert {"framework", "session_state", "chat_history", "seen", "conversation_item_added"} <= {t for _, _, t, _ in evs}
    assert any(src == "log" for _, src, _, _ in evs)
    # The agent's replies reach the tester as the other side of its conversation.
    assert {"role": "user", "content": "Done, I found it."} in tester.asked[-1]["messages"]


@selfhost
def test_agent_leaves(db, stub, tester):
    res, _ = run(db, tester("hi", "bye", "still there?"), Scenario(name="s", prompt="p", max_turns=5))
    assert (res["status"], res["ended"]) == ("done", "agent_left")


@selfhost
def test_max_minutes(db, stub, tester):
    res, _ = run(db, tester(*["please use the tool"] * 20), Scenario(name="s", prompt="p", max_turns=20, max_minutes=0.05))
    assert (res["status"], res["ended"]) == ("done", "max_minutes")


@selfhost
def test_never_joined(db, tester):  # waits the full 30 s
    res, evs = run(db, tester(), Scenario(name="s", prompt="p"), agent="nobody-livetest")
    assert res["status"] == "never_joined" and "never joined" in res["ended"]


@selfhost
def test_parallel_run_from_the_cli(tmp_path, stub, tester, monkeypatch, capsys):
    from livetest.cli import main

    monkeypatch.setenv("LIVETEST_AGENT_KEY_LIVETEST_STUB", KEY)
    monkeypatch.setenv("LIVETEST_AGENT_SECRET_LIVETEST_STUB", SECRET)
    cli = lambda *a: main(["--db", str(tmp_path / "t.db"), *a])
    assert cli("agent", "add", "--name", "livetest-stub", "--url", URL) == 0
    assert cli("plugin", "add", "--kind", "llm", "--plugin", "fake", "--base-url", tester.url, "--model", "fake") == 0
    for s in ("a", "b", "c"):  # one turn each, to keep it short
        assert cli("scenario", "add", "--scenario", s, "--prompt", "p", "--max-turns", "1") == 0
    assert cli("run", "start", "--name", "livetest-stub", "--scenario", "all", "--parallel", "2", "-y") == 0
    capsys.readouterr()
    assert cli("run", "list", "--json") == 0
    assert {k: v for k, v in json.loads(capsys.readouterr().out)[0].items()
            if k in ("id", "status", "tests", "passed")} == {"id": "1", "status": "done", "tests": 3, "passed": 3}


@selfhost
@pytest.mark.parametrize("line", ["please fail", "be slow"])
def test_agent_doesnt_reply(db, stub, tester, line):
    """The agent errors out of a turn, or takes over 10 s to start: the test ends there as no_reply (not a livetest
    error) and fails, though the scenario sets no checks."""
    res, _ = run(db, tester("hello", line, "anyone there?"), Scenario(name="s", prompt="p", max_turns=3))
    assert (res["status"], res["ended"], res["passed"], res["checks"]) == ("done", "no_reply", 0, "[]")


@selfhost
def test_ctrl_c_stops_the_run(tmp_path, stub, tester, monkeypatch):
    import _thread
    import signal

    from livetest.cli import main

    monkeypatch.setenv("LIVETEST_AGENT_KEY_LIVETEST_STUB", KEY)
    monkeypatch.setenv("LIVETEST_AGENT_SECRET_LIVETEST_STUB", SECRET)
    path = tmp_path / "t.db"
    cli = lambda *a: main(["--db", str(path), *a])
    assert cli("agent", "add", "--name", "livetest-stub", "--url", URL) == 0
    assert cli("plugin", "add", "--kind", "llm", "--plugin", "fake", "--base-url", tester.url, "--model", "fake") == 0
    assert cli("scenario", "add", "--scenario", "long", "--prompt", "p", "--max-turns", "20") == 0
    tester(*["please use the tool"] * 40)

    def press_ctrl_c():  # once the first test is talking; the same signal a real Ctrl+C sends
        with closing(store.connect(path)) as db:
            while not store.run_events(db, "1"):
                time.sleep(0.2)
        time.sleep(1)
        _thread.interrupt_main(signal.SIGINT)

    threading.Thread(target=press_ctrl_c, daemon=True).start()
    assert cli("run", "start", "--name", "livetest-stub", "--scenario", "longx2", "--parallel", "1", "-y") == 130
    with closing(store.connect(path)) as db:
        assert [r["status"] for r in store.results(db, "1")] == ["stopped", "stopped"]
        assert store.runs(db, "1")[0]["status"] == "stopped"
        assert store.events(db, store.results(db, "1")[0]["id"])  # kept what it recorded


@selfhost
def test_stop(db, stub, tester):
    """Stopping a run: the running test ends cleanly and keeps its events; the queued one never starts."""
    s = Scenario(name="s", prompt="p", max_turns=20)
    tests = [(s, f"livetest-{uuid.uuid4().hex[:8]}") for _ in range(2)]
    first, second = store.start_run(db, "stop-me", "livetest-stub", None, tests)
    tester_llm = tester(*["please use the tool"] * 20)

    async def go():
        going = asyncio.create_task(runner.run(db, [(first, s, tests[0][1]), (second, s, tests[1][1])],
                                               connect(), tester_llm, 1, KEY, SECRET))
        while not any(t == "tester_turn" for _, _, t, _ in store.events(db, first)):
            await asyncio.sleep(0.2)
        assert store.stop(db, "stop-me") == 2
        await going
        await tester_llm.aclose()

    asyncio.run(go())
    a, b = store.result(db, first), store.result(db, second)
    assert (a["status"], a["passed"], b["status"]) == ("stopped", None, "stopped")
    assert store.events(db, first) and not store.events(db, second)
    assert store.runs(db, "stop-me")[0]["status"] == "stopped"


@voiced
def test_voice(db, stub, tester, tmp_path):
    """The same conversation by voice: the tester hears the greeting out, then its TTS speaks, the agent's STT hears it
    and back. Recorded."""
    import av

    res, evs = run(db, tester("hello", "please use the tool", "thanks"), Scenario(name="s", prompt="p", max_turns=3),
                   voice=voices(), recordings=tmp_path)
    assert (res["status"], res["ended"]) == ("done", "max_turns"), res
    of = lambda kind: [d for _, _, t, d in evs if t == kind]
    assert [d["text"] for d in of("tester_turn")] == ["hello", "please use the tool", "thanks"]  # no 4th turn
    # Whether the agent cut the tester off: its line may then hold more than was spoken (no transcript synchronizer).
    assert all(d["interrupted"] is False for d in of("tester_turn"))  # the stub never talks over the tester
    # The tester's STT heard the agent's TTS (Moonshine sometimes hears "canned reply" as "Can Reply").
    assert any("reply" in d["text"].lower() for d in of("tester_heard"))
    assert of("user_input_transcribed")  # and the agent's STT heard the tester
    assert len(of("tool_execution_updated")) == 2  # "use the tool", by voice
    # The tester's own states, for its tester_turn spans: it thinks (its LLM) and speaks (its TTS); it hears the agent.
    assert {"thinking", "speaking"} <= {d["new_state"] for d in of("tester_state_changed")}
    assert "speaking" in {d["new_state"] for d in of("tester_user_state_changed")}
    # LiveKit's timings on the tester's clock: when it spoke, and when it heard the agent start (the reply gap's ends).
    assert all({"started_speaking_at", "stopped_speaking_at"} <= d["metrics"].keys() for d in of("tester_turn"))
    assert all("started_speaking_at" in d["metrics"] for d in of("tester_heard"))
    assert res["passed"] == 1, res["checks"]  # latency runs from the end of the tester's speech
    greeted = next(ts for ts, _, t, d in evs if t == "conversation_item_added"
                   and d[t]["item"].get("message", {}).get("role") == "ASSISTANT")
    assert greeted < next(ts for ts, _, t, _ in evs if t == "tester_turn")  # no talking over the greeting
    assert "help" in json.dumps(tester.asked[0]["messages"]).lower()  # heard live, or from the agent's history
    assert Path(res["recording"]) == tmp_path / "test-1.ogg" and res["recording_started_at"]
    with av.open(res["recording"]) as f:  # stereo: channel 0 the agent as heard, 1 the tester as spoken
        assert f.streams.audio[0].channels == 2 and f.duration / av.time_base > 3
    assert {u["kind"] for u in json.loads(res["usage"])["tester"]} >= {"llm", "tts"}


@voiced
def test_voice_without_recording(db, stub, tester):
    res, _ = run(db, tester("hello"), Scenario(name="s", prompt="p", max_turns=1), voice=voices())
    assert (res["status"], res["recording"]) == ("done", None)


@voiced
def test_stt_must_stream():
    """A server without a realtime websocket (here the TTS one) is refused as a tester STT."""
    with pytest.raises(ConnectionError, match="stream"):
        asyncio.run(stt.check(TTS_URL, "m", None))


@selfhost
def test_agent_never_greets(db, stub, tester):
    res, _ = run(db, tester(), Scenario(name="s", prompt="p"), profile=SILENT)
    assert (res["status"], res["ended"], res["passed"]) == ("done", "no_greeting", 0)
    assert not tester.asked  # the tester never spoke


@selfhost
def test_exact_opening(db, stub, tester):
    res, evs = run(db, tester("thanks"), Scenario(name="s", prompt="p", max_turns=2), opening="please use the tool",
                   profile=SILENT)
    assert [d["text"] for _, _, t, d in evs if t == "tester_turn"] == ["please use the tool", "thanks"]
    assert len(tester.asked) == 1  # the opening needed no LLM


@voiced
def test_voice_exact_opening(db, stub, tester):
    res, evs = run(db, tester(), Scenario(name="s", prompt="p", max_turns=1), voice=voices(),
                   opening="please use the tool", profile=SILENT)
    assert (res["status"], res["ended"]) == ("done", "max_turns"), res
    assert [d["text"] for _, _, t, d in evs if t == "tester_turn"] == ["please use the tool"]
    assert len([1 for _, _, t, _ in evs if t == "tool_execution_updated"]) == 2  # said, heard, called
    assert not tester.asked
