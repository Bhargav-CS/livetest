"""The CLI (entry point): parse flags, call runtime, print. The UI calls the same runtime functions."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from collections import Counter
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

import jwt
from pydantic import ValidationError

from livetest.platform import keys, llm, stt, tts
from livetest.platform import logging as logs
from livetest.platform.db import DEFAULT_PATH
from livetest.runtime import runner, session, store, tokens
from livetest.runtime.models import Agent, Participant, Plugin, Profile, Scenario, key_var

ISSUES = "https://github.com/Bhargav-CS/livetest/issues"
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
# Left out of run logs' text view, kept in --json and the DB: partial transcripts repeat the _heard line, and usage is
# cumulative (the verdict has the totals; the UI derives per-turn tokens from the stream).
QUIET = {"user_input_transcribed", "session_usage_updated"}


class Fail(Exception):
    """A user error: main() prints it as `error: ...` and exits 2."""


def ask(question: str) -> str:
    """Prompt only when a person is at the terminal; scripts and CI get '' instead of a hang."""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(question).strip()
    except EOFError:  # stdin closed; on Windows NUL even claims to be a terminal
        print()
        return ""


def need(value: str | None, question: str, flag: str) -> str:
    """The flag's value, else ask at a terminal, else fail naming the flag (CLI standard rule 5)."""
    if value := value or ask(question):
        return value
    raise Fail(f"missing {flag}")


