"""`report create`: the run page (static/run.html) with a run's rows baked in, so it opens offline as one file.

The page renders from raw rows (the run, its results, their events); the UI's API serves the same rows (`data`).
"""

import base64
import json
from datetime import datetime
from pathlib import Path

from livetest.runtime import checks, store

PAGE = Path(__file__).with_name("static") / "run.html"
FONTS = PAGE.parent / "fonts"
SLOT = '<script id="data" type="application/json">null</script>'
# What the Report screen lists, by event type; every other event draws the timelines.
CONVO = {"tester_turn", "tester_heard", "conversation_item_added"}
INFO = {"framework", "agent_info", "session_state", "seen"}


def embed(result: int, file: Path) -> str:
    return "data:audio/ogg;base64," + base64.b64encode(file.read_bytes()).decode()


def data(db, run: dict, test: int | None = None, audio=embed, recordings: bool = True) -> dict:
    """The rows the page renders for `run` (a `store.runs` row), or its test `test`. Each result's `audio` is
    `audio(result id, file)` when its recording exists and `recordings` is on, else None. ValueError: no such test."""
    results = [r for r in store.results(db, run["id"]) if test in (None, r["test"])]
    if not results:
        raise ValueError(f"run {run['id']} has no test {test}; see: livetest run list --run {run['id']}")
    for r in results:
        r["events"] = store.events(db, r["id"])
        r["replies"] = checks.replies(r["events"])  # the reply gaps, paired as the latency check pairs them
        rec = Path(r["recording"]) if r["recording"] else None
        # None with `recording` set: the file is gone (or left out), and the page says which.
        r["audio"] = audio(r["id"], rec) if recordings and rec and rec.is_file() else None
    return {"run": run, "results": results, "test": test, "recordings": recordings,
            "reply_p95_ms": checks.rank([g["gap"] for r in results for g in r["replies"]], 95),
            "made": datetime.now().astimezone().isoformat()}


def filename(run: str, test: int | None = None) -> str:
    return f"run-{run}{f'_test-{test}' if test else ''}_report.html"


def page(rows: dict) -> str:
    """run.html with `rows` baked in and its fonts inside: one file, no server, no internet."""
    html = PAGE.read_text(encoding="utf-8")
    for font in FONTS.glob("*.woff2"):
        html = html.replace(f"url(/fonts/{font.name})", "url(data:font/woff2;base64," + base64.b64encode(font.read_bytes()).decode() + ")")
    # Every < escaped: a prompt or transcript holding </script> can't end the tag.
    return html.replace(SLOT, SLOT.replace("null", json.dumps(rows).replace("<", "\\u003c")))


def create(db, run: dict, test: int | None = None, recordings: bool = True) -> str:
    """The page for `run`, or its test `test`. ValueError: no such test."""
    return page(data(db, run, test, recordings=recordings))


def sizes(db, run: dict, test: int | None = None, recordings: bool = True) -> dict:
    """What the Report screen shows: the file's name and bytes, and each section's bytes and count. Page (the viewer
    and its fonts) is the rest, so the sections add up to the file exactly."""
    rows = data(db, run, test, recordings=recordings)
    size = lambda v: len(json.dumps(v).encode())
    events = [e for r in rows["results"] for e in r["events"]]
    convo, info = [e for e in events if e[2] in CONVO], [e for e in events if e[2] in INFO]
    logs = [e for e in events if e[1] == "log"]
    timelines = [e for e in events if e[2] not in CONVO | INFO and e[1] != "log"]
    audio = [r["audio"] for r in rows["results"] if r["audio"]]
    summary = [{k: v for k, v in r.items() if k not in ("events", "audio")} for r in rows["results"]]
    sections = [("Summary", size([rows["run"], summary]), len(summary)), ("Conversations", size(convo), len(convo)),
                ("Timelines", size(timelines), len(timelines)), ("Logs", size(logs), len(logs)),
                ("Agent info", size(info), len(info)), ("Recordings", sum(map(len, audio)), len(audio))]
    total = len(page(rows).encode())
    sections.append(("Page", total - sum(b for _, b, _ in sections), 1))
    return {"file": filename(run["id"], test), "bytes": total,
            "sections": [{"name": n, "bytes": b, "count": c} for n, b, c in sections]}
