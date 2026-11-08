"""The CLI (entry point): parse flags, call runtime, print. The UI calls the same runtime functions."""

import argparse
import asyncio
import getpass
import json
import sqlite3
import sys
import uuid
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

from pydantic import ValidationError

from livetest.platform import keys, llm
from livetest.platform.db import DEFAULT_PATH
from livetest.runtime import session, store, tokens
from livetest.runtime.models import Agent, Participant, Plugin, Profile, Scenario, key_var


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
    if not store.rows(db, Agent) and offer("agent", "the one you test", "agent add -h"):
        if code := cmd_agent_add(db, argparse.Namespace(**vars(args), name=None, sandbox=None, endpoint=None, url=None)):
            return code
    if "llm" not in store.active(db) and offer("tester LLM", "the model that plays the user", "plugin add -h"):
        return cmd_plugin_add(db, argparse.Namespace(**vars(args), plugin=None, kind="llm", provider=None,
                                                     base_url=None, model=None))
    return 0


def offer(what: str, why: str, see: str) -> bool:
    """Ask a person whether to add a missing thing now; scripts are told where to look instead."""
    if sys.stdin.isatty():
        return ask(f"No {what} yet ({why}). Add one now? [Y/n] ").lower() in ("", "y", "yes")
    print(f"no {what} yet; see: livetest {see}")
    return False


def cmd_plugin_add(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    name = need(args.plugin, "Plugin name: ", "--plugin")
    url = args.base_url or llm.PRESETS.get(args.provider) or need(
        None, f"Provider ({', '.join(llm.PRESETS)}) or a base URL, e.g.\n"
              "  https://api.together.xyz/v1   any OpenAI-compatible API\n"
              "  http://localhost:8000/v1      a server on your machine (vLLM; Ollama is http://localhost:11434/v1)\n"
              "Provider or base URL: ", "--base-url")
    plugin = Plugin(name=name, kind=args.kind, base_url=llm.PRESETS.get(url, url),
                    model=need(args.model, "Model: ", "--model"))
    free(db, Plugin, name)
    file = env_file(args)
    # ponytail: on Windows getpass reads the console even when stdin is redirected; fine until a CI box hangs on it
    key = getpass.getpass("API key (Enter for none): ").strip() if sys.stdin.isatty() else ""
    try:
        secs = asyncio.run(llm.check(plugin.base_url, plugin.model, key or keys.get(file, key_var(name))))
    except ConnectionError as e:
        print(f"error: {plugin.model} at {plugin.base_url} didn't answer ({e}); check the URL, model and key",
              file=sys.stderr)
        return 3
    print(f"checked: first token in {secs:.1f} s")
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
    if args.json:
        print(json.dumps(found, indent=2))
    elif not found:
        print("none yet; see: livetest plugin add -h")
    for p in [] if args.json else found:
        print(f"{'*' if p['active'] else ' '} {p['name']:<20} {p['kind']:<4} {p['base_url']:<40} {p['model']}  "
              f"key: {'set' if p['key'] else 'none'}")
    return 0


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
        goal=need(args.goal, "Goal (what the tester wants done): ", "--goal"),
        tags=args.tag, persona=pairs(args.persona), max_turns=args.max_turns, max_minutes=args.max_minutes,
        expect=expect,
    )))


def summary(n: int, m) -> str:
    """One `list` line per agent, profile or scenario."""
    if isinstance(m, Scenario):
        return f"{n:>4}  {m.name:<24} {','.join(m.tags):<16} {m.goal}"
    rest = m.model_dump(exclude={"name"}, exclude_none=True, exclude_defaults=True)
    if isinstance(m, Agent):  # at most one connection is set
        return f"{m.name:<24} " + (" ".join(f"{k} {v}" for k, v in rest.items()) or "token at run time")
    return f"{m.name:<24} {json.dumps(rest)}"