def confirm(question: str, args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise Fail("this needs confirmation; pass -y to skip the question")
    return ask(f"{question} [y/N] ").lower() in ("y", "yes")


def value(text: str):
    """A flag value as JSON when it parses (42, true, {"a": 1}), else the plain string."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def metadata(text: str) -> dict | str:
    """LiveKit metadata: a JSON object stays an object, anything else is a string."""
    return m if isinstance(m := value(text), dict) else text


def pairs(items: list[str] | None, parse=value) -> dict:
    """['k=v', ...] → {k: parse(v)}."""
    out = {}
    for item in items or []:
        k, sep, v = item.partition("=")
        if not (k and sep):
            raise Fail(f"expected key=value, got '{item}'")
        out[k] = parse(v)
    return out


def given(**fields) -> dict:
    """Drop flags that weren't passed, so the model's defaults apply."""
    return {k: v for k, v in fields.items() if v is not None}


def label(kind: type) -> str:
    return kind.__name__.lower()  # agent, profile, scenario: also the CLI group's name


def flag(kind: type) -> str:
    return "name" if kind is Agent else label(kind)  # rule 3: --name is only ever the agent


def env_file(args: argparse.Namespace) -> Path:
    return args.db.parent / ".env"  # keys sit next to the DB, never in it


def errors_file(args: argparse.Namespace) -> Path:
    return args.db.parent / "errors.log"


def find(db: sqlite3.Connection, kind: type, name: str):
    """A saved agent, profile or plugin by name."""
    if found := {m.name: m for _, m in store.rows(db, kind)}.get(name):
        return found
    raise Fail(f"no {label(kind)} named '{name}'; see: livetest {label(kind)} list")


def secret_vars(kind: type, name: str) -> list[str]:
    """The .env vars holding a plugin's key or an agent's API key and secret."""
    return [key_var(name, w) for w in ("AGENT_KEY", "AGENT_SECRET")] if kind is Agent else [key_var(name)]


def free(db: sqlite3.Connection, kind: type, name: str) -> None:
    """Refuse a taken name, or one that would share another's keys in .env (a-b and a_b)."""
    if taken := [m.name for _, m in store.rows(db, kind) if key_var(m.name) == key_var(name)]:
        raise Fail(f"{label(kind)} '{taken[0]}' already exists" + ("" if taken[0] == name else f"; '{name}' would share its key"))


def save(db: sqlite3.Connection, model) -> int:
    try:
        n = store.add(db, model)
    except sqlite3.IntegrityError:
        raise Fail(f"{label(type(model))} '{model.name}' already exists") from None
    print(f"added {label(type(model))} '{model.name}'" + (f" as number {n}" if isinstance(model, Scenario) else ""))
    return 0


def cmd_init(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    # connect() in main() already created the DB; init is the friendly first step that says so.
    print(f"livetest is set up: {args.db}")
    # The agent first: it's what you came to test.
    again = ["--db", str(args.db), *(["-v"] if args.verbose else [])]
    if not store.rows(db, Agent) and offer("agent", "the one you test", "agent add -h"):
        if code := main([*again, "agent", "add"]):
            return code
    if "llm" not in store.active(db) and offer("tester LLM", "the model that plays the user", "plugin add -h"):
        return main([*again, "plugin", "add", "--kind", "llm"])
    return 0


def offer(what: str, why: str, see: str) -> bool:
    """Ask a person whether to add a missing thing now; scripts are told where to look instead."""
    if sys.stdin.isatty():
        return ask(f"No {what} yet ({why}). Add one now? [Y/n] ").lower() in ("", "y", "yes")
    print(f"no {what} yet; see: livetest {see}")
    return False


def cmd_plugin_add(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    kind = need(args.kind, "Kind (llm: thinks, stt: hears, tts: speaks): ", "--kind")
    name = need(args.plugin, "Plugin name: ", "--plugin")
    url = args.base_url or llm.PRESETS.get(args.provider) or need(
        None, f"Provider ({', '.join(llm.PRESETS)}) or a base URL, e.g.\n"
              "  https://api.together.xyz/v1   any OpenAI-compatible API\n"
              "  http://localhost:8000/v1      a server on your machine (vLLM; Ollama is http://localhost:11434/v1)\n"
              "Provider or base URL: ", "--base-url")
    plugin = Plugin(name=name, kind=kind, base_url=llm.PRESETS.get(url, url),
                    model=need(args.model, "Model: ", "--model"), voice_id=args.voice_id)
    free(db, Plugin, name)
    file = env_file(args)
    # ponytail: on Windows getpass reads the console even when stdin is redirected; fine until a CI box hangs on it
    key = getpass.getpass("API key (Enter for none): ").strip() if sys.stdin.isatty() else ""
    try:
        check = {"llm": llm.check, "stt": stt.check, "tts": tts.check}[plugin.kind]
        secs = asyncio.run(check(plugin.base_url, plugin.model, key or keys.get(file, key_var(name)),
                                 *([plugin.voice_id] if plugin.voice_id else [])))
    except ConnectionError as e:
        print(f"error: {plugin.model} at {plugin.base_url} didn't answer ({e}); check the URL, model and key",
              file=sys.stderr)
        return 3
    print({"llm": f"checked: first token in {secs:.1f} s", "tts": f"checked: first audio in {secs:.1f} s"}
          .get(plugin.kind, "checked: it streams"))
    save(db, plugin)
    if key:
        keys.save(file, key_var(name), key)
    if plugin.kind not in store.active(db):
        store.use(db, name)
        print(f"it's the active {plugin.kind}")
    return 0


def cmd_plugin_use(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    name = need(args.plugin, "Plugin to make active: ", "--plugin")
    if not store.use(db, name):
        raise Fail(f"no plugin named '{name}'; see: livetest plugin list")
    print(f"'{name}' is now active")
    return 0


def cmd_plugin_list(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    on = {p.name for p in store.active(db).values()}
    found = [m.model_dump(mode="json") | {"active": m.name in on, "key": bool(keys.get(env_file(args), key_var(m.name)))}
             for _, m in store.rows(db, Plugin)]  # whether a key is set, never the key
    return listing(args, found, [f"{'*' if p['active'] else ' '} {p['name']:<20} {p['kind']:<4} {p['base_url']:<40} "
                                 f"{p['model']}  key: {'set' if p['key'] else 'none'}" for p in found], "plugin add -h")


def cmd_agent_add(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    name = need(args.name, "Agent name (its LiveKit agent_name): ", "--name")
    source = given(sandbox=args.sandbox, endpoint=args.endpoint, url=args.url)
    if not source and (where := ask("Sandbox ID, token endpoint (https://...) or LiveKit URL (wss://...);\n"
                                    "Enter to pass a token when you run it: ")):
        source = {"url" if where.startswith(("ws://", "wss://")) else
                  "endpoint" if where.startswith(("http://", "https://")) else "sandbox": where}
    agent = Agent(name=name, **source)
    free(db, Agent, name)
    file, names = env_file(args), secret_vars(Agent, name)
    # ponytail: on Windows getpass reads the console even when stdin is redirected; same as plugin add
    typed = {v: getpass.getpass(f"{q} (Enter to use ${v}): ").strip()
             for v, q in zip(names, ("LiveKit API key", "API secret"))} if agent.url and sys.stdin.isatty() else {}
    if source:  # like plugin add: prove it works, save nothing if it doesn't
        key, secret = (typed.get(v) or keys.get(file, v) for v in names)
        try:
            url, token = tokens.connect(agent, f"livetest-{uuid.uuid4().hex[:8]}", key=key, secret=secret)
            secs = asyncio.run(session.joins(url, token, key, secret))
        except ConnectionError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        except ValueError as e:
            raise Fail(e) from None
        print(f"checked: {name} joined in {secs:.1f} s")
    else:
        print("no token source: pass its token with --token when you run it")
    save(db, agent)
    for v, s in typed.items():
        if s:
            keys.save(file, v, s)
    return 0


def cmd_profile_add(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    return save(db, Profile(
        name=need(args.profile, "Profile name: ", "--profile"),
        room=given(metadata=args.room_metadata),
        participant=given(identity=args.identity, name=args.participant_name, metadata=args.metadata,
                          attributes=pairs(args.attribute, str)),
        dispatch=given(metadata=args.dispatch_metadata),
    ))


def cmd_scenario_add(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    expect = [{"type": t, "name": name, "args": pairs(rest)}
              for t, calls in (("tool_called", args.tool_called), ("tool_not_called", args.tool_not_called))
              for name, *rest in calls or []]
    if args.no_tool_errors:
        expect.append({"type": "no_tool_errors"})
    if args.p95_ms is not None or args.p99_ms is not None:
        expect.append(given(type="latency", p95_ms=args.p95_ms, p99_ms=args.p99_ms))
    return save(db, Scenario(**given(
        name=need(args.scenario, "Scenario name: ", "--scenario"),
        prompt=need(args.prompt, "Prompt (the tester LLM's whole system prompt): ", "--prompt"),
        tags=args.tag, max_turns=args.max_turns, max_minutes=args.max_minutes, expect=expect,
    )))


def summary(n: int, m) -> str:
    """One `list` line per agent, profile or scenario."""
    if isinstance(m, Scenario):
        return f"{n:>4}  {m.name:<24} {','.join(m.tags):<16} {' '.join(m.prompt.split())[:80]}"
    rest = m.model_dump(exclude={"name"}, exclude_none=True, exclude_defaults=True)
    if isinstance(m, Agent):  # at most one connection is set
        return f"{m.name:<24} " + (" ".join(f"{k} {v}" for k, v in rest.items()) or "token at run time")
    return f"{m.name:<24} {json.dumps(rest)}"


def listing(args: argparse.Namespace, found: list, lines: list[str], start: str) -> int:
    """What every `list` prints: JSON for scripts, else a line per row, or where to start when there are none."""
    print(json.dumps(found, indent=2) if args.json else "\n".join(lines) or f"none yet; see: livetest {start}")
    return 0


def cmd_list(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    found = [(n, m) for n, m in store.rows(db, args.kind) if not getattr(args, "tag", None) or args.tag in m.tags]
    number = args.kind is Scenario  # scenarios are picked by number in `run start`
    return listing(args, [({"number": n} if number else {}) | m.model_dump(mode="json") for n, m in found],
                   [summary(n, m) for n, m in found], f"{label(args.kind)} add -h")


def cmd_remove(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    what = label(args.kind)
    name = need(getattr(args, flag(args.kind)), f"{what.capitalize()} name to remove: ", f"--{flag(args.kind)}")
    if not confirm(f"Remove {what} '{name}'?", args):
        print("cancelled")
        return 0
    if not store.remove(db, args.kind, name):
        raise Fail(f"no {what} named '{name}'; see: livetest {what} list")
    if args.kind in (Agent, Plugin):
        for v in secret_vars(args.kind, name):  # its keys go with it
            keys.remove(env_file(args), v)
    print(f"removed {what} '{name}'")
    return 0


def outcome(r: dict) -> str:
    return {1: "passed", 0: "failed"}.get(r["passed"], r["status"])


def verdict(r: dict) -> str:
    """One line per test: number, scenario, outcome, the failed checks or how it ended, and the agent's errors."""
    failed = [c["check"] for c in json.loads(r["checks"] or "[]") if not c["passed"]]
    why = (", ".join(failed) or r["ended"] or "")[:200]  # an error's text can be a whole response body
    errors = f"  ({r['agent_errors']} agent errors)" if r["agent_errors"] else ""
    last = f"\n     last agent error: {' '.join(r['last_error'].split())[:200]}" if r["passed"] == 0 and r["last_error"] else ""
    return f"#{r['test']:<3} {json.loads(r['scenario'])['name']:<24} {outcome(r).upper():<16} {why}{errors}{used([r])}{last}"


def used(results: list[dict]) -> str:
    """LLM tokens summed over `results`, per side: '  tokens agent 900 in (512 cached) / 40 out, tester 250 in / 12 out'.
    Counts only (no cost in v0.1); STT / TTS and per-model counts are in --json."""
    parts = []
    for side in ("agent", "tester"):
        if rows := [x for r in results for x in json.loads(r["usage"] or "{}").get(side, []) if x["kind"] == "llm"]:
            i, c, o = (sum(x.get(k, 0) for x in rows) for k in ("input_tokens", "input_cached_tokens", "output_tokens"))
            parts.append(f"{side} {i} in{f' ({c} cached)' if c else ''} / {o} out")
    return "  tokens " + ", ".join(parts) if parts else ""


def run_line(r: dict) -> str:
    return (f"{r['id']:<16} {r['agent']:<20} {r['started_at']}  {r['finished']}/{r['tests']} tests  "
            f"{r['passed']}/{r['checked']} passed  {r['status']}")


def event_line(agent: str, test: int, ts: float, source: str, kind: str, data: dict) -> str:
    ev = data.get(kind) or {}  # session-host events nest under their own name
    if kind in ("tester_turn", "tester_heard"):  # tester_heard: voice, the tester's STT of the agent
        what = f"{kind.removesuffix('_turn')}: {data['text']}"
    elif m := (ev.get("item") or {}).get("message"):  # a USER item is what the agent took in (voice: what its STT heard)
        what = f"agent_{agent}{'_heard' if m['role'] == 'USER' else ''}: " + " ".join(c.get("text", "") for c in m.get("content", []))
    elif source == "log":
        what = f"[log] {kind} {data['logger']}: {data['message']}"
    elif "new_state" in ev:
        what = f"[{source}] {kind} {ev['new_state']}"
    else:
        what = f"[{source}] {kind} {json.dumps(ev)[:160]}" if ev else f"[{source}] {kind}"
    return f"{time.strftime('%H:%M:%S', time.localtime(ts))} #{test:<3} {what}"


def pick_run(db: sqlite3.Connection, args: argparse.Namespace) -> dict:
    run = need(args.run, "Run (its id, or latest): ", "--run")
    if found := store.runs(db, run):
        return found[0]
    raise Fail(f"no run '{run}'; see: livetest run list")


def cmd_run_start(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    agent = find(db, Agent, need(args.name, "Agent to test: ", "--name"))
    items = args.scenario or need(None, "Scenarios to run (e.g. 1-5 7x3, or all): ", "--scenario").split()
    try:
        tests = runner.select(items, store.rows(db, Scenario))
        run = runner.run_id(args.run) if args.run else store.next_run_id(db)
    except ValueError as e:
        raise Fail(e) from None
    if store.runs(db, run):
        raise Fail(f"run id '{run}' is taken; pick another with --run, or leave it out")
    if args.parallel < 1:
        raise Fail("--parallel must be 1 or more")
    profile = find(db, Profile, args.profile) if args.profile else None
    plugin = tester_plugin(db, "llm", args.llm)
    voice = (tester_plugin(db, "stt", args.stt), tester_plugin(db, "tts", args.tts)) if args.voice else ()
    token = args.token or os.environ.get("LIVETEST_TOKEN")
    if token and len(tests) > 1:
        a = agent.name
        raise Fail(f"a raw token joins one room, so it runs one test; this selection has {len(tests)}.\n"
                   f"To run more, save how livetest gets tokens for '{a}': remove it, then add it again with one of\n"
                   f"  livetest agent rm --name {a} -y\n  livetest agent add --name {a} --sandbox <sandbox-id>\n"
                   f"  livetest agent add --name {a} --endpoint <your-token-endpoint-url>\n"
                   f"  livetest agent add --name {a} --url <livekit-server-url>   # asks for its API key and secret")
    file = env_file(args)
    key, secret = (keys.get(file, v) for v in secret_vars(Agent, agent.name)) if agent.url else (None, None)

    def connect(room: str) -> tuple[str, str]:
        return tokens.connect(agent, room, profile, token=token, url=os.environ.get("LIVEKIT_URL"), key=key, secret=secret)

    try:
        connect(f"livetest-{run}-check")  # fail fast (bad token, profile mismatch, missing key), before anything is saved
    except ValueError as e:
        raise Fail(e) from None
    except ConnectionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    rooms = ([jwt.decode(token, options={"verify_signature": False}).get("video", {}).get("room", "")] if token  # its room
             else [f"livetest-{run}-{n}-{uuid.uuid4().hex[:6]}" for n in range(1, len(tests) + 1)])
    folder = args.db.parent / "recordings" / f"run-{run}" if voice and args.recordings else None
    print(f"run id: {run}\n{agent.name}: {len({n for n, _ in tests})} scenarios, {len(tests)} tests, "
          f"{'voice' if voice else 'text'} mode, {args.parallel} in parallel, profile {profile.name if profile else 'none'}, "
          f"tester {', '.join(f'{p.kind} {p.name}' for p in (plugin, *voice))}"
          + (f"\nrecording: {f'on ({folder}, one file per test)' if folder else 'off'}" if voice else "")
          + "\nopens: " + (f'tester ("{args.start_conversation}")' if args.start_conversation else "agent"))
    if not confirm("continue?", args):
        print("cancelled")
        return 0

    ids = store.start_run(db, run, agent.name, profile, [(s, room) for (_, s), room in zip(tests, rooms)],
                          "voice" if voice else "text")
    print(f"running; verdicts print when it ends, Ctrl+C stops it. Follow it: livetest run logs --run {run}", flush=True)

    async def go() -> None:
        key_of = lambda p: keys.get(file, key_var(p.name))
        tester = llm.llm(plugin.base_url, plugin.model, key_of(plugin))
        ears_mouth = (stt.stt(voice[0].base_url, voice[0].model, key_of(voice[0])),
                      tts.tts(voice[1].base_url, voice[1].model, key_of(voice[1]), voice[1].voice_id)) if voice else None
        try:
            await runner.run(db, [(i, s, room) for i, (_, s), room in zip(ids, tests, rooms)],
                             connect, tester, args.parallel, key, secret, ears_mouth, folder,
                             opening=args.start_conversation)
        finally:
            for model in (tester, *(ears_mouth or ())):
                await model.aclose()

    interrupted = False
    try:
        asyncio.run(go())
    except KeyboardInterrupt:  # Ctrl+C = run stop: running tests ended as stopped, queued ones never start
        interrupted = True
        store.stop(db, run)
    found = store.results(db, run)
    print("\n".join(map(verdict, found)))
    print(f"run {run}: " + ", ".join(f"{n} {w}" for w, n in Counter(map(outcome, found)).items()) + used(found))
    if any(r["ended"] == "no_greeting" for r in found):
        print('the agent never greeted; if the tester should open, pass --start-conversation "<its first line>"')
    if errored := [r["test"] for r in found if r["status"] == "error"]:
        print(f"test {', '.join(map(str, errored))} hit an error; see: livetest run logs --run {run} --test {errored[0]}\n"
              f"tracebacks are in {errors_file(args)}; if it looks like a livetest bug, report it with "
              f"`livetest --version`: {ISSUES}")
    statuses = {r["status"] for r in found}
    if interrupted:
        return 130
    if statuses & {"never_joined", "no_session_host"}:
        return 3
    return 1 if "error" in statuses or any(r["passed"] == 0 for r in found) else 0


def tester_plugin(db: sqlite3.Connection, kind: str, name: str | None) -> Plugin:
    """The tester's LLM / STT / TTS: flag > LIVETEST_<KIND> > the active one."""
    if not (name := name or os.environ.get(f"LIVETEST_{kind.upper()}")):
        if found := store.active(db).get(kind):
            return found
        raise Fail(f"no tester {kind.upper()} yet; see: livetest plugin add --kind {kind} -h")
    if (found := find(db, Plugin, name)).kind != kind:
        raise Fail(f"'{name}' is a {found.kind} plugin, not {kind}; see: livetest plugin list")
    return found


def cmd_run_list(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    found = store.results(db, pick_run(db, args)["id"]) if args.run else store.runs(db)
    return listing(args, found, [verdict(r) if args.run else run_line(r) for r in found], "run start -h")


def cmd_run_logs(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    run, after = pick_run(db, args), 0
    while True:
        live = store.runs(db, run["id"])[0]["status"] in ("running", "stopping")  # read first: the last events still print
        for rowid, test, ts, source, kind, data in store.run_events(db, run["id"], args.test, after):
            after = rowid
            if source == "log" and LEVELS.index(kind) < LEVELS.index(args.level):
                continue
            if kind in QUIET and not args.json:
                continue
            print(json.dumps({"test": test, "ts": ts, "source": source, "type": kind, "data": data}) if args.json
                  else event_line(run["agent"], test, ts, source, kind, data), flush=True)
        if not live:
            return 0
        time.sleep(0.5)


def cmd_run_stop(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    run = pick_run(db, args)["id"]
    what = f"test {args.test} of run {run}" if args.test else f"run {run}"
    if not confirm(f"Stop {what}?", args):
        print("cancelled")
        return 0
    if not (n := store.stop(db, run, args.test)):
        raise Fail(f"{what} has nothing running or queued")
    print(f"stopping {what} ({n} test{'s' * (n > 1)}); a running test ends within a few seconds and keeps what it recorded")
    return 0


def cmd(sub, name: str, func, help: str, *examples: str, **kw) -> argparse.ArgumentParser:
    """One command or group. Every help screen ends with examples (CLI standard rule 4); func=None prints help."""
    p = sub.add_parser(name, help=help, description=kw.pop("description", help), **kw,
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog="examples:\n" + "\n".join(f"  {e}" for e in examples))
    p.set_defaults(func=func, help_of=p)
    # Global, so it works after the command too; SUPPRESS keeps the root's value when it's not given here.
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                   help="debug logs and tracebacks on the console")
    return p


def group(sub, kind: type, help: str, *examples: str):
    """`livetest <kind>`: returns its actions' subparsers. Add `add`, then call list_and_remove (help lists in order)."""
    return cmd(sub, label(kind), None, help, *examples).add_subparsers(dest="action", title="actions", metavar="<action>")


def list_and_remove(actions, kind: type) -> None:
    """The shared actions every group has."""
    what, f = label(kind), flag(kind)
    a = "an" if what[0] in "aeiou" else "a"
    p = cmd(actions, "list", cmd_list, f"list {what}s", f"livetest {what} list", f"livetest {what} list --json")
    p.add_argument("--json", action="store_true", help="print JSON for scripts")
    p.set_defaults(kind=kind)
    p = cmd(actions, "remove", cmd_remove, f"remove {a} {what}", f"livetest {what} remove --{f} my-{what}",
            f"livetest {what} rm --{f} my-{what} -y",
            description=f"Remove {a} {what}. Asks first at a terminal; scripts pass -y.", aliases=["rm"])
    p.add_argument(f"--{f}", help=f"the {what}'s name")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(kind=kind)


# Root help lists commands grouped by purpose; argparse's own list is alphabetical and flat.
# Add each new command here as it lands.
COMMANDS = """\
get started:
  init        set livetest up

manage:
  agent       add, list or remove the agents you test
  profile     add, list or remove who the tester is, as the agent sees them
  scenario    add, list or remove what the tester is told to do, and what to check
  plugin      add, list, remove or pick the tester's LLM, STT and TTS

test:
  run         start, list, follow or stop runs: scenarios against an agent
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="livetest",
        usage="livetest <command> [<action>] [flags]",
        description="End-to-end tests for any LiveKit agent, from just a token.\n\n" + COMMANDS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="start here:\n  livetest init\n  livetest agent add --name my-agent --sandbox my-sandbox-id\n\n"
               "every command and action takes -h, e.g. livetest agent add -h",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_PATH, help=f"database file (default: {DEFAULT_PATH})")
    parser.add_argument("--version", action="version", version=version("livetest"))
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logs and tracebacks on the console")
    # A command or group typed without its action prints its help instead of an error.
    parser.set_defaults(func=None, help_of=parser)
    # SUPPRESS keeps argparse's flat, alphabetical list out of root help (COMMANDS shows them).
    sub = parser.add_subparsers(dest="command", help=argparse.SUPPRESS, prog="livetest")

    cmd(sub, "init", cmd_init, "Set livetest up.", "livetest init")

    actions = group(sub, Agent, "Add, list or remove the agents you test.",
                    "livetest agent add --name my-agent --sandbox my-sandbox-id", "livetest agent list",
                    "livetest agent remove --name my-agent")
    p = cmd(actions, "add", cmd_agent_add, "add an agent and how to get a token for it",
            "livetest agent add                      # asks for the name and sandbox ID",
            "livetest agent add --name my-agent --sandbox my-sandbox-id",
            "livetest agent add --name my-agent --url ws://localhost:7880",
            "livetest agent add --name my-agent --endpoint https://example.com/api/token",
            description="Add an agent to test. Give at most one of --sandbox, --endpoint, --url.\n"
                        "Leave out --name or all three and livetest asks for them. It proves the agent works first:\n"
                        "gets a token, joins a fresh room and waits for the agent (one short session); saves nothing\n"
                        "if it never joins. With --url it asks for the API key and secret (hidden) and saves them in\n"
                        ".env next to the database; scripts set LIVETEST_AGENT_KEY_<NAME> / LIVETEST_AGENT_SECRET_<NAME>.\n"
                        "With none of the three, pass a token with --token each time you use the agent.")
    p.add_argument("--name", help="the agent's LiveKit agent_name (what it registered with)")
    p.add_argument("--sandbox", metavar="ID", help="LiveKit sandbox token server ID")
    p.add_argument("--endpoint", metavar="URL", help="your token endpoint")
    p.add_argument("--url", help="LiveKit server URL; livetest makes tokens with the agent's API key and secret")
    list_and_remove(actions, Agent)

    actions = group(sub, Profile, "Add, list or remove profiles: who the tester is, as the agent sees them.",
                    "livetest profile add --profile pro-user --attribute plan=pro", "livetest profile list",
                    "livetest profile remove --profile pro-user")
    p = cmd(actions, "add", cmd_profile_add, "add a profile",
            "livetest profile add --profile pro-user --participant-name Priya --attribute plan=pro --attribute lang=en",
            "livetest profile add --profile acme --room-metadata '{\"tenant\": \"acme\"}' --dispatch-metadata '{\"job\": 1}'",
            description="Add a profile: the metadata and attributes the agent sees for the tester.\n"
                        "Metadata flags take a JSON object or plain text. Pick it per run: run start --profile NAME.")
    p.add_argument("--profile", help="its name")
    p.add_argument("--identity", help=f"the tester's participant identity (default: {Participant().identity})")
    p.add_argument("--participant-name", metavar="NAME", help="the tester's display name")
    p.add_argument("--attribute", action="append", metavar="KEY=VALUE",
                   help="participant attribute, repeatable; the agent reads participant.attributes")
    p.add_argument("--metadata", type=metadata, metavar="JSON|TEXT", help="the agent reads participant.metadata")
    p.add_argument("--room-metadata", type=metadata, metavar="JSON|TEXT", help="the agent reads ctx.room.metadata")
    p.add_argument("--dispatch-metadata", type=metadata, metavar="JSON|TEXT", help="the agent reads ctx.job.metadata")
    list_and_remove(actions, Profile)

    actions = group(sub, Scenario, "Add, list or remove scenarios: the tester's prompt and what to check.",
                    "livetest scenario add --scenario reschedule --prompt 'You want your Tuesday slot moved to Friday.'",
                    "livetest scenario list --tag booking", "livetest scenario remove --scenario reschedule")
    p = cmd(actions, "add", cmd_scenario_add, "add a scenario",
            "livetest scenario add --scenario reschedule --prompt 'You want your Tuesday slot moved to Friday.'",
            "livetest scenario add --scenario angry --tag billing --prompt 'You are an angry customer who wants a "
            "refund. Reply with one short turn.' --tool-called issue_refund amount=40 --tool-not-called cancel_account "
            "--no-tool-errors --p95-ms 1500",
            description="Add a scenario. --prompt is the tester LLM's whole system prompt, sent as written: say who it\n"
                        "plays, what it wants and how it talks. Values after = are read as JSON when they parse\n"
                        "(42, true), else as text. Each scenario gets a number, never reused, for run start --scenario.")
    p.add_argument("--scenario", help="its name")
    p.add_argument("--prompt", help="the tester LLM's whole system prompt")
    p.add_argument("--tag", action="append", help="repeatable; groups scenarios for scenario list --tag")
    p.add_argument("--max-turns", type=int, metavar="N", help="end the test after this many turns (default: 10)")
    p.add_argument("--max-minutes", type=float, metavar="N", help="end the test after this long (default: 5)")
    p.add_argument("--tool-called", nargs="+", action="append", metavar=("NAME", "KEY=VALUE"),
                   help="expect this tool called, with these args; repeatable")
    p.add_argument("--tool-not-called", nargs="+", action="append", metavar=("NAME", "KEY=VALUE"),
                   help="expect this tool never called (with these args, if given); repeatable")
    p.add_argument("--no-tool-errors", action="store_true", help="expect no tool call to fail")
    p.add_argument("--p95-ms", type=int, metavar="MS", help="reply latency limit, 95th percentile")
    p.add_argument("--p99-ms", type=int, metavar="MS", help="reply latency limit, 99th percentile")
    list_and_remove(actions, Scenario)
    actions.choices["list"].add_argument("--tag", help="only scenarios with this tag")

    actions = group(sub, Plugin, "Add, list, remove or pick plugins: the LLM the tester thinks with, and the STT and TTS it hears\n"
                    "and speaks with in voice mode. One per kind is active.",
                    "livetest plugin add --kind llm --plugin local-qwen --base-url http://localhost:8000/v1 --model Qwen/Qwen3.5-2B",
                    "livetest plugin use --plugin my-groq", "livetest plugin list")
    p = cmd(actions, "add", cmd_plugin_add, "add a plugin; checks that it answers first",
            "livetest plugin add                      # asks for everything",
            "livetest plugin add --kind llm --plugin local-qwen --base-url http://localhost:8000/v1 --model Qwen/Qwen3.5-2B",
            "livetest plugin add --kind llm --plugin my-groq --provider groq --model openai/gpt-oss-120b",
            "livetest plugin add --kind llm --plugin together --base-url https://api.together.xyz/v1 --model meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "livetest plugin add --kind stt --plugin moonshine --base-url http://localhost:9092/v1 --model moonshine-base",
            "livetest plugin add --kind tts --plugin kokoro --base-url http://localhost:8881/v1 --model kokoro-7m --voice-id af_msa",
            description="Add a plugin: any OpenAI-compatible server. At a terminal it asks for the API key (hidden) and\n"
                        "saves it in .env next to the database, never in it; scripts set LIVETEST_KEY_<PLUGIN> instead.\n"
                        "Makes one streamed call first and saves nothing if that fails. The first of its kind is active.\n"
                        "An STT must stream (OpenAI's realtime websocket): a batch one would add seconds to every turn.")
    p.add_argument("--plugin", help="its name")
    p.add_argument("--kind", choices=["llm", "stt", "tts"],
                   help="what it does: think (llm), or hear (stt) and speak (tts) in voice mode")
    p.add_argument("--voice-id", metavar="VOICE", help="tts: the voice to speak with, e.g. alloy (OpenAI)")
    p.add_argument("--provider", choices=llm.PRESETS, help="fills in --base-url for a cloud provider")
    p.add_argument("--base-url", metavar="URL", help="any OpenAI-compatible URL: an API (https://api.together.xyz/v1) "
                                                     "or your own server (http://localhost:8000/v1)")
    p.add_argument("--model", help="the model's name on that server")
    list_and_remove(actions, Plugin)
    actions.choices["list"].set_defaults(func=cmd_plugin_list)
    p = cmd(actions, "use", cmd_plugin_use, "make a plugin the active one of its kind",
            "livetest plugin use --plugin my-groq")
    p.add_argument("--plugin", help="its name")

    actions = cmd(sub, "run", None, "Start, list, follow or stop runs: scenarios against an agent.",
                  "livetest run start --name my-agent --scenario 1-5", "livetest run list",
                  "livetest run logs --run latest", "livetest run stop --run latest"
                  ).add_subparsers(dest="action", title="actions", metavar="<action>")
    p = cmd(actions, "start", cmd_run_start, "run scenarios against an agent",
            "livetest run start --name my-agent --scenario 1-20x10 25-35x2",
            "livetest run start --name my-agent --scenario all --profile pro-user --parallel 8 -y",
            "livetest run start --name my-agent --scenario reschedule --run nightly-1",
            "livetest run start --name my-agent --scenario 1-5 --voice --start-conversation \"Hi, I need to reschedule\"",
            "livetest run start --name my-agent --scenario 3 --token <token>   # an agent saved without a token source",
            description="Run scenarios against an agent, in text mode or --voice. --scenario takes numbers (7), ranges (1-20),\n"
                        "names and all; xN repeats one (1-20x10). Shows the plan and asks first; scripts pass -y.\n"
                        "Prints each test's verdict at the end; run logs follows the conversations. Ctrl+C stops the\n"
                        "run like run stop, keeping what it recorded. A test fails if the agent doesn't start a reply within 10 s;\n"
                        "provider errors it retries past only show up as a count of agent errors.\n"
                        "Exit code: 0 all passed, 1 a test failed, 2 bad input, 3 the agent never joined.")
    p.add_argument("--name", help="the agent to test")
    p.add_argument("--scenario", nargs="+", metavar="ITEM", help="what to run: 7, 1-20, a name or all; xN repeats")
    p.add_argument("--run", metavar="ID", help="the run's id (default: the next free number)")
    p.add_argument("--profile", help="who the tester is, as the agent sees them (default: a plain participant)")
    p.add_argument("--parallel", type=int, default=4, metavar="N",
                   help="tests at once (default: 4); the agent's hosting sets the real limit")
    p.add_argument("--llm", metavar="PLUGIN", help="the tester's LLM, if not the active one (env: LIVETEST_LLM)")
    p.add_argument("--voice", action="store_true", help="talk to the agent by voice: needs a tester STT and TTS")
    p.add_argument("--start-conversation", metavar="TEXT",
                   help="the tester opens by saying exactly TEXT (default: the agent opens, and a test fails if it "
                        "doesn't greet within 10 s)")
    p.add_argument("--stt", metavar="PLUGIN", help="voice: the tester's STT, if not the active one (env: LIVETEST_STT)")
    p.add_argument("--tts", metavar="PLUGIN", help="voice: the tester's TTS, if not the active one (env: LIVETEST_TTS)")
    p.add_argument("--recordings", action=argparse.BooleanOptionalAction, default=True,
                   help="voice: record each test, stereo .ogg in recordings/ next to the database (default: on)")
    p.add_argument("--token", help="for an agent saved without a token source; runs one test (env: LIVETEST_TOKEN, "
                                   "server URL from LIVEKIT_URL)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p = cmd(actions, "list", cmd_run_list, "list runs, or one run's tests",
            "livetest run list", "livetest run list --run latest", "livetest run list --run 17 --json")
    p.add_argument("--run", help="a run's id, or latest: list its tests and verdicts")
    p.add_argument("--json", action="store_true", help="print JSON for scripts")
    p = cmd(actions, "logs", cmd_run_logs, "print a run's timeline; follows while it's live",
            "livetest run logs --run latest", "livetest run logs --run 17 --test 4 --level WARNING",
            "livetest run logs --run 17 --json")
    p.add_argument("--run", help="a run's id, or latest")
    p.add_argument("--test", type=int, metavar="N", help="only this test")
    p.add_argument("--level", choices=LEVELS, default="INFO", type=str.upper,
                   help="hide log lines below this (default: INFO)")
    p.add_argument("--json", action="store_true", help="one JSON object per line, for scripts")
    p = cmd(actions, "stop", cmd_run_stop, "stop a run, or one of its tests",
            "livetest run stop --run latest", "livetest run stop --run 17 --test 4 -y",
            description="Stop a run or one test. A running test ends cleanly and keeps what it recorded (no pass or\n"
                        "fail); queued tests never start. Asks first at a terminal; scripts pass -y.")
    p.add_argument("--run", help="a run's id, or latest")
    p.add_argument("--test", type=int, metavar="N", help="only this test")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    for stream in (sys.stdout, sys.stderr):  # an agent's text can be any character; a cp1252 console shows ?, not a crash
        stream.reconfigure(errors="replace")
    args = parser.parse_args(argv)
    if args.func is None:
        args.help_of.print_help()
        return 0
    logs.setup(args.verbose, errors_file(args))
    try:
        with closing(store.connect(args.db)) as db:
            return args.func(db, args)
    except KeyboardInterrupt:  # Ctrl+C: stop quietly, the shell's usual code
        print(file=sys.stderr)
        return 130
    except (Fail, sqlite3.DatabaseError) as e:
        print(f"error: {e}", file=sys.stderr)
    except ValidationError as e:
        for err in e.errors():
            where = ".".join(map(str, err["loc"]))  # e.g. max_turns, participant.attributes
            print(f"error: {where + ': ' if where else ''}{err['msg'].removeprefix('Value error, ')}", file=sys.stderr)
    except Exception as e:  # a bug: the traceback goes to errors.log, the person gets where to report it
        logging.getLogger("livetest").exception("livetest %s crashed", version("livetest"), extra={"console": False})
        print(f"error: livetest hit a bug ({e or type(e).__name__}). The details are in {errors_file(args)};\n"
              f"please report it there with that file and `livetest --version`: {ISSUES}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
