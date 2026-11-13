# livetest: product plan and tracker

**What this file is:** what we're building, why, and how far along it is.
- It's the product-level companion to `livekit_tester_research.md`, which holds the technical findings: what a token can reach and how.
- Update the status boxes as work lands.
- Status key: `[x]` built · `[~]` proven in a `scratchpad/` probe, not in the tool yet · `[ ]` not started.

## Goal

Anyone building with LiveKit can test their agent end to end without testing by hand.
- **Input:** only a token, or a way to get one.
- **No agent changes:** it never touches the agent's code and doesn't need the agent's telemetry.
- **Any host:** works on LiveKit Cloud and on self-hosted servers.

## Positioning

**Black-box end-to-end tests for any LiveKit agent, from just a token.**

What makes it different: through the session host it reads the agent's internals:
- tool calls with timing
- the system prompt and session options
- what the agent's own STT heard
- end-of-turn decisions

That lets the judge do real root-cause analysis instead of only saying "the transcript looks wrong".

### Existing tools (searched 2026-11-05/06)

| Tool | Limits compared with livetest |
|---|---|
| [LiveKit Agent Simulations](https://docs.livekit.io/testing/simulations/) (`lk agent simulate`) | Needs a LiveKit Cloud project and the agent's source; Python agents only; docs say beta, [blog](https://livekit.com/blog/testing-voice-agents-with-agent-simulations) says GA |
| [LiveKit unit tests](https://docs.livekit.io/testing/unit-tests/) | Imports the agent's code; one turn at a time, no judge |
| [Hamming](https://hamming.ai/resources/voice-agent-test-personas-support-calls-template), [Coval](https://www.coval.ai/products/simulation), [Cekura](https://www.cekura.ai/discover/voice-agent-testing-platforms), [Vapi Simulations](https://vapi.ai/blog/vapi-simulations) | Paid SaaS, mostly built around phone calls and audio; scores audio and transcript, no agent internals |
| quangdang46/livekit_agent_simulator, deepeval connector, Future AGI | Need API key and secret or run the agent themselves; see the research file |

Ideas worth borrowing:
- promptfoo uses a CLI for runs plus a [local web viewer](https://promptfoo.dev/docs/usage/web-ui/) for results.

## Decisions so far

- **Distribution:** a standalone tool installed from the GitHub README (sh / iex / winget), not a pip package. Whether it needs docker/podman to run anywhere is deferred.
- **Interface:** CLI and a local web UI (`livetest ui`), equal front ends over the same runtime. No hosted SaaS for now.
- **Every command takes `-h` / `--help`**, and each help screen ends with examples.
- **No project files: everything lives in one SQLite DB per user** (`~/.livetest/livetest.db`, created on first use). Users (developers, QA, non-coders) add agents, profiles and scenarios through the CLI or the UI, and both call the same `livetest/db.py` functions.
- **Agents, profiles and scenarios are pydantic models**, stored as JSON in the DB. Their JSON Schemas (`model_json_schema()`) can drive the UI's forms. Not YAML.
- **Profiles (room metadata, participant metadata and attributes, dispatch metadata) are a must-have;** a run picks one.
- **Stack:** Python 3.13 + uv (see "Tech stack").
- **Results go in SQLite**, and the UI reads them from there, recordings included (proposed; see "Results, logs and the UI").

## How a user would use it (proposed)

### Install

```
curl -LsSf https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.sh | sh     # macOS / Linux
irm https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.ps1 | iex          # Windows
```

- Both scripts install uv if it's missing, then run `uv tool install git+https://github.com/Bhargav-CS/livetest@<tag>`, then `livetest init`, which prints the next steps: the web UI (`livetest ui show`) or the terminal commands.
- uv downloads its own Python 3.13, so the user needs neither Python nor pip.
- winget needs a real installer or binary (see "Tech stack"), so it comes later.

### CLI standard

**Locked 2026-11-08.** `-h` is the documentation until real docs exist, so every command follows the same rules and the next one is guessable. New commands must follow this section; change the section first, with the user's agreement, if a rule has to change.

**The model:** agents, scenarios and profiles are independent of each other. A run is the only place they meet:

```
agent    (what to test, how to connect)  ─┐
scenario (what the user wants)            ├─→  run start --name my-agent --scenario 1-20x10 --profile pro-user
profile  (who the user is)                ─┘
```

**Command tree** (✓ = built)

```
livetest                                   overview, grouped by purpose, plus "start here"          ✓
livetest init                              set up the DB, then say what's next (the install runs it)  ✓
livetest doctor                            check the setup, fix common problems
livetest uninstall                         (later)

livetest agent    add | list | remove                               --name <agent_name>        ✓
livetest scenario add | list | remove | generate     --scenario <scenario>   [--tag T]
livetest profile  add | list | remove                --profile <profile>
livetest plugin   add | list | remove | use          --plugin <plugin>   --kind llm|stt|tts  ✓

livetest run      start | list | stop | logs         --name <agent_name> --scenario <selection> [--profile P]
livetest ui       show | serve [--port] | stop                                                    ✓
livetest report   create                             --run <run|latest> [--test N] [--output FILE]
```

**Rules**

1. **Shape:** things you manage are `livetest <group> <action> [flags]`; one-shot actions are `livetest <verb>`.
2. **Same actions everywhere:** `add`, `list`, `remove` (alias `rm`). No `show` (`list` covers it) and no `edit` (remove and add again, or change the DB; the UI edits). Groups add their own only where needed: `scenario generate`, `plugin use`, the `run` actions, `report create`.
3. **Names are flags, never positional. `--name` always means the agent under test** (its LiveKit `agent_name`, as used in explicit dispatch), and appears only where an agent is involved: `agent …` and `run …`. Other things are named by a flag of their own, with the same meaning in every command: `--profile`, `--scenario`, `--plugin`, `--run` (a run's id, or `latest`; on `run start` it sets the new run's id), `--test` (one conversation in a run: one scenario × one repeat).
4. **Help at every level, never an error:** a command or group typed without its action prints its help, exit 0, without touching the DB. Every help screen ends with copy-paste examples. Root help groups commands by purpose (`COMMANDS` in `cli.py`).
5. **Prompt at a terminal, fail clearly in a script:** missing values are asked for at a terminal. In a script they fail and name the flag that fixes it. Destructive actions ask "are you sure?" at a terminal; scripts must pass `-y`.
6. **Output:** plain text for people. `--json` on `list` and `run` for scripts, CI and AI tools (Claude will prefer the CLI). Errors go to stderr, start with `error:` and say what to do next. No tracebacks unless `-v`: they always go to `~/.livetest/errors.log` (next to the DB, rotated at 1 MB), and the error message says where it is and where to report it (GitHub issues, with `livetest --version`).
7. **Exit codes:** `0` ok · `1` a test failed (or errored) · `2` bad input or config · `3` couldn't connect or the agent never joined (incl. no session host). A run with mixed outcomes exits with the first that applies: 3, then 1, then 0; stopped tests count for neither. Ctrl+C: 130 (in `run start` it stops the run first).
8. **Global flags:** `--db <file>` (default `~/.livetest/livetest.db`), `-v` (debug logs on the console), `--version`.

**`run`**

```
livetest run start --name my-agent --scenario 1-20x10 25-35x2 [--run ID] [--profile P] [--parallel N] [--llm NAME]
                   [--voice [--stt NAME] [--tts NAME] [--no-recordings]] [--start-conversation TEXT] [--token T] [-y]
livetest run list [--run <run>]                  # active and past runs: id, agent, progress, pass rate; with --run, its tests and verdicts
livetest run logs --run <run> [--test N] [--level L]  # one timeline: turns, tool calls, states, log lines; follows while the run is live
livetest run stop --run <run> [--test N] [-y]
```

- **A raw token runs one test** (it fixes the room): a bigger selection fails with the commands that save a token source for the agent (`agent rm`, then `agent add --sandbox | --endpoint | --url`).
- **Stop** goes through the DB: `run stop` marks running tests `stopping` and queued ones `stopped`; each running test checks its row every second and ends cleanly, keeping what it recorded. The UI stops the same way, and so does Ctrl+C in `run start`. In `run logs` Ctrl+C only stops following.
- **Later: `run start --detach`** (user, 2026-11-12): hands the run to a background livetest process and returns at once; `run logs` / `run list` / `run stop` work on it as they do now. Foreground stays the default, since CI needs the exit code. Runs started from the UI (module 11) already live in its server process.
- **A run's status comes from its tests:** running while any is queued or running, then stopping, stopped (any stopped) or done.
- **The agent must start each reply within 10 s** (`checks.REPLY_S`). Provider errors (4xx / 5xx) it retries past don't fail a test; they're counted per test (agent errors). A turn with no reply in time ends the test (`no_reply`) and fails it (user, 2026-11-13).
- **Who opens** (user, 2026-11-12): the agent, unless `--start-conversation TEXT`, which the tester says exactly as written. The tester replies to a greeting already in the agent's history, or (in voice) hears a live one out first. An agent that doesn't greet within 10 s ends the test `no_greeting` and fails it. livetest never writes a tester line itself.
- **`run logs` labels the conversation** `tester:`, `agent_<name>:` and `agent_<name>_heard:` (what the agent took in: an echo in text mode, its STT in voice). Voice mode adds `tester_heard:` (the tester's STT), so both sides' word errors show.

- `run start` needs `--name` and `--scenario`, so nothing runs by accident. `--scenario all` must be typed.
- **Selection** (`--scenario` takes one or more items): each scenario has a global number (shown by `scenario list`, never reused after a remove). An item is `7`, `1-20`, a scenario name, or `all`; `xN` repeats it. Overlaps add up; ranges skip removed numbers.
- **Recording is on by default** (`--recordings` / `--no-recordings`, argparse `BooleanOptionalAction`): one file per test in `~/.livetest/recordings/run-<n>/`. Text mode has no audio, so there's nothing to record.
- **Before starting**, it prints the plan and asks to continue; `-y` skips that:
  ```
  run id: nightly-1
  my-agent: 31 scenarios, 222 tests, voice mode, 4 in parallel, profile pro-user
  recording: on (~/.livetest/recordings/run-nightly-1/, one file per test)
  continue? [y/N]
  ```
- **Run id:** `run start --run <id>` sets it; without it, livetest picks the next free number (`17`, `18`, …). `latest` always works.
  - Allowed: letters, digits, `-`, `_`, `.`, up to 64 characters, unique. It ends up in folder and file names and UI links, so anything else fails at once with a fix (`error: run id 'nightly 1' has a space; use nightly-1`).
  - Stored as `TEXT UNIQUE`: lookups take microseconds whatever the format, so the format is chosen for people, not speed.
- **Live view:** every turn, tool call, state change and log line is written to the DB as it happens (WAL), so `run logs` and the UI read the same rows. The recording isn't live: it arrives when each test ends (v0.1). `run start` prints only each test's verdict, at the end; `run logs` or the UI follows the conversations.

**`report`**

```
livetest report create --run 17              # → ./run-17_report.html          the whole run
livetest report create --run 17 --test 4     # → ./run-17_test-4_report.html   one conversation
livetest report create --run latest --output ~/Desktop/nightly.html
```

- Its own group, not under `run`: a report reads a finished run and runs nothing. Later actions go here, e.g. `report gate --rules <file>`, a CI gate whose thresholds each org defines.
- **One self-contained HTML file** (works offline, no CDN), written to the current directory unless `--output` says otherwise. It holds everything livetest collected:
  - summary: agent, profile, mode, pass rate, latency, usage (tokens incl. cached, audio seconds, characters)
  - per test: transcript with tool calls inline, checks, judge verdict and root cause, per-turn metrics, trace timeline, logs, recording with a player synced to the transcript
  - what the agent showed: prompt, tools, session options; the profile asked for vs what the agent saw
- **Recordings are embedded** whenever the run recorded them. A big voice run gives a big file; that's local storage the user controls.
- **Same page as the UI's run page:** the UI fills it from the live API, `report create` saves it with the data baked in. One front end, not two.
- A report contains the agent's system prompt and recordings; sharing it shares those. Keys are never in it.

**Connecting**
- How to get a token is saved on the agent (`agent add --sandbox | --endpoint | --url`). An agent saved without one takes `--token` on `run start` (env `LIVETEST_TOKEN`); the token is never saved, and an expired one fails with "pass a new one with --token".
- **`agent add` proves the agent works (decided 2026-11-08, no `agent probe`):** it gets a token, joins a fresh room, waits up to 30 s for the agent and saves only if it joined (else exit 3). One short agent session; with the API key the room is deleted after, which frees the job slot. A broken setup that says "added" costs more than that session.
- `--url` mode makes tokens with the agent's own API key and secret (per agent: dev and test projects differ), asked by `agent add` and saved in `.env` as `LIVETEST_AGENT_KEY_<NAME>` / `LIVETEST_AGENT_SECRET_<NAME>`. SIP-only agents need `--kind sip`, which only the API-key mode or the user's own endpoint can set.
- **Precedence:** flag > env var > what's saved in livetest.
- **Plugins (decided 2026-11-08):** the tester's LLM / STT / TTS. Users add as many as they like (`plugin add`, or the UI's Plugins tab) and set one active per kind (`plugin use`). All are OpenAI-compatible for now (`--provider openai|groq|gemini` presets the base URL; none means a local server, e.g. vLLM, Ollama). Every LLM goes through `livekit.plugins.openai.LLM`, which always streams. `plugin add` makes one streamed call first and saves nothing if it fails. Native provider plugins (e.g. Anthropic) later if needed. A run will take `--llm NAME` / `LIVETEST_LLM` to override the active one (module 7).
- **Keys (plugins, API secret):** asked by `plugin add` (hidden input) and `init`, and saved in `~/.livetest/.env` (next to the DB) as `LIVETEST_KEY_<PLUGIN>`, readable only by the user. Real env vars override it, for CI. Keys never go in the DB or in results. The OS keychain (`keyring`) is the upgrade if users ask.

### Where data lives

```
~/.livetest/livetest.db  # agents, profiles, scenarios, runs, results (one per user, created on first use)
~/.livetest/recordings/  # .ogg files the DB points at
```

- Sharing with a teammate or CI: `livetest export` / `livetest import` of one JSON file (later).
- The JSON examples below show the model shapes, as stored in the DB and accepted by the CLI/UI.

### Scenario format (proposed)

The model shape, as stored in the DB and accepted by the CLI and UI. The UI's forms and AI tools can work from its JSON Schema.

```json
{
  "name": "reschedule-angry-customer",
  "tags": ["booking"],
  "prompt": "You are an impatient customer (en-IN) who interrupts. Move your Tuesday appointment to Friday and refuse the first slot offered. Reply with one short turn.",
  "max_turns": 12,
  "max_minutes": 8,
  "expect": [
    { "type": "tool_called", "name": "reschedule", "args": { "day": "friday" } },
    { "type": "tool_not_called", "name": "cancel_booking" },
    { "type": "latency", "p99_ms": 1500 },
    { "type": "no_tool_errors" }
  ]
}
```

- **`prompt` is the tester LLM's whole system prompt, sent as written** (user, 2026-11-12): who it plays, what it wants and how it talks. livetest adds nothing, so the user has full control; the LLM sees only it and the agent's words.

### Tests, checks and limits (decided 2026-11-08)

- **A test is judged only on the checks its scenario sets; livetest adds none of its own** (user, 2026-11-13). It collects and shows; a test with no failed check passes, unless it ended `no_reply` / `no_greeting`, which fail it.
  - **v0.1:** `tool_called` / `tool_not_called` (with args), `no_tool_errors`, reply latency p95/p99 limits.
  - **Reported, never checked:** the `max_minutes` limit and the agent leaving. A test that never joined or errored is a status, not a check.
  - **Removed from v0.1, planned with the LLM judge in v0.2:** the built-in loops (repeated replies) and finished-in-time checks. The 10 s reply rule stays, as `no_reply`.
  - **No LLM judge in v0.1.** Most users will drive livetest from Claude, which can judge for them. A judge module may come later.
- **Tool checks show what happened, not just pass/fail:** the expected call and the actual call side by side (name, args, output), so the tester sees "right tool, wrong args" directly, on passes as well as failures.
- **How a test ends:** `max_turns`, `max_minutes`, the agent ending the session, or the user pressing **Stop** (on one test or the whole run). Stop ends the conversation cleanly and keeps everything recorded so far; the result is marked **"stopped by user"**, with no pass or fail.
- **Tester's LLM / STT / TTS:** any OpenAI-compatible endpoint, with the user's own keys. Locally hosted models come in v0.2.
- **Mode:** text is the default; voice is an option in v0.1.
- **Usage, not cost (user, 2026-11-12):** the agent's session usage names each model and provider, with LLM tokens (incl. cached), STT audio seconds and TTS characters (see the research doc); the tester's LLM reports its tokens too. LiveTest records and shows both, and prices nothing: with any OpenAI-compatible model allowed, a built-in price table can't keep up, so users work out cost themselves. Cost (a price table, user-set prices) is for a later version.

- A scenario belongs to no agent and has no profile: the run picks both. It has a global number for `run start` selections; `tags` group scenarios when several agents are tested (`scenario list --tag booking`).
- `expect` entries carry a `type` field (a tagged union), so the schema can say exactly which fields each check takes. They're cheap, deterministic, and run before the judge.
- **How the schemas are made:** pydantic models validate what's saved and produce the schemas with `model_json_schema()`, so there's no second schema to keep in sync and no `jsonschema` dependency.

### Profiles: metadata and attributes (must-have)

Many agents branch on `ctx.room.metadata`, `participant.metadata`, `participant.attributes` or `ctx.job.metadata`. A **profile** is who the user is, as the agent sees them. A run has to be able to set it, and has to record what the agent actually saw.

- **Named "profile", not "session",** because LiveKit already uses "session" for the agent's `AgentSession`.
- **Profiles are global,** not tied to an agent: a profile describes a user, so any scenario of any agent can use it.
- **Picked at run time:** `run start --profile X` applies to the whole run, so the same scenarios can run as a different user (e.g. free vs pro). A run without a profile joins as a plain participant with identity `tester`.
- **Later, if mixing is needed in one run:** the selection can carry a profile, `1-20@pro-user 21-25@free-user`, like `x10` carries the repeat count.

```json
{
  "name": "pro-user",
  "room":        { "metadata": { "tenant": "acme" } },
  "participant": { "identity": "tester", "name": "Priya", "metadata": { "user_id": 42 }, "attributes": { "plan": "pro", "lang": "en" } },
  "dispatch":    { "metadata": { "job": 1 } }
}
```

- **Value types:** a `metadata` value can be a JSON object (livetest serializes it) or a plain string, because LiveKit metadata is a string. `attributes` values must be strings; the model enforces this.
- **The dispatch's `agent_name`** comes from the agent (`--name`), not the profile.
- **Where each field lands:**

  | Field | Token field | Agent reads it as |
  |---|---|---|
  | `room.metadata` | `roomConfig.metadata` | `ctx.room.metadata` (only applies when the room is created; livetest always uses a fresh room) |
  | `participant.metadata` / `attributes` | `metadata` / `attributes` | `participant.metadata` / `participant.attributes` |
  | `dispatch.metadata` | `roomConfig.agents[].metadata` | `ctx.job.metadata` |

- **What each connection mode can set:**
  - **API key:** everything.
  - **Sandbox endpoint:** everything except `kind` (verified 2026-11-06).
  - **Custom endpoint:** whatever it accepts. The token-endpoint standard says to return 4xx for fields it won't set, and livetest reports that as "your endpoint rejected X".
  - **Raw token:** fixed by the token. livetest decodes the token, shows what's in it, and fails a run whose profile asks for something different.
- **Changing attributes mid-run** (frontends do this as the user navigates). Proposed for later:
  - a scenario step `{ "at_turn": 3, "set_attributes": { "screen": "calendar" } }`
  - it needs the token grant `canUpdateOwnMetadata`
  - without the grant, LiveKit drops the update silently, so livetest must read the attributes back and fail loudly
- **Recorded in every result:**
  - the profile that was asked for
  - what the room actually had after joining: room metadata, the tester's metadata and attributes
  - the agent's published attributes

## Features and status

### Core (the user's five asks)

1. **End-to-end tests, with or without voice**
   - [x] Join as a standard participant with the dispatch inside the token, from a sandbox, endpoint, API key or raw token (`livetest/runtime/tokens.py`, `session.py`; live-tested on Cloud, self-hosted proven by probes)
   - [x] Voice mode: the tester's own STT, LLM and TTS session in the room (`runtime/runner.py` `_voice`), the session host still reporting; passes on the self-hosted voice stub and ran on Cloud (ALIVE); `--start-conversation TEXT` or the agent opens (module 9)
   - [x] Text mode through the session host (`RemoteSession`: `update_io` plus `run`), 0.3 to 3.4 s per turn; the tester LLM plays the scenario's prompt; ends on `max_turns` / `max_minutes` / the agent leaving (`livetest/runtime/runner.py`)
   - Session host required, no `lk.chat` fallback: almost every metric comes from it (user, 2026-11-08). Without it the result is `no_session_host`
2. **Bring your own keys**
   - [x] Tester STT / TTS through any OpenAI-compatible `base_url` (`platform/stt.py`, `tts.py`, `plugin add --kind stt|tts`); the STT must stream (realtime websocket); tested with private-ai's Moonshine and Kokoro-7M
   - [x] Tester LLM through any OpenAI-compatible `base_url` (vLLM, Ollama, private-ai, cloud providers), always streamed; many plugins, one active per kind (`livetest/platform/llm.py`, `plugin add|list|rm|use`)
   - [x] Keys asked by `plugin add` / `init` (or the UI's Plugins page), saved in `~/.livetest/.env`, env vars override; never in the DB (`livetest/platform/keys.py`)
3. **Logs, traces and recordings for every test**
   - [x] Session-host events: tool calls, state changes, usage, metrics, what the agent's STT heard; plus the agent's prompt, tools, session options and chat history, all written to `events` as they arrive (`runtime/runner.py`)
   - [x] Usage per test, counts only (no cost in v0.1): the agent's models from its last `session_usage_updated` (cumulative), the tester's from one `tester_usage` event at the end (LiveKit's `ModelUsageCollector` on its LLM, and STT / TTS in voice; LiveKit counts 0 tokens for a cancelled LLM call, e.g. a preemptive or interrupted one, so both sides under-count what the provider bills); saved as `results.usage` for every status, LLM tokens on each verdict line and the run total, everything in `--json` (`runtime/checks.py` `usage`, module 8)
   - [~] Tool calls rebuilt as tester-side OTel spans with real durations
   - [x] Local stereo `.ogg` (Opus) recording per voice test, channel 0 = agent, channel 1 = tester (`RecorderIO` wired by hand in `_voice`); browsers play it, so the UI and report are the players
   - [~] Grafana panel playing the recording next to traces
   - [x] Every run stored in `~/.livetest/livetest.db` (SQLite), recordings next to it (see "Results, logs and the UI"): `runs` + `results` + `events`, recordings as paths on `results`
   - [x] Log lines as `events` rows, tagged with their test through a `contextvar` (parallel-safe); console logging in `platform/logging.py`, `-v` global; tracebacks in `~/.livetest/errors.log` for bug reports (module 7)
   - [x] `livetest run start|list|logs|stop`: selection grammar, run ids, confirmation, `--parallel`, Stop (DB statuses), `--json`, exit codes, 10 s reply rule (`cli.py`, `runtime/runner.py`, `runtime/store.py`)
   - [x] `livetest report create`: one self-contained HTML per run or per test, recordings and fonts embedded (`--no-recordings` leaves them out); LiveKit's per-turn metrics as reported, nothing placed by guesswork (`ui/report.py`, `ui/static/run.html`, module 10; the test view becomes the C design in module 11)
   - [ ] `livetest report gate`: user-defined CI rules (later)
   - [ ] Optional OTLP export (Grafana, Langfuse, etc.); not required for a first run
4. **LLM as judge**
   - [ ] One OpenAI-compatible client (Claude, Gemini and OpenAI all expose one)
   - [ ] Custom HTTP judge: POST the run bundle, get back a verdict JSON
   - [ ] What the judge sees: transcript, tool calls, system prompt, session options, metrics, STT-heard text
   - [ ] What it returns: verdict, score per rubric item, description, root cause tied to a turn / prompt line / tool, fixes
     - Fix types: a prompt diff, a tool-schema change, or a session option such as `min_endpointing_delay`
5. **Scenarios that emulate the agent's users**
   - [x] DB at `~/.livetest/livetest.db`, created on first use (`livetest/platform/db.py`, tables in `livetest/runtime/store.py`)
   - [x] `livetest init`: sets up the DB and prints the next steps, `livetest ui show` or the terminal commands for what's still missing; asks nothing, starts nothing, the same everywhere (the install scripts run it)
   - [x] `livetest agent add/list/rm`: the agent's LiveKit `agent_name` plus how to get a token; one prompt picks sandbox / endpoint / URL; `add` joins and saves only if the agent came; per-agent keys in `.env` (`livetest/cli.py`)
   - [x] `livetest profile …` / `livetest scenario add|list|rm` in the DB, flags only; scenario numbers never reused; `PRAGMA user_version` schema steps (`runtime/store.py`, `platform/db.py`)
   - [x] Scenario model: prompt, tags, `max_turns`, `max_minutes` (default 5), `expect` (tool_called / tool_not_called / no_tool_errors / latency) (`livetest/runtime/models.py`); evaluated by `livetest/runtime/checks.py` (module 6)
   - [x] Deterministic checks (`livetest/runtime/checks.py`): the scenario's own only (the built-ins loops / finished in time were removed 2026-11-13, for v0.2 with the judge); tool called / not called (args as a subset, exact values), no tool errors, reply latency p95/p99 (`checks.replies`, tester clock, nearest rank; voice: the tester stops speaking → its VAD hears the agent; text: line sent → `AS_SPEAKING`; cut-off turns skipped, unanswered ones fail). Outcomes and verdict on the result row (`passed`, `checks`). Never joined stays a status; checks run only on `done`
   - [ ] More checks, from user feedback: text contains / regex, token budget
   - [x] Profiles applied to every token: room metadata, participant metadata/attributes, dispatch metadata; tokens livetest didn't make are checked against the profile (`livetest/runtime/tokens.py`)
   - [x] Reusable profiles in the DB; what the agent actually saw recorded in each result (`seen` event: room metadata, the tester's metadata and attributes, the agent's attributes)

6. **CLI usability**
   - [x] CLI standard (see "CLI standard"): grouped root help, help on bare groups, `remove`/`rm`, confirmation with `-y`, `--name` flags, `--json` on `list` and `run logs`, global `-v`, exit codes
   - [ ] `livetest doctor`: finds and fixes common user issues (missing keys, bad token source, server unreachable), so users never debug at code level

### Extras, ranked by value

- [ ] **Repeat N times and report a pass rate:** LLM agents are nondeterministic.
- [ ] **Baseline diff:** pass rate, latency and cost before and after a prompt change.
- [ ] **Agent STT accuracy:** the tester knows what it said, so compare against `user_input_transcribed` for a word error rate (example: "Bargov Pottky" for "Bhargav Patki").
- [ ] **Replay a failure:** re-run with the same user turns to check a fix.
- [ ] **`livetest generate`:** draft scenarios from the probed prompt and tools (happy path, edge cases, adversarial).
- [ ] **MCP server:** Claude Code or Cursor can run tests, read the root-cause analysis and fix the prompt in a loop.
- [ ] **Security pack:**
  - attack scenarios: prompt extraction, injection, PII leaks
  - a warning when the session host is open (any participant can read the prompt and inject turns)
- [ ] **Voice perturbations:** background noise, timed barge-ins, silences that hit `user_away_timeout`, speaking rate, accents.
- [ ] **Import production transcripts as scenarios.**
- [ ] **Cost estimate before a run.**
- [ ] **Concurrency and load tests** (`lk perf` partly covers this).
- [ ] **Multi-agent agents** (handoffs, a supervisor running `AgentTask`s, task groups; [LiveKit workflows](https://docs.livekit.io/agents/build/workflows/)). They all run in one `AgentSession`, so the tester's single session-host connection already sees them. Every switch arrives live as an `agent_handoff` item (`old_agent_id → new_agent_id`), and livetest records it today. What's missing: `agent_info` is asked once at join (the first agent's prompt and tools only), and `agent_config_update` items (each agent's instructions, tools added / removed) aren't sent live; they appear only in the chat history, also read once at join. Estimated ~1 day (user, 2026-11-13: later):
  - runner: after each `agent_handoff`, ask for `agent_info` and `session_state` again; at the end, ask for `chat_history` again; the stub agent gets a tool that hands off, for a self-hosted test
  - page: the agent active at each `agent_turn` and tool call (from the handoff times), an `agent_handoff old → new` row in Traces, and the Agent tab listing each agent's prompt, tools and config changes
  - not planned unless asked: one connection per agent participant (a supervisor that's a separate participant in the room, 2–3 days); "handed off to X" / "tool called by agent X" checks (the user decides what fails)
  - subagent delegation inside a tool stays a black box: only the tool's arguments, output and duration are seen
- [ ] **More collected data, smallest first** (found 2026-11-13; the session host and room offer them, livetest doesn't save them yet):
  - join and connect times per test (token, connect, agent joined); `agent add` already measures them (~1 h)
  - WebRTC stats: the agent's through the session host's `get_rtc_stats` request (no client helper in 1.7.1), the tester's from `room.get_rtc_stats()` (~3 h)
  - the agent's `lk.transcription` stream with segment timings (~½ day)
  - overlap and silence measured from the stereo recording (1–2 days, audio analysis)

## Tech stack (searched 2026-11-06)

**Python 3.13 + uv is enough.** No other tool gives a real speed-up:
- **Where a test's time goes:** waiting on the agent, the LLMs and the network.
- **Media is already native:** livekit `rtc` media runs in Rust (livekit-ffi) under the Python API.
- **The session-host client and the STT/TTS plugins livetest needs are in livekit-agents.** agents-js also has a `RemoteSession`, which suggests Node agents host the session too (untested). Rust and Go SDKs would mean rewriting both.

| Need | Choice | Why |
|---|---|---|
| Language / runtime | Python 3.13 | `RemoteSession`, `rtc`, plugins |
| Dev, lockfile, install | uv | already used; `uv tool install` gives users an isolated tool with its own Python |
| CLI | argparse (stdlib) | `-h` on every subcommand for free |
| Scenario models + JSON Schema | pydantic (installed) | validation and schema from one model |
| Tester LLM + judge | `livekit.plugins.openai.LLM` (installed) with `base_url` | always streams; covers any OpenAI-compatible endpoint (vLLM, Ollama, private-ai, Groq, Gemini, OpenAI); the same object drops into voice mode's `AgentSession` |
| Results store | sqlite3 (stdlib, SQLite 3.50) | one file, JSON functions built in, WAL so the UI reads while a run writes |
| UI server | aiohttp.web (installed) | its `FileResponse` handles HTTP Range, which browsers need to seek in audio; stdlib `http.server` doesn't |
| Viewer front end | one HTML page, vanilla JS, no build step | nothing to bundle, so it ships as package data |
| Telemetry | OpenTelemetry (installed), off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set | optional export only |

Rejected:
- **uvloop:** no Windows support.
- **FastAPI / Flask:** new dependencies; aiohttp covers it.
- **typer / click:** installed through livekit-agents, but argparse needs no extra pin.
- **SQLModel** (2026-11-08): table models skip pydantic validation (our `extra="forbid"` and validators), can't map our nested models to columns, and pull in SQLAlchemy. Rows already store each model as JSON, so one generic `put` / `all` helper in `store.py` covers every table.
- **Migrations:** `PRAGMA user_version` plus an ordered list of steps in `store.py`, run on connect, so every released version opens old DBs. Until v0.1 ships there's one step, edited in place (CLAUDE.md, Workflow 7).
- **YAML:** the user prefers JSON.
- **A Rust / Go / Node rewrite:** loses the session-host client and plugins for no speed gain.

**Packaging, later:**
- `uv tool install` covers sh / iex.
- winget and offline installs need a binary: **PyApp** (a small Rust launcher that fetches Python on first run) or **PyInstaller** (fully bundled). Pick one when winget is next.
- **uvx.sh** one-liners need a PyPI package, and `livetest` is taken there.
- **Install size:** onnxruntime (silero VAD) and the voice plugins are heavy, and text mode doesn't need them. Consider `livetest[voice]` as an extra.

## Code structure

Only `livetest/` ships to users (the wheel holds nothing else). The rest of the repo is for building it.

```
livetest/
├── platform/      what livetest needs to run, kept generic: db.py, keys.py, llm.py; stt.py, tts.py, logging.py (empty: where they go)
├── runtime/       what livetest does: models.py, store.py, tokens.py, session.py, setup.py (add / list / remove, shared by CLI and UI); runner, checks
├── integrations/  optional extras, switched on by the entry point: otel.py (empty: where OTLP export to Grafana / Langfuse goes)
├── ui/            entry point: report.py (static/run.html with a run's rows baked in), server.py (aiohttp + JSON API serving run.html and static/app.html, the shell)
└── cli.py         entry point
tests/             pytest (dev-only dependency); test_layers.py enforces the rule below
dev/               contributor-only: livekit-server.yaml (self-hosted LiveKit for the live tests)
docs/              this plan and the research
```

- **Empty placeholder files are kept on purpose:** they mark where a later implementation goes (user, 2026-11-08).
- **Dependencies flow inward:** integrations → runtime → platform. A module imports its own layer or one further in, never further out. Entry points (`cli.py`, `ui/`) wire layers together, and nothing imports them.
- **Platform knows nothing about livetest's data:** `platform/db.py` opens SQLite; `runtime/store.py` owns the tables and queries.
- **CLI and UI are equal:** both call the same runtime functions.

## Results, logs and the UI (proposed)

**One SQLite file per user:** `~/.livetest/livetest.db`. Recordings are files in `~/.livetest/recordings/`; the DB stores their paths, not blobs, so the UI can serve them with Range.

```
runs     (id, agent, profile, mode, version, started_at)   -- status comes from its tests; the agent's SDK is in each test's framework event
results  (id, run, test, scenario, profile, room, status, ended, passed, checks, usage, recording, recording_started_at,
          started_at, finished_at)
events   (result, ts, source, type, data)   -- source agent | tester | log, data is JSON
```

- **Few tables on purpose.** Turns, tool calls, metrics and log lines are all `events` rows (a log line: `source='log'`, its level in `type`; decided 2026-11-08, no `logs` table); the UI and `report` query them with SQLite's `json_extract`.
- **`ts` is the tester's receive time,** so everything lines up with the recording. The agent's own clock is used only for durations (tool calls), since the two machines' clocks can differ.
- **Logs:** a small `logging.Handler` writes livetest's logs, and LiveKit's Rust logs that reach Python logging, into `events` (INFO and up; DEBUG with `-v`). Lines outside a test, or from another thread, stay on the console. The agent's server logs stay out of reach, since it's a black box. What the agent does send (`error` and `debug_message` events) lands in `events`.
- **Agent errors are reported, not checked** (user, 2026-11-09): they never fail a test. The report and the UI show an error count per test and per run among the metrics, and each error's message on the test's timeline (problems lane) next to the turn it hit. First seen on Baymax: its LLM's 429 rate limit, which explained a 3.08 s reply.
  - **Later (v0.2 or v0.3):** consider letting errors fail a test, e.g. an opt-in `no_agent_errors` check. See the roadmap.
- **`livetest report create`** writes one HTML file per run (or per test); see "CLI standard". CI gating comes later as `report gate` with user-defined rules.

**`livetest ui`** (aiohttp, already installed with livekit-agents, bound to `127.0.0.1` only because results hold system prompts and recordings). **Design final 2026-11-08:** [LiveTest UI canvas](https://claude.ai/artifact/Ei4gStAX9gF5u3nvonpPJc). Dark only, styled like LiveKit's dashboard, brand "LiveTest".
- **A, the app shell:**
  - sidebar: TEST (Runs, Scenarios, Profiles), SETUP (Agents, Plugins, Settings)
  - runs as a list or cards, with a date filter and the score only
  - run page: stats, live wall, failed checks and agent errors grouped by check, attempt dots
  - report screen (what's inside, sizes)
  - Plugins: tester LLM, STT and TTS, cloud or local / OpenAI-compatible
  - new-run drawer, first-run checklist
- **C, the test page** (`/runs/<run>/tests/<n>`), the one design for a test, in the app and in the report file (user, 2026-11-12):
  - a fixed top (cards, checks, a sticky player with both sides' state bands) over tabs: Traces · Turns · Transcript · Events · Usage · Agent · Logs, with an Inspector beside them; keyboard (Space, ← →, J / K, /, Esc); links with `?t=&tab=&span=`
  - Traces: LiveKit's span tree with both sides (`tester_turn`: its LLM thinking, TTS speaking, the agent hearing it; `agent_turn`: thinking, llm_node, tts_node, tools, speaking, the reply gap, errors), with the recording playing along it; the voice tester's own states are recorded for it
  - usage strip in ms and tokens incl. cached, with both sides' models
  - checks and problems in time order, as data: livetest draws no conclusions of its own (pass / fail come from the scenario's checks and the 10 s reply rule)
  - transcript that highlights the turn being spoken; inspector with Copy as JSON
  - logs and what the agent showed (prompt, tools, options, profile sent)
  - download this test's report
- **The report file** is the same pages offline: a whole run is the run overview, with each test opening as C; one test is just its C page.
- **Usage is two sets:** the agent's own (its session usage) and LiveTest's to run the test (the tester's models). Shown as counts, no cost (later version).
- **Only what we measure:** no LLM judge (verdicts, root causes, fixes), no run-to-run comparisons, no known-issue tracking until they're designed.
- **Live:** transcript, state, tool calls and usage stream in; the recording arrives when each test ends (v0.1).
- **JSON API** (`ui/server.py`): `/api/runs`; `/api/runs/<run>[?test=N]`, the same rows `report create` bakes in (`report.data`), each recording as `/api/results/<id>/recording` (Range); `POST /api/runs/<run>/stop`; `/api/runs/<run>/report[/sizes][?test=N&recordings=0]`; the setup kinds (`/api/{agents|profiles|scenarios|plugins}`, scenarios with their last score); `/api/plugins/<name>/check`; `/api/settings`; `/fonts/`. Only `Host: 127.0.0.1 | localhost`, and every write only as JSON, so other websites can't read or act on it (DNS rebinding, forms).
- **Built in module 11, to the A board** (`ui/server.py`, `static/app.html`, `run.html` served, `static/fonts/`): Inter and JetBrains Mono ship with livetest (OFL) and go inside reports. Sidebar with Jump to (Ctrl K) and New run; a 4-step Welcome until the first run; Runs grouped by day with a date filter and List / Cards; Scenarios / Profiles / Agents (Add opens a side drawer with what the CLI's flags take, Remove per row, the CLI command below); Plugins as three cards (LLM, STT, TTS: Test connection checks and saves as the active one) over the saved list; a read-only Settings page; the New run drawer (tag filter, last-score chips, the plan built as you pick, extras under More options, greyed out while starting; the run lives in the UI's process); run pages with the sidebar, Copy link, Stop, Re-run failed / Run again (the drawer pre-filled), Report, and a Live now strip; each test as the C page (`/runs/<run>/tests/<n>`, and `#tests/<n>` in a report file); the Report screen (scope, Include recordings = `report create --no-recordings`, each section's measured size, Download). `ui stop` stops the UI's own runs cleanly first. Add and remove go through `runtime/setup.py`, the run through `runner.prepare` / `queue` / `launch`, the same functions as the CLI.
- Still to design: agent detail with what the agent showed (prompt, tools); a richer scenario editor (the form mirrors the CLI's flags) and profile editor; editing in place (rule 2: remove and add again).

### Recordings in the UI

- **Player:** a native `<audio controls>` over the Range-served `.ogg`.
- **Timeline** on one time axis, starting at `recording_started_at`:
  - waveform lanes: left = agent, right = tester, drawn from the stereo channels with WebAudio `decodeAudioData` on a `<canvas>`, no library
  - agent state and user state lanes (listening / thinking / speaking)
  - tool-call bars (name, duration, error in red)
  - transcript turns
- **Sync:** clicking a turn, tool bar or state seeks the audio; playback moves a playhead and highlights the current turn.
- **Under each tester turn:** what the agent's STT heard, with the word errors marked (feeds STT accuracy).
- **Text-mode results:** the same timeline with no audio lanes.
- **Grafana:** the panel stays possible through OTLP export, but it's no longer needed to see a recording.

## v0.1 build order (agreed 2026-11-08)

One module at a time. The loop for each: **plan** (files, functions, CLI surface, the check that proves it; user approves) → **test** (write the check first) → **implement** (ponytail ultra) → **confirm** (user runs or reviews it; tick the boxes above). No code before the user says go.

| # | Status | Module | Where | Proven by | Needs LiveKit? |
|---|---|---|---|---|---|
| 1 | [x] | Profiles and scenarios in the DB: `PRAGMA user_version` migrations, generic JSON-row helper, scenario numbers + tags, `expect` tagged union, `max_minutes`; flags only; `profile` / `scenario add\|list\|rm` | `runtime/models.py`, `runtime/store.py`, `cli.py` | pytest (`tests/test_cli.py`) | no |
| 2 | [x] | Plugins and keys: `plugin add\|list\|remove\|use` (LLM now; STT/TTS in 9), one active per kind, keys → `~/.livetest/.env`; OpenAI-compatible streaming LLM; `init` offers the first one; Groq out of user deps | `platform/{llm,keys}.py`, `runtime/{models,store}.py`, `cli.py` | pytest + one streamed call to private-ai | no |
| 3 | [x] | Tokens: sandbox, endpoint, API key (per-agent keys in `.env`), raw token; profile applied to each; tokens livetest didn't make checked against the profile. `agent add` joins and waits for the agent before saving; `init` offers an agent, then an LLM | `runtime/tokens.py`, `runtime/session.py`, `cli.py` | pytest + deployed Baymax (Cloud) | yes |
| 4 | — | ~~`agent probe`~~: dropped (user, 2026-11-08). Joining moved into `agent add` (module 3); reading prompt / tools / session options moves into the run (module 5) | | | |
| 5 | [x] | One text-mode test: tester LLM drives `RemoteSession`; reads and records the agent's prompt / tools / session options; ends on max_turns / max_minutes / agent ends; results, events, logs written live | `runtime/runner.py`, `runtime/store.py`, `runtime/session.py`, `platform/logging.py` | pytest + self-hosted stub agent (`tests/stub_agent.py`) | yes |
| 6 | [x] | Checks: the scenario's own (tool checks with expected vs actual, latency p95/p99); the built-ins were removed 2026-11-13 (v0.2) | `runtime/checks.py` | pytest on saved event dumps | no |
| 7 | [x] | `run start\|list\|logs\|stop`: selection grammar, run ids, confirmation, `--parallel`, Stop, `--json`, exit codes | `cli.py`, `runtime/runner.py` | pytest + self-hosted | yes |
| 8 | [x] | Usage: the agent's (its session usage) and the tester's (its LLM's tokens), saved on each result; no cost (user, 2026-11-12) | `runtime/checks.py`, `runtime/runner.py`, `runtime/store.py`, `platform/llm.py`, `cli.py` | pytest on saved usage | no |
| 9 | [x] | Voice mode and recording (after the test ends) | `runtime/runner.py` | self-hosted, voice stub | yes |
| 10 | [x] | `report create`: one offline HTML file | `ui/report.py`, `ui/static/` | open the file | no |
| 11 | [ ] | `livetest ui`: aiohttp + JSON API + the A/C page | `ui/server.py`, `ui/static/` | browser | no |
| 12 | [ ] | `livetest doctor` | `cli.py` | pytest | partly |
| 13 | [ ] | Install scripts (sh / iex via `uv tool install`), ending with `livetest init`; upgrading = re-running them, which stops the background UI first | `install.sh`, `install.ps1` | fresh install on Windows | no |

- **Decided before module 5:** no session-host-off fallback (`lk.chat`); the session host is required (user, 2026-11-08).
- **The UI ships in v0.1** (user confirmed before module 11, 2026-11-12).

## Roadmap (proposed)

| Version | Scope |
|---|---|
| v0.1 | See "v0.1 build order": connection modes, `-h` everywhere, `agent add` that joins, scenarios + profiles, text mode (voice optional), recording, deterministic checks, parallel, usage, SQLite results, `report`, UI, `doctor`, exit codes, standalone install. No judge |
| v0.2 | Local tester models, cost (price table and user-set prices, on top of saved usage), STT accuracy, judge (once designed) with reply-time, loop and time-limit checks, multi-agent agents (handoffs, tasks: see Extras); consider: agent errors failing a test (or v0.3) |
| v0.3 | `generate`, repeat-N, `diff`, JUnit and GitHub Action, MCP server |
| v0.4 | Perturbations, security pack, concurrency, SIP |

## Risks and open questions

- **The session host is an internal module** (`livekit.agents.voice.remote_session`).
  - Pin the supported livekit-agents versions.
  - An agent with `session_host=False` gets `no_session_host`; there's no fallback (decided 2026-11-08).
- **Live view must be verified before the UI commits to it** (user, 2026-11-08):
  - expected to work live: transcript, tool calls, agent/user state, usage (session-host events arrive as they happen; tool spans are rebuilt from them)
  - not proven: playing a recording while the test runs. The tester-side `RecorderIO` writes the `.ogg` and finalizes it on close, so the plan is audio after the test ends
  - check with a probe: event latency into the DB, and whether a partial `.ogg` can be served
  - **probed 2026-11-08 (self-hosted, stub agent):** events are live (median 9 ms), so the live transcript, state and tool spans are safe to build. Live audio isn't possible with `RecorderIO` as shipped (0 bytes until close), but a partial `.ogg` plays about 4 s behind once the writes reach disk. **Decided (user, 2026-11-08): in v0.1 the recording arrives after the test ends.** Live audio (the tester writing its own recording) is a later option. DB write latency is not measured yet.
- **v0.1 supports Python livekit-agents 1.7.x agents.** Anything else is untested.
- **Parallel runs are limited by the agent's hosting, not by LiveTest** (probed 2026-11-08, see the research doc): on one laptop, up to 8 at once were clean; at 16 the agent's worker ran out of warm processes and some agents never joined. So `--parallel` defaults low (4), the user raises it, and "agent never joined" is its own result, separate from the agent failing a check.
- **Untested so far:**
  - parallel voice runs (heavier than text)
  - Node agents (v0.1 may have to say "Python agents only")
  - other livekit-agents versions
  - real SIP calls
- **Standalone packaging:** sh / iex go through `uv tool install` (see "Tech stack"). winget needs PyApp or PyInstaller; not decided.
- **Naming:** `livetest` is taken on PyPI. That doesn't matter without pip, but keep "LiveKit" out of the product name for trademark reasons.
- **Agent-side gotcha:** on Windows, an agent exports OTel only for its first session per process (see the research file).
