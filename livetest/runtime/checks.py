"""What a finished test is judged on: the scenario's own `expect` only (tool checks, latency); and what it used.

Pure: reads a result's events, no LiveKit. Each outcome shows what was expected and what happened, passes too.
"""

import json
import math

from livetest.runtime.models import Latency, NoToolErrors, Scenario, ToolCheck

# The runner ends a test when the agent doesn't start replying (or greeting) within this many seconds of a tester turn;
# provider errors (4xx / 5xx) it retries past inside that don't count. The test ends no_reply / no_greeting and fails
# (store.finish_result).
REPLY_S = 10


def _args(text: str | None):
    try:
        return json.loads(text or "{}")
    except ValueError:  # the agent's LLM wrote bad JSON; show it as it came
        return text


def usage(events) -> dict:
    """Counts, no cost: the agent's models from its last session usage (it's cumulative), each as {kind, provider,
    model, ...counts as reported}; the tester's from its `tester_usage` event, written once as the test ends. Nothing
    reported, nothing listed."""
    agent, tester = [], []
    for _, _, kind, data in events:
        if kind == "session_usage_updated":
            agent = [{"kind": k, **v} for m in data[kind].get("usage", {}).get("model_usage", []) for k, v in m.items()]
        elif kind == "tester_usage":
            tester = data["usage"]
    return {"agent": agent, "tester": tester}


def _started_reply(kind: str, data: dict) -> bool:
    ev = data.get(kind) or {}
    return (kind == "agent_state_changed" and ev.get("new_state") == "AS_SPEAKING"
            or kind == "conversation_item_added" and ev.get("item", {}).get("message", {}).get("role") == "ASSISTANT")


def replies(events) -> list[dict]:
    """Each tester turn the agent was to answer, {turn, at, gap} in s on the tester's own clock; gap None when no reply
    started before the tester's next turn or the end. `turn` counts every tester_turn from 1.
    Voice: the tester stopped speaking → it heard the agent start after that (its VAD, as the runner waits on it; a
    tester_heard item only comes once the agent finishes, so a reply cut off by the test's end has none). Text: the line sent → the
    agent's speaking state (or, with no TTS, its words) arriving. Not graded: a turn the agent cut off (interrupted;
    it was talking already and may not have heard it), and one with no reply where REPLY_S hadn't passed before the
    next turn or the end (e.g. the tester's "bye" and the agent hanging up; a no_reply ending always waited REPLY_S)."""
    said = [(i, ts, d) for i, (ts, _, kind, d) in enumerate(events) if kind == "tester_turn"]
    heard = [ts for ts, _, kind, d in events if kind == "tester_user_state_changed" and d.get("new_state") == "speaking"]
    end = max((e[0] for e in events), default=0)
    # Voice: the tester hears nothing after its session closes (tester_closed); livetest's usage wait comes after.
    closed = max((ts for ts, source, kind, _ in events if source == "tester" and kind != "tester_usage"), default=end)
    out = []
    for n, (i, ts, d) in enumerate(said, 1):
        if d.get("interrupted"):
            continue
        j, until, nxt = said[n] if n < len(said) else (len(events), end, {})
        m = d.get("metrics", {})
        if "stopped_speaking_at" in m:  # voice
            until = nxt.get("metrics", {}).get("started_speaking_at", until) if nxt else closed
            start = m["stopped_speaking_at"]
            at = min((h for h in heard if start < h < until), default=None)  # earlier: its previous reply finishing
        else:  # text; the next tester turn comes only after the agent's whole reply
            start, at = ts, next((t for t, _, kind, e in events[i + 1:j] if _started_reply(kind, e)), None)
        if at is not None or until - start >= REPLY_S:
            out.append({"turn": n, "at": start, "gap": None if at is None else at - start})
    return out


def rank(gaps: list[float | None], q: int) -> int | None:
    """The q-th percentile in ms, nearest rank (with under 20 turns, p95 is the slowest). An unanswered turn (None)
    ranks slowest; None if it's the one picked, or no turns."""
    ranked = sorted(math.inf if g is None else g for g in gaps)
    v = ranked[math.ceil(q / 100 * len(ranked)) - 1] if ranked else math.inf
    return None if v == math.inf else round(v * 1000)


def evaluate(scenario: Scenario, events) -> list[dict]:
    """The scenario's own checks, nothing of livetest's. `events` as `store.events` gives them: (ts, source, type,
    data), in order. Run only on `done` results."""
    calls, outputs, out = [], {}, []
    for _, _, kind, data in events:
        if kind == "function_tools_executed":
            calls += data[kind].get("function_calls", [])
            outputs |= {o["call_id"]: o for o in data[kind].get("function_call_outputs", [])}
    calls = [{"name": c["name"], "args": _args(c.get("arguments")), "output": outputs.get(c["call_id"], {}).get("output"),
              "error": outputs.get(c["call_id"], {}).get("is_error", False)} for c in calls]
    gaps = [r["gap"] for r in replies(events)]
    for c in scenario.expect:
        if isinstance(c, ToolCheck):
            hits = [x for x in calls if x["name"] == c.name]
            match = any(isinstance(x["args"], dict) and c.args.items() <= x["args"].items() for x in hits)
            out.append({"check": c.type, "passed": match == (c.type == "tool_called"),
                        "expected": {"name": c.name, "args": c.args}, "actual": hits})
        elif isinstance(c, NoToolErrors):
            errors = [x for x in calls if x["error"]]
            out.append({"check": c.type, "passed": not errors, "expected": "no tool errors", "actual": errors})
        elif isinstance(c, Latency):
            want = c.model_dump(exclude={"type"}, exclude_none=True)  # {"p95_ms": 1500, ...}
            got = {k: rank(gaps, int(k[1:3])) for k in want}  # None: that turn got no reply
            out.append({"check": c.type, "passed": all(got[k] is not None and got[k] <= v for k, v in want.items()),
                        "expected": want, "actual": got})
    return out
