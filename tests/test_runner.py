"""One text-mode test, end to end. The tester LLM is a local fake OpenAI server saying scripted lines (no credits).

Live part: a self-hosted LiveKit server (`podman kube play --replace dev/livekit-server.yaml`) and the stub agent,
which these tests start themselves. Skipped unless set:

    $env:LIVETEST_SELFHOST="ws://<podman machine ip>:7880"; uv run pytest tests/test_runner.py
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from livetest.platform import llm
from livetest.runtime import runner, store, tokens
from livetest.runtime.models import Agent, Scenario

URL, KEY, SECRET = os.getenv("LIVETEST_SELFHOST"), "devkey", "secret"  # livekit-server --dev
selfhost = pytest.mark.skipif(not URL, reason="set LIVETEST_SELFHOST=ws://<podman machine ip>:7880")


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
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def make(*say):
        lines.extend(say)
        return llm.llm(f"http://127.0.0.1:{srv.server_port}/v1", "fake", None)

    make.asked = asked
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


def run(db, tester_llm, scenario, agent="livetest-stub"):
    url, token = tokens.connect(Agent(name=agent, url=URL), f"livetest-{uuid.uuid4().hex[:8]}", key=KEY, secret=SECRET)

    async def go():
        try:
            return await runner.run_text(db, scenario, url, token, tester_llm, key=KEY, secret=SECRET)
        finally:
            await tester_llm.aclose()

    n = asyncio.run(go())
    return store.result(db, n), store.events(db, n)


def test_logs_land_on_their_own_test(db):
    s = Scenario(name="s", goal="g")
    a, b = store.start_result(db, s, None, "ra"), store.start_result(db, s, None, "rb")
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
        assert [(src, t, d["message"]) for src, t, d in store.events(db, n)] == [("log", "INFO", f"from {n}")]


@selfhost
def test_max_turns(db, stub, tester, caplog):
    caplog.set_level(logging.INFO)  # what the CLI's logging setup does
    res, evs = run(db, tester("hello", "please use the tool", "thanks"), Scenario(name="s", goal="g", max_turns=3))
    assert (res["status"], res["ended"], res["room"].startswith("livetest-")) == ("done", "max_turns", True)
    of = lambda kind: [d for _, t, d in evs if t == kind]
    assert [d["text"] for d in of("tester_turn")] == ["hello", "please use the tool", "thanks"]
    info = of("agent_info")[0]
    assert "Stub agent" in info["instructions"] and "lookup" in json.dumps(info["tools"])
    assert len(of("tool_execution_updated")) == 2  # started, ended: the tool's real duration
    # No session_usage_updated: the stub runs no model, so it has no usage (Baymax's probes show it).
    assert {"framework", "session_state", "chat_history", "seen", "conversation_item_added"} <= {t for _, t, _ in evs}
    assert any(src == "log" for src, _, _ in evs)
    # The agent's replies reach the tester as the other side of its conversation.
    assert {"role": "user", "content": "Done, I found it."} in tester.asked[-1]["messages"]


@selfhost
def test_agent_leaves(db, stub, tester):
    res, _ = run(db, tester("hi", "bye", "still there?"), Scenario(name="s", goal="g", max_turns=5))
    assert (res["status"], res["ended"]) == ("done", "agent_left")


@selfhost
def test_max_minutes(db, stub, tester):
    res, _ = run(db, tester(*["please use the tool"] * 20), Scenario(name="s", goal="g", max_turns=20, max_minutes=0.05))
    assert (res["status"], res["ended"]) == ("done", "max_minutes")


@selfhost
def test_never_joined(db, tester):  # waits the full 30 s
    res, evs = run(db, tester(), Scenario(name="s", goal="g"), agent="nobody-livetest")
    assert res["status"] == "never_joined" and "never joined" in res["ended"]
