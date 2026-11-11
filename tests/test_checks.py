"""Checks on a real stub run (`stub_events.json`: 3 turns, one 1.5 s `lookup` call), saved by scratchpad/dump_stub.py,
plus edited copies for each failure."""

import copy
import json
from pathlib import Path

from livetest.runtime import checks, store
from livetest.runtime.models import Scenario

EVENTS = [tuple(e) for e in json.loads(Path(__file__).with_name("stub_events.json").read_text())]


def run(*expect, ended="max_turns", events=EVENTS):
    return {o["check"]: o for o in checks.evaluate(Scenario(name="s", goal="g", expect=list(expect)), ended, events)}


def replies(events):
    """The agent's messages in `events`, to edit in place."""
    return [d["conversation_item_added"]["item"]["message"] for _, _, t, d in events
            if t == "conversation_item_added" and d["conversation_item_added"]["item"].get("message", {}).get("role") == "ASSISTANT"]


def test_real_run_passes():
    out = run({"type": "tool_called", "name": "lookup"}, {"type": "tool_not_called", "name": "cancel"},
              {"type": "no_tool_errors"}, {"type": "latency", "p95_ms": 2500})
    assert set(out) == {"silent", "loops", "finished_in_time", "tool_called", "tool_not_called", "no_tool_errors", "latency"}
    assert all(o["passed"] for o in out.values()), out
    # Expected and actual side by side, on passes too.
    assert out["tool_called"]["actual"] == [{"name": "lookup", "args": {}, "output": "found it", "error": False}]
    # The tool turn's gap includes the 1.5 s tool, so p95 (nearest rank, 3 turns) is that turn.
    assert 2000 < out["latency"]["actual"]["p95_ms"] < 2500


def test_tool_checks_fail():
    wrong = run({"type": "tool_called", "name": "lookup", "args": {"q": "x"}})["tool_called"]
    assert not wrong["passed"] and wrong["actual"][0]["args"] == {}  # right tool, wrong args: both shown
    assert not run({"type": "tool_called", "name": "missing"})["tool_called"]["passed"]
    assert not run({"type": "tool_not_called", "name": "lookup"})["tool_not_called"]["passed"]


def test_tool_error():
    events = copy.deepcopy(EVENTS)
    for _, _, t, d in events:
        if t == "function_tools_executed":
            d[t]["function_call_outputs"][0]["is_error"] = True
    assert not run({"type": "no_tool_errors"}, events=events)["no_tool_errors"]["passed"]


def test_latency_over_limit():
    out = run({"type": "latency", "p95_ms": 1000, "p99_ms": 3000})["latency"]
    assert not out["passed"] and out["expected"] == {"p95_ms": 1000, "p99_ms": 3000}


def test_silent():
    """A turn the agent doesn't start replying to within 10 s fails, whatever it said later."""
    last = max(n for n, (_, _, t, d) in enumerate(EVENTS) if t == "agent_state_changed" and d[t]["new_state"] == "AS_SPEAKING")
    never = EVENTS[:last] + EVENTS[last + 1:]
    late = [(ts + checks.REPLY_S + 1, *e) if n == last else (ts, *e) for n, (ts, *e) in enumerate(EVENTS)]
    for events in (never, late):
        out = run(events=events)["silent"]
        assert not out["passed"] and out["actual"] == ["thanks"]


def test_loops():
    events = copy.deepcopy(EVENTS)
    replies(events)[1]["content"][0]["text"] = "Hello, this is a canned reply!"  # all three replies now alike
    assert not run(events=events)["loops"]["passed"]


def test_max_minutes_fails():
    assert not run(ended="max_minutes")["finished_in_time"]["passed"]


def test_verdict_stored(tmp_path):
    db = store.connect(tmp_path / "t.db")
    s = Scenario(name="s", goal="g")
    a, b, c = store.start_run(db, "r", "agent", None, [(s, "r")] * 3)
    store.finish_result(db, a, "done", "max_turns", [{"check": "x", "passed": True}])
    store.finish_result(db, b, "done", "max_minutes", [{"check": "x", "passed": True}, {"check": "y", "passed": False}])
    store.finish_result(db, c, "never_joined", "no agent")
    got = [(store.result(db, n)["passed"], store.result(db, n)["checks"]) for n in (a, b, c)]
    assert got == [(1, '[{"check": "x", "passed": true}]'), (0, got[1][1]), (None, None)]
    db.close()
