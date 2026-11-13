"""`livetest ui`: the app shell and run pages, and the JSON API they read, on this machine only.

The API serves the same rows `report create` bakes in, so the run page is one file for both. Setup and runs go
through the same runtime functions as the CLI; runs started here live in this process.
"""

import asyncio
import logging
from pathlib import Path

from aiohttp import web
from pydantic import ValidationError

from livetest.platform import keys, llm
from livetest.runtime import runner, setup, store
from livetest.runtime.models import Agent, Plugin, Profile, Scenario, key_var
from livetest.ui import report

STATIC = Path(__file__).with_name("static")
KINDS = {"agents": Agent, "profiles": Profile, "scenarios": Scenario, "plugins": Plugin}
log = logging.getLogger("livetest.ui")


@web.middleware
async def local(request: web.Request, handler):
    # Results hold system prompts and recordings, and any website can send requests to localhost. A foreign Host is
    # DNS rebinding; a write that isn't JSON is a form another site could submit (JSON needs a CORS preflight, which
    # this server never answers).
    if request.url.host not in ("127.0.0.1", "localhost"):
        raise web.HTTPForbidden()
    if request.method not in ("GET", "HEAD") and request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    return await handler(request)


def fail(status: int, error) -> web.Response:
    """What the pages show: {"error": "..."}, worded like the CLI's."""
    if isinstance(error, ValidationError):  # e.g. "max_turns: Input should be greater than or equal to 1"
        error = "; ".join(f"{'.'.join(map(str, e['loc'])) + ': ' if e['loc'] else ''}{e['msg'].removeprefix('Value error, ')}"
                          for e in error.errors())
    return web.json_response({"error": str(error)}, status=status)


def test_of(request: web.Request) -> int | None:
    try:
        return int(t) if (t := request.query.get("test")) else None
    except ValueError:
        raise web.HTTPNotFound() from None


