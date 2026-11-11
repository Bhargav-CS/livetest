"""What a finished test is judged on: built-ins (silent, loops, finished in time) plus the scenario's `expect`.

Pure: reads a result's events, no LiveKit. Each outcome shows what was expected and what happened, passes too.
"""

import json
import math
from difflib import SequenceMatcher

from livetest.runtime.models import Latency, NoToolErrors, Scenario, ToolCheck

# The agent must start replying within this many seconds of a tester turn; provider errors (4xx / 5xx) it retries
# past inside that don't count. The runner ends the test on a turn that misses it.
REPLY_S = 10


def _args(text: str | None):
    try:
        return json.loads(text or "{}")
    except ValueError:  # the agent's LLM wrote bad JSON; show it as it came
        return text


def evaluate(scenario: Scenario, ended: str, events) -> list[dict]:
    """`events` as `store.events` gives them: (ts, source, type, data), in order. Run only on `done` results."""
    turns, replies, calls, outputs = [], [], [], {}
    for ts, _, kind, data in events:
        ev = data.get(kind) or {}
        if kind == "tester_turn":
            turns.append({"said": data["text"], "at": ts, "gap": None})
        elif kind == "agent_state_changed" and ev.get("new_state") == "AS_SPEAKING" and turns and turns[-1]["gap"] is None:
            # ponytail: tester_turn ts is when the text is sent; voice mode (module 9) needs when the tester stops speaking
            turns[-1]["gap"] = ts - turns[-1]["at"]
        elif kind == "conversation_item_added" and (m := ev["item"].get("message", {})).get("role") == "ASSISTANT":
            replies.append(" ".join(c.get("text", "") for c in m.get("content", [])))
        elif kind == "function_tools_executed":
            calls += ev.get("function_calls", [])
            outputs |= {o["call_id"]: o for o in ev.get("function_call_outputs", [])}
    calls = [{"name": c["name"], "args": _args(c.get("arguments")), "output": outputs.get(c["call_id"], {}).get("output"),
              "error": outputs.get(c["call_id"], {}).get("is_error", False)} for c in calls]

    unanswered = [t["said"] for t in turns if t["gap"] is None or t["gap"] > REPLY_S]
    # ponytail: 90% alike back to back catches word-for-word loops; v0.2 likely wants a semantic or windowed rule
    loops = [[a, b] for a, b in zip(replies, replies[1:]) if a and SequenceMatcher(None, a, b).ratio() >= 0.9]
    out = [
        {"check": "silent", "passed": not unanswered, "expected": f"a reply to every tester turn within {REPLY_S} s",
         "actual": unanswered},
        {"check": "loops", "passed": not loops, "expected": "no reply repeating the one before", "actual": loops},
        {"check": "finished_in_time", "passed": ended != "max_minutes",
         "expected": f"ends within {scenario.max_minutes} min", "actual": ended},
    ]
    gaps = sorted(t["gap"] for t in turns if t["gap"] is not None)
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
            # Nearest rank: with under 20 turns, p95 is the slowest turn.
            got = {k: round(gaps[math.ceil(int(k[1:3]) / 100 * len(gaps)) - 1] * 1000) if gaps else None for k in want}
            out.append({"check": c.type, "passed": bool(gaps) and all(got[k] <= v for k, v in want.items()),
                        "expected": want, "actual": got})
    return out
