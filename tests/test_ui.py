"""`livetest ui`: the server's API over the same rows `report create` bakes in, recordings with Range, Stop, and the
localhost-only guards. What the pages draw is checked in a browser."""

import asyncio
import json
from contextlib import closing
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from livetest.cli import main
from livetest.platform import keys, llm
from livetest.runtime import runner, session, store, tokens
from livetest.runtime.models import Agent, Plugin, Scenario
from livetest.ui import server

EVENTS = json.loads(Path(__file__).with_name("stub_events.json").read_text())


@pytest.fixture
def path(tmp_path):
    """A DB with run 'r' (two finished tests of the stub run, the first recorded) and run 'q' (two queued tests)."""
    path = tmp_path / "livetest.db"
    with closing(store.connect(path)) as db:
        ids = store.start_run(db, "r", "bot", None, [(Scenario(name="s", prompt="p"), "x")] * 2, "voice")
        for n in ids:
            for ts, source, kind, data in EVENTS:
                store.event(db, n, source, kind, data, ts)
            store.finish_result(db, n, "done", "max_turns", [], {"agent": [], "tester": []})
        (tmp_path / "test-1.ogg").write_bytes(b"OggS fake")
        store.recorded(db, ids[0], str(tmp_path / "test-1.ogg"), EVENTS[0][0])
        store.start_run(db, "q", "bot", None, [(Scenario(name="s", prompt="p"), "x")] * 2)
    return path


def call(path: Path, go):
    """Run `go(client)` against the app over `path`, on a real local socket."""
    async def run():
        async with TestClient(TestServer(server.app(path))) as client:
            return await go(client)
    return asyncio.run(run())


def test_api_has_the_reports_rows(path):
    async def go(c):
        return ([r["id"] for r in await (await c.get("/api/runs")).json()], await (await c.get("/api/runs/r")).json(),
                await (await c.get("/api/runs/latest?test=2")).json(),
                (await c.get("/api/runs/nope")).status, (await c.get("/api/runs/r?test=9")).status)
    runs, whole, one, nope, no_test = call(path, go)
    assert runs == ["r", "q"]
    assert whole["run"]["id"] == "r" and [t["test"] for t in whole["results"]] == [1, 2]
    assert [e[2] for e in whole["results"][0]["events"]] == [e[2] for e in EVENTS]
    first, second = whole["results"]
    assert first["audio"] == f"/api/results/{first['id']}/recording"  # a URL here, base64 in the report file
    assert second["audio"] is None  # never recorded
    assert one["run"]["id"] == "q" and [t["test"] for t in one["results"]] == [2]
    assert nope == no_test == 404


def test_recording_seeks(path, tmp_path):
    async def go(c):
        first, second = (await (await c.get("/api/runs/r")).json())["results"]
        part = await c.get(first["audio"], headers={"Range": "bytes=0-3"})
        got = part.status, await part.read()
        (tmp_path / "test-1.ogg").unlink()
        return got, (await c.get(first["audio"])).status, (await c.get(f"/api/results/{second['id']}/recording")).status
    (status, body), gone, never = call(path, go)
    assert (status, body) == (206, b"OggS")  # Range is what lets the browser seek
    assert gone == never == 404


def test_stop(path):
    async def go(c):
        one = await (await c.post("/api/runs/q/stop", json={"test": 2})).json()
        rest = await (await c.post("/api/runs/q/stop", json={})).json()
        again = await (await c.post("/api/runs/q/stop", json={})).json()
        return one, rest, again, (await c.post("/api/runs/nope/stop", json={})).status
    one, rest, again, nope = call(path, go)
    assert (one, rest, again, nope) == ({"stopped": 1}, {"stopped": 1}, {"stopped": 0}, 404)
    with closing(store.connect(path)) as db:
        assert [r["status"] for r in store.results(db, "q")] == ["stopped", "stopped"]


def test_only_this_machines_pages(path):
    async def go(c):
        return ((await c.get("/api/runs", headers={"Host": "evil.example:8787"})).status,  # DNS rebinding
                (await c.get("/api/runs", headers={"Host": "localhost:8787"})).status,
                (await c.post("/api/runs/q/stop", data="test=1")).status)  # a form any website could post
    assert call(path, go) == (403, 200, 415)
    with closing(store.connect(path)) as db:
        assert [r["status"] for r in store.results(db, "q")] == ["queued", "queued"]


def test_pages(path):
    async def go(c):
        home, run, test = await c.get("/"), await c.get("/runs/r"), await c.get("/runs/r/tests/2")
        return home.status, await home.text(), run.status, await run.text(), test.status, await test.text()
    home, shell, run, page, test, test_page = call(path, go)
    assert home == run == test == 200 and "LiveTest" in shell
    assert '<script id="data" type="application/json">null</script>' in page  # run.html unbaked: it fetches the API
    assert test_page == page  # a test (the C page) is the same file; it reads the test from its path


