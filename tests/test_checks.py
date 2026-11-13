"""Checks on a real stub run (`stub_events.json`: 3 turns, one 1.5 s `lookup` call), saved by scratchpad/dump_stub.py,
plus edited copies for each failure."""

import copy
import json
from pathlib import Path

from livetest.runtime import checks, store
from livetest.runtime.models import Scenario

EVENTS = [tuple(e) for e in json.loads(Path(__file__).with_name("stub_events.json").read_text())]


def run(*expect, events=EVENTS):
    return {o["check"]: o for o in checks.evaluate(Scenario(name="s", prompt="p", expect=list(expect)), events)}


def test_real_run_passes():
    out = run({"type": "tool_called", "name": "lookup"}, {"type": "tool_not_called", "name": "cancel"},
              {"type": "no_tool_errors"}, {"type": "latency", "p95_ms": 2500})
    assert set(out) == {"tool_called", "tool_not_called", "no_tool_errors", "latency"}  # the scenario's own, nothing else
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


def test_no_checks_of_livetests_own():
    """A scenario with no checks of its own has none (a no_reply / no_greeting ending fails the test in
    store.finish_result instead). Loop and time-limit checks come with the LLM judge (v0.2)."""
    assert checks.evaluate(Scenario(name="s", prompt="p"), EVENTS) == []
    assert checks.evaluate(Scenario(name="s", prompt="p"), []) == []  # e.g. the agent never greeted


def test_replies_pair_on_the_testers_clock():
    """Each reply goes with the turn that triggered it. Voice: the tester stopped speaking → it heard the agent start;
    the agent's own speaking state (e.g. resuming after a false interruption) doesn't count. A turn the agent cut off
    isn't graded; one never answered fails the latency check."""
    def turn(ts, a, b, cut=False):
        return (ts, "tester", "tester_turn", {"text": "t", "interrupted": cut, "metrics": {"started_speaking_at": a,
                                                                                         "stopped_speaking_at": b}})
    speaking = lambda ts: (ts, "agent", "agent_state_changed", {"agent_state_changed": {"new_state": "AS_SPEAKING"}})
    # The tester's VAD hearing the agent start; its ts is the SDK's created_at (the tester_heard item comes later)
    heard = lambda _, at: (at, "tester", "tester_user_state_changed", {"old_state": "listening", "new_state": "speaking"})
    voice = [turn(5.1, 2, 5), speaking(5.5), heard(9, 6.2), speaking(9.5),  # 9.5: a resume, not a reply
             turn(10.1, 9.8, 10, cut=True),
             heard(21, 19.5), turn(20.1, 18, 20),  # 19.5: its previous reply finishing over turn 3, not an answer
             (31, "tester", "tester_closed", {})]  # 11 s of nothing heard after turn 3
    assert checks.replies(voice) == [{"turn": 1, "at": 5, "gap": 1.2000000000000002}, {"turn": 3, "at": 20, "gap": None}]
    out = run({"type": "latency", "p95_ms": 5000}, events=voice)["latency"]
    assert not out["passed"] and out["actual"] == {"p95_ms": None}
    # The tester says bye and the agent hangs up within REPLY_S: that last turn isn't graded.
    assert [r["turn"] for r in checks.replies(voice[:-1])] == [1]
    # max_minutes mid-reply: the agent started 3 s after the turn, no tester_heard item yet; still answered.
    cut = [turn(5.1, 2, 5), heard(None, 8), (9, "tester", "tester_closed", {})]
    assert checks.replies(cut) == [{"turn": 1, "at": 5, "gap": 3}]
    # max_minutes at 16 s closes the tester; the agent speaks at 17 s, unheard, during livetest's usage wait: not graded.
    late = [turn(10.1, 8, 10), (16, "tester", "tester_closed", {}), speaking(17), (21, "tester", "tester_usage", {"usage": []})]
    assert checks.replies(late) == []
    # Text: the agent only spoke after the tester's next line, so the first line went unanswered.
    text = [(1, "tester", "tester_turn", {"text": "a"}), (12, "tester", "tester_turn", {"text": "b"}), speaking(13)]
    assert [r["gap"] for r in checks.replies(text)] == [None, 1]


def test_usage():
    """Counts only. The agent's usage is cumulative, so its last event wins; the tester's comes once, at the end."""
    def used(*models):
        return {"session_usage_updated": {"usage": {"model_usage": list(models)}}}

    llm = {"provider": "api.groq.com", "model": "m", "input_tokens": 900, "input_cached_tokens": 512, "output_tokens": 40}
    stt = {"provider": "Deepgram", "model": "nova-3", "audio_duration": 2.5}
    tester = [{"kind": "llm", "provider": "x", "model": "m", "input_tokens": 50, "output_tokens": 4},
              {"kind": "tts", "provider": "z", "model": "kokoro-7m", "characters_count": 30, "audio_duration": 2.1}]
    events = [
        (1, "agent", "session_usage_updated", used({"stt": {"provider": "Deepgram", "model": "nova-3"}})),
        (2, "tester", "tester_turn", {"text": "hi"}),
        (3, "agent", "session_usage_updated", used({"llm": llm}, {"stt": stt})),
        (4, "tester", "tester_usage", {"usage": tester}),
    ]
    assert checks.usage(events) == {"agent": [{"kind": "llm", **llm}, {"kind": "stt", **stt}], "tester": tester}
    # The stub runs no model and the dump's tester reported none: nothing used, nothing made up.
    assert checks.usage(EVENTS) == {"agent": [], "tester": []}


def test_verdict_stored(tmp_path):
    db = store.connect(tmp_path / "t.db")
    s = Scenario(name="s", prompt="p")
    a, b, c, d, e = store.start_run(db, "r", "agent", None, [(s, "r")] * 5)
    store.finish_result(db, a, "done", "max_turns", [{"check": "x", "passed": True}])
    store.finish_result(db, b, "done", "max_minutes", [{"check": "x", "passed": True}, {"check": "y", "passed": False}])
    store.finish_result(db, c, "never_joined", "no agent", None, {"agent": [], "tester": []})
    store.finish_result(db, d, "done", "no_reply", [])  # the agent stopped answering: failed, checks or not
    store.finish_result(db, e, "done", "no_greeting", [])
    got = [(store.result(db, n)["passed"], store.result(db, n)["checks"]) for n in (a, b, c, d, e)]
    assert got == [(1, '[{"check": "x", "passed": true}]'), (0, got[1][1]), (None, None), (0, "[]"), (0, "[]")]
    assert [store.result(db, n)["usage"] for n in (a, c)] == [None, '{"agent": [], "tester": []}']  # unchecked tests too
    db.close()
