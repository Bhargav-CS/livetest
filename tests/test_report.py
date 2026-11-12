"""`report create`: one offline HTML file, a run's rows baked in. What the page draws from them is checked by opening it."""

import base64
import json
import re
from contextlib import closing
from pathlib import Path

import pytest

from livetest.cli import main
from livetest.runtime import store
from livetest.runtime.models import Scenario

EVENTS = json.loads(Path(__file__).with_name("stub_events.json").read_text())


@pytest.fixture
def run(tmp_path, monkeypatch):
    """`livetest --db ...` over run 'r': two tests of the stub run, the first recorded. Reports land in tmp_path."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "livetest.db"
    with closing(store.connect(path)) as db:
        ids = store.start_run(db, "r", "bot", None, [(Scenario(name="s", prompt="p"), "x")] * 2, "voice")
        for n in ids:
            for ts, source, kind, data in EVENTS:
                store.event(db, n, source, kind, data, ts)
            store.finish_result(db, n, "done", "max_turns", [], {"agent": [], "tester": []})
        (tmp_path / "test-1.ogg").write_bytes(b"OggS fake")
        store.recorded(db, ids[0], str(tmp_path / "test-1.ogg"), EVENTS[0][0])
    return lambda *a: main(["--db", str(path), *a])


def data(file: Path) -> dict:
    """The JSON the page renders from."""
    page = file.read_text(encoding="utf-8")
    return json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S)[1])


def test_whole_run(run, tmp_path):
    assert run("report", "create", "--run", "r") == 0
    got = data(tmp_path / "run-r_report.html")
    assert got["run"]["id"] == "r" and [t["test"] for t in got["results"]] == [1, 2]
    assert [e[2] for e in got["results"][0]["events"]] == [e[2] for e in EVENTS]
    assert got["results"][0]["audio"] == "data:audio/ogg;base64," + base64.b64encode(b"OggS fake").decode()
    assert got["results"][1]["audio"] is None  # never recorded
    (tmp_path / "test-1.ogg").unlink()  # the page says "recording missing": the path is set, the audio isn't
    assert run("report", "create", "--run", "latest") == 0
    again = data(tmp_path / "run-r_report.html")["results"][0]
    assert again["audio"] is None and again["recording"]


def test_one_test(run, tmp_path):
    assert run("report", "create", "--run", "r", "--test", "2") == 0
    assert [t["test"] for t in data(tmp_path / "run-r_test-2_report.html")["results"]] == [2]
    assert run("report", "create", "--run", "r", "--output", "out/nightly.html") == 0
    assert len(data(tmp_path / "out" / "nightly.html")["results"]) == 2


def test_text_cant_break_out_of_the_page(run, tmp_path):
    evil = "</script><script>alert(1)</script>"
    with closing(store.connect(tmp_path / "livetest.db")) as db:
        store.event(db, 1, "tester", "tester_turn", {"text": evil})
    assert run("report", "create", "--run", "r", "--test", "1") == 0
    file = tmp_path / "run-r_test-1_report.html"
    assert "<script>alert(1)" not in file.read_text(encoding="utf-8")
    assert evil in [e[3].get("text") for e in data(file)["results"][0]["events"]]  # kept exactly, only escaped


@pytest.mark.parametrize("argv", [["--run", "nope"], ["--run", "r", "--test", "9"]])
def test_refuses(run, argv, capsys):
    assert run("report", "create", *argv) == 2
    assert capsys.readouterr().err.startswith("error:")