def test_fonts_and_settings(path):
    async def go(c):
        font = await c.get("/fonts/inter.woff2")
        return font.status, (await font.read())[:4], await (await c.get("/api/settings")).json()
    status, magic, settings = call(path, go)
    assert (status, magic) == (200, b"wOF2")  # shipped with livetest: no request to Google
    assert settings == {"db": str(path), "env": str(path.parent / ".env"), "recordings": str(path.parent / "recordings")}


def test_last_scores(path):
    """Each scenario's passed / graded in the latest run that graded it: run q (later, nothing graded yet) doesn't count."""
    with closing(store.connect(path)) as db:
        store.add(db, Scenario(name="s", prompt="p"))
        store.add(db, Scenario(name="never", prompt="p"))
        store.finish_result(db, store.results(db, "r")[1]["id"], "done", "max_turns", [{"check": "c", "passed": False}])

    async def go(c):
        return {s["name"]: s["last"] for s in await (await c.get("/api/scenarios")).json()}
    assert call(path, go) == {"s": {"run": "r", "passed": 1, "graded": 2}, "never": None}


def test_cli(path, capsys):
    assert main(["--db", str(path), "ui"]) == 0 and "actions" in capsys.readouterr().out
    assert main(["--db", str(path), "ui", "stop"]) == 0 and "not running" in capsys.readouterr().out


@pytest.fixture
def offline(monkeypatch):
    """Every plugin answers and every agent joins, without a network."""
    async def answers(*a):
        return 0.25

    async def joins(*a, **kw):
        return 1.5

    monkeypatch.setattr(llm, "check", answers)
    monkeypatch.setattr(session, "joins", joins)
    monkeypatch.setattr(tokens, "connect", lambda agent, room, profile=None, **kw: ("wss://x", "token"))


def test_setup(path, offline):
    """Agents, profiles, scenarios and plugins: added through the same checks as the CLI, keys to .env only."""
    async def go(c):
        said = lambda r: r.json()
        out = [await said(await c.post("/api/plugins", json={"name": "p", "kind": "llm", "base_url": "http://x/v1",
                                                               "model": "m", "key": "sk-1"})),
               await said(await c.post("/api/agents", json={"name": "bot", "sandbox": "sb"})),
               await said(await c.post("/api/profiles", json={"name": "pro", "participant": {"attributes": {"plan": "pro"}}})),
               await said(await c.post("/api/scenarios", json={"name": "s2", "prompt": "p", "tags": ["t"]}))]
        bad = [(r.status, (await r.json())["error"]) for r in [
            await c.post("/api/scenarios", json={"name": "s3", "prompt": "p", "max_turns": 0}),
            await c.post("/api/profiles", json={"name": "pro"}),
            await c.post("/api/agents", json={"name": "two", "sandbox": "a", "url": "ws://b"})]]
        lists = {k: await (await c.get(f"/api/{k}")).json() for k in ("agents", "profiles", "scenarios", "plugins")}
        gone = [(await c.delete("/api/profiles/pro", json={})).status, (await c.delete("/api/profiles/pro", json={})).status,
                (await c.post("/api/plugins/p/use", json={})).status, (await c.post("/api/plugins/nope/use", json={})).status]
        # A Plugins card's Test connection on the saved one: checked with its saved key, nothing added.
        again = await (await c.post("/api/plugins/p/check", json={})).json()
        # A card with only the model changed: a new plugin, with the active one's key.
        await c.post("/api/plugins", json={"name": "m2", "kind": "llm", "base_url": "http://x/v1", "model": "m2", "key_from": "p"})
        return out, bad, lists, gone, again
    out, bad, lists, gone, again = call(path, go)
    assert again == {"message": "'p' answered in 0.2 s"}
    assert keys.get(path.parent / ".env", "LIVETEST_KEY_M2") == "sk-1"
    assert [o["message"] for o in out] == ["added plugin 'p': answered in 0.2 s; it's the active llm",
                                           "added agent 'bot': it joined in 1.5 s", "added profile 'pro'",
                                           "added scenario 's2' as number 1"]
    assert bad == [(400, "max_turns: Input should be greater than or equal to 1"), (400, "profile 'pro' already exists"),
                   (400, "pick only one of sandbox, endpoint, url")]
    assert lists["plugins"] == [{"name": "p", "kind": "llm", "base_url": "http://x/v1", "model": "m", "voice_id": None,
                                 "active": True, "key": True}]  # whether a key is set, never the key
    assert keys.get(path.parent / ".env", "LIVETEST_KEY_P") == "sk-1"
    assert lists["scenarios"][0]["number"] == 1 and lists["agents"][0]["sandbox"] == "sb"
    assert gone == [200, 404, 200, 404]