def app(path: Path) -> web.Application:
    db, env = store.connect(path), path.parent / ".env"
    here: dict[str, asyncio.Task] = {}  # runs started from this UI, by id

    def run_of(request: web.Request) -> dict:
        if found := store.runs(db, request.match_info["run"]):
            return found[0]
        raise web.HTTPNotFound()

    async def home(_):
        return web.FileResponse(STATIC / "app.html")

    async def page(_):
        return web.FileResponse(STATIC / "run.html")  # the run, or one test (the C page): it fetches /api/runs/<run> itself

    async def runs(_):
        return web.json_response(store.runs(db))

    async def run(request: web.Request):
        try:
            return web.json_response(report.data(db, run_of(request), test_of(request),
                                                 lambda n, _: f"/api/results/{n}/recording"))
        except ValueError:  # no such test
            raise web.HTTPNotFound() from None

    def report_of(request: web.Request, make):
        """`make(db, run, test, recordings)` for the Report screen's choices: ?test=N, ?recordings=0."""
        found, test = run_of(request), test_of(request)
        try:
            return found, test, make(db, found, test, request.query.get("recordings") != "0")
        except ValueError:
            raise web.HTTPNotFound() from None

    async def download(request: web.Request):
        found, test, page = report_of(request, report.create)
        # Run ids are letters, digits, - _ . only, so the name needs no quoting.
        return web.Response(text=page, content_type="text/html",
                            headers={"Content-Disposition": f'attachment; filename="{report.filename(found["id"], test)}"'})

    async def sizes(request: web.Request):
        return web.json_response(report_of(request, report.sizes)[2])

    async def recording(request: web.Request):
        try:
            file = store.result(db, int(request.match_info["id"]))["recording"]
        except IndexError:
            raise web.HTTPNotFound() from None
        if not (file and Path(file).is_file()):
            raise web.HTTPNotFound()
        return web.FileResponse(file)  # answers Range, which the browser needs to seek

    async def stop(request: web.Request):
        test = (await request.json()).get("test")
        return web.json_response({"stopped": store.stop(db, run_of(request)["id"], test)})

    async def start(request: web.Request):
        """The new-run drawer: `run start`'s flags as JSON; the run lives in this process."""
        b = await request.json()
        try:
            # ponytail: the fail-fast token fetch blocks this loop for its HTTP call (~1 s); move it to a thread if it bites
            plan = runner.prepare(db, path.parent, b.get("name") or "", b.get("scenario") or [], run=b.get("run") or None,
                                  profile=b.get("profile") or None, parallel=int(b.get("parallel") or 4),
                                  voice=bool(b.get("voice")), plugins=b.get("plugins") or {},
                                  recordings=b.get("recordings", True), opening=b.get("start_conversation") or None,
                                  token=b.get("token") or None)
        except ValueError as e:
            return fail(400, e)
        except ConnectionError as e:
            return fail(502, e)
        ids = runner.queue(db, plan)
        here[plan.run] = task = asyncio.create_task(runner.launch(db, plan, ids, env))

        def ended(t: asyncio.Task) -> None:
            here.pop(plan.run, None)
            if not t.cancelled() and t.exception():  # each test catches its own errors; this is the run around them
                log.error("run %s failed", plan.run, exc_info=t.exception())
        task.add_done_callback(ended)
        return web.json_response({"run": plan.run})

    async def wind_down(_=None):
        """Stop this UI's runs and wait for them to end cleanly, keeping what they recorded (`ui stop`, Ctrl+C)."""
        for found in list(here):
            store.stop(db, found)
        if here:
            await asyncio.wait(list(here.values()), timeout=30)

    async def quit_(_):
        stopping = len(here)
        await wind_down()
        return web.json_response({"stopped": stopping})

    async def listed(request: web.Request):
        return web.json_response(setup.listed(db, env, KINDS[request.match_info["kind"]]))

    async def add(request: web.Request):
        """A model's fields as JSON; a plugin's "key", an agent's "key" and "secret" go to .env. Checked like the CLI's.
        A plugin's "key_from": no key typed, so use that saved plugin's (a Plugins card with only the model changed)."""
        kind, body = KINDS[request.match_info["kind"]], await request.json()
        key, secret, src = body.pop("key", None) or None, body.pop("secret", None) or None, body.pop("key_from", None)
        if src and not key:
            key = keys.get(env, key_var(src))
        try:
            model = kind(**body)
            if kind is Plugin:
                secs, first = await setup.add_plugin(db, env, model, key)
                said = f": answered in {secs:.1f} s" + (f"; it's the active {model.kind}" if first else "")
            elif kind is Agent:
                secs = await setup.add_agent(db, env, model, key, secret)
                said = f": it joined in {secs:.1f} s" if secs is not None else ": no token source, so a run needs its token"
            else:
                n = setup.save(db, model)
                said = f" as number {n}" if kind is Scenario else ""
        except ValueError as e:  # ValidationError too
            return fail(400, e)
        except ConnectionError as e:
            return fail(502, e)
        return web.json_response({"message": f"added {setup.label(kind)} '{model.name}'{said}"})

    async def remove(request: web.Request):
        kind, name = KINDS[request.match_info["kind"]], request.match_info["name"]
        if not setup.remove(db, env, kind, name):
            raise web.HTTPNotFound()
        return web.json_response({"message": f"removed {setup.label(kind)} '{name}'"})

    async def use(request: web.Request):
        if not store.use(db, name := request.match_info["name"]):
            raise web.HTTPNotFound()
        return web.json_response({"message": f"'{name}' is now active"})

    async def check(request: web.Request):
        """A Plugins card's Test connection on the saved plugin, with its saved key."""
        try:
            secs = await setup.check_plugin(env, setup.find(db, Plugin, name := request.match_info["name"]))
        except ValueError:
            raise web.HTTPNotFound() from None
        except ConnectionError as e:
            return fail(502, e)
        return web.json_response({"message": f"'{name}' answered in {secs:.1f} s"})

    async def presets(_):
        return web.json_response(llm.PRESETS)

    async def settings(_):  # the Settings page: where things live
        return web.json_response({"db": str(path), "env": str(env), "recordings": str(path.parent / "recordings")})

    async def close(_):
        db.close()

    a = web.Application(middlewares=[local])
    a.on_shutdown.append(wind_down)
    a.on_cleanup.append(close)
    kinds = "{kind:" + "|".join(KINDS) + "}"
    a.add_routes([web.get("/", home), web.get("/runs/{run}", page), web.get(r"/runs/{run}/tests/{test:\d+}", page),
                  web.get("/api/runs", runs), web.post("/api/runs", start), web.get("/api/runs/{run}", run),
                  web.get("/api/runs/{run}/report", download), web.get("/api/runs/{run}/report/sizes", sizes),
                  web.post("/api/runs/{run}/stop", stop),
                  web.get(r"/api/results/{id:\d+}/recording", recording), web.post("/api/quit", quit_),
                  web.get(f"/api/{kinds}", listed), web.post(f"/api/{kinds}", add),
                  web.delete(f"/api/{kinds}/{{name}}", remove), web.post("/api/plugins/{name}/use", use),
                  web.post("/api/plugins/{name}/check", check), web.get("/api/presets", presets),
                  web.get("/api/settings", settings), web.static("/fonts", STATIC / "fonts")])
    return a