def cmd_list(db: sqlite3.Connection, args: argparse.Namespace) -> int:
    found = [(n, m) for n, m in store.rows(db, args.kind) if not getattr(args, "tag", None) or args.tag in m.tags]
    if args.json:
        number = args.kind is Scenario  # scenarios are picked by number in `run start`
        print(json.dumps([({"number": n} if number else {}) | m.model_dump(mode="json") for n, m in found], indent=2))
    elif not found:
        print(f"none yet; see: livetest {label(args.kind)} add -h")
    for n, m in [] if args.json else found:
        print(summary(n, m))
    return 0


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


def cmd(sub, name: str, func, help: str, *examples: str, **kw) -> argparse.ArgumentParser:
    """One command or group. Every help screen ends with examples (CLI standard rule 4); func=None prints help."""
    p = sub.add_parser(name, help=help, description=kw.pop("description", help), **kw,
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog="examples:\n" + "\n".join(f"  {e}" for e in examples))
    p.set_defaults(func=func, help_of=p)
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
  scenario    add, list or remove what the tester tries to get done
  plugin      add, list, remove or pick the LLM the tester thinks with
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

    actions = group(sub, Scenario, "Add, list or remove scenarios: what the tester tries to get done.",
                    "livetest scenario add --scenario reschedule --goal 'Move my Tuesday slot to Friday'",
                    "livetest scenario list --tag booking", "livetest scenario remove --scenario reschedule")
    p = cmd(actions, "add", cmd_scenario_add, "add a scenario",
            "livetest scenario add --scenario reschedule --goal 'Move my Tuesday slot to Friday'",
            "livetest scenario add --scenario angry --goal 'Get a refund' --tag billing --persona mood=angry "
            "--tool-called issue_refund amount=40 --tool-not-called cancel_account --no-tool-errors --p95-ms 1500",
            description="Add a scenario. Values after = are read as JSON when they parse (42, true), else as text.\n"
                        "Each scenario gets a number, never reused, for run start --scenario.")
    p.add_argument("--scenario", help="its name")
    p.add_argument("--goal", help="what the tester is trying to get done")
    p.add_argument("--tag", action="append", help="repeatable; groups scenarios for scenario list --tag")
    p.add_argument("--persona", action="append", metavar="KEY=VALUE", help="how the tester behaves, repeatable")
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

    actions = group(sub, Plugin, "Add, list, remove or pick plugins: the LLM the tester thinks with. One per kind is active.",
                    "livetest plugin add --plugin local-qwen --base-url http://localhost:8000/v1 --model Qwen/Qwen3.5-2B",
                    "livetest plugin use --plugin my-groq", "livetest plugin list")
    p = cmd(actions, "add", cmd_plugin_add, "add a plugin; checks that it answers first",
            "livetest plugin add                      # asks for everything",
            "livetest plugin add --plugin local-qwen --base-url http://localhost:8000/v1 --model Qwen/Qwen3.5-2B",
            "livetest plugin add --plugin my-groq --provider groq --model openai/gpt-oss-120b",
            "livetest plugin add --plugin together --base-url https://api.together.xyz/v1 --model meta-llama/Llama-3.3-70B-Instruct-Turbo",
            description="Add a plugin: any OpenAI-compatible server. At a terminal it asks for the API key (hidden) and\n"
                        "saves it in .env next to the database, never in it; scripts set LIVETEST_KEY_<PLUGIN> instead.\n"
                        "Makes one streamed call first and saves nothing if that fails. The first of its kind is active.")
    p.add_argument("--plugin", help="its name")
    p.add_argument("--kind", choices=["llm"], default="llm", help="what it does (default: llm; stt and tts come later)")
    p.add_argument("--provider", choices=llm.PRESETS, help="fills in --base-url for a cloud provider")
    p.add_argument("--base-url", metavar="URL", help="any OpenAI-compatible URL: an API (https://api.together.xyz/v1) "
                                                     "or your own server (http://localhost:8000/v1)")
    p.add_argument("--model", help="the model's name on that server")
    list_and_remove(actions, Plugin)
    actions.choices["list"].set_defaults(func=cmd_plugin_list)
    p = cmd(actions, "use", cmd_plugin_use, "make a plugin the active one of its kind",
            "livetest plugin use --plugin my-groq")
    p.add_argument("--plugin", help="its name")

    args = parser.parse_args(argv)
    if args.func is None:
        args.help_of.print_help()
        return 0
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
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