def test_setup_unreachable_saves_nothing(path, monkeypatch):
    async def dead(*a):
        raise ConnectionError("refused")

    monkeypatch.setattr(llm, "check", dead)

    async def go(c):
        r = await c.post("/api/plugins", json={"name": "p", "base_url": "http://x/v1", "model": "m"})
        return r.status, (await r.json())["error"], await (await c.get("/api/plugins")).json()
    assert call(path, go) == (502, "m at http://x/v1 didn't answer (refused); check the URL, model and key", [])


@pytest.fixture
def ready(path, offline):
    """An agent, a scenario and an active tester LLM, saved straight to the DB."""
    with closing(store.connect(path)) as db:
        store.add(db, Agent(name="bot", sandbox="sb"))
        store.add(db, Scenario(name="s", prompt="p"))
        store.add(db, Plugin(name="p", base_url="http://x/v1", model="m"))
        store.use(db, "p")
    return path


def test_new_run(ready, monkeypatch):
    """The run starts in the server's process; bad input names what's wrong."""
    async def fake(db, tests, *a, **kw):
        for result, _, _ in tests:
            store.begin(db, result)
            store.finish_result(db, result, "done", "max_turns", [{"check": "c", "passed": True}])

    monkeypatch.setattr(runner, "run", fake)

    async def go(c):
        want = {"name": "bot", "scenario": ["sx2"], "run": "n1", "parallel": 1}
        started = await (await c.post("/api/runs", json=want)).json()
        for _ in range(100):  # the run is a task in the server's loop
            if (await (await c.get("/api/runs/n1")).json())["run"]["status"] == "done":
                break
            await asyncio.sleep(0.02)
        bad = [(r.status, (await r.json())["error"]) for r in [await c.post("/api/runs", json=want),
                                                             await c.post("/api/runs", json=want | {"run": "n2", "scenario": ["9"]})]]
        return started, (await (await c.get("/api/runs/n1")).json())["run"], bad
    started, run, bad = call(ready, go)
    assert started == {"run": "n1"} and (run["status"], run["passed"], run["tests"]) == ("done", 2, 2)
    assert bad == [(400, "run id 'n1' is taken; pick another with --run, or leave it out"),
                   (400, "'9' picks no scenario; see: livetest scenario list")]


def test_quit_ends_its_runs_cleanly(ready, monkeypatch):
    """`ui stop` asks first: the UI's own runs are stopped and end as stopped, keeping what they recorded."""
    async def until_stopped(db, tests, *a, **kw):
        store.begin(db, tests[0][0])
        while store.result(db, tests[0][0])["status"] != "stopping":
            await asyncio.sleep(0.02)
        store.finish_result(db, tests[0][0], "stopped", "stopped by user")

    monkeypatch.setattr(runner, "run", until_stopped)

    async def go(c):
        await c.post("/api/runs", json={"name": "bot", "scenario": ["s"], "run": "n1"})
        await asyncio.sleep(0.05)
        return await (await c.post("/api/quit", json={})).json(), (await (await c.get("/api/runs/n1")).json())["run"]["status"]
    assert call(ready, go) == ({"stopped": 1}, "stopped")


def test_download_report(path):
    async def go(c):
        whole, one = await c.get("/api/runs/r/report"), await c.get("/api/runs/r/report?test=2")
        return (whole.headers["Content-Disposition"], await whole.text(), one.headers["Content-Disposition"],
                (await c.get("/api/runs/r/report?test=9")).status)
    whole, page, one, missing = call(path, go)
    assert whole == 'attachment; filename="run-r_report.html"' and one == 'attachment; filename="run-r_test-2_report.html"'
    assert "data:audio/ogg;base64," in page and missing == 404  # the same file report create writes


def test_report_screen(path):
    """The Report screen: what goes inside and how big, measured; recordings can be left out."""
    async def go(c):
        sizes = [await (await c.get(f"/api/runs/r/report/sizes{q}")).json() for q in ("", "?recordings=0", "?test=2")]
        bare = await c.get("/api/runs/r/report?recordings=0")
        return sizes, len((await (await c.get("/api/runs/r/report")).text()).encode()), await bare.text()
    (whole, bare, one), length, page = call(path, go)
    names = [s["name"] for s in whole["sections"]]
    assert names == ["Summary", "Conversations", "Timelines", "Logs", "Agent info", "Recordings", "Page"]
    assert sum(s["bytes"] for s in whole["sections"]) == whole["bytes"] == length  # exactly the file Download writes
    rec = lambda got: next(s for s in got["sections"] if s["name"] == "Recordings")
    assert rec(whole)["bytes"] > 0 and rec(bare)["bytes"] == 0 and rec(one)["bytes"] == 0  # test 2 was never recorded
    assert whole["file"] == "run-r_report.html" and one["file"] == "run-r_test-2_report.html"
    assert "data:audio" not in page and '"recordings": false' in page
