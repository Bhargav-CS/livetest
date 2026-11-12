"""`report create`: the run page (static/run.html) with a run's rows baked in, so it opens offline as one file.

The page renders from raw rows (the run, its results, their events) the same way the UI's will from its JSON API.
"""

import base64
import json
from datetime import datetime
from pathlib import Path

from livetest.runtime import store

PAGE = Path(__file__).with_name("static") / "run.html"
SLOT = '<script id="data" type="application/json">null</script>'


def create(db, run: dict, test: int | None = None) -> str:
    """The page for `run` (a `store.runs` row), or for its test `test`, recordings embedded. ValueError: no such test."""
    results = [r for r in store.results(db, run["id"]) if test in (None, r["test"])]
    if not results:
        raise ValueError(f"run {run['id']} has no test {test}; see: livetest run list --run {run['id']}")
    for r in results:
        r["events"] = store.events(db, r["id"])
        rec = Path(r["recording"]) if r["recording"] else None
        # None with `recording` set: the file is gone, and the page says so.
        r["audio"] = "data:audio/ogg;base64," + base64.b64encode(rec.read_bytes()).decode() if rec and rec.is_file() else None
    data = {"run": run, "results": results, "test": test, "made": datetime.now().astimezone().isoformat()}
    # Every < escaped: a prompt or transcript holding </script> can't end the tag.
    return PAGE.read_text(encoding="utf-8").replace(SLOT, SLOT.replace("null", json.dumps(data).replace("<", "\\u003c")))
