# Commands

[← README](../README.md)

```
livetest <command> [<action>] [flags]
```

Every command and action takes `-h`. `remove`, `run start` and `run stop` ask first; `-y` skips the question. `list` actions and `run logs` take `--json` for scripts.

**Global flags:** `--db FILE` (default `~/.livetest/livetest.db`) · `-v` (debug logs and tracebacks on the console) · `--version`

| Command | Actions |
|---|---|
| [`init`](#init) | set livetest up |
| [`agent`](#agent) | `add` · `list` · `remove` (`rm`) |
| [`profile`](#profile) | `add` · `list` · `remove` (`rm`) |
| [`scenario`](#scenario) | `add` · `list` · `remove` (`rm`) |
| [`plugin`](#plugin) | `add` · `list` · `remove` (`rm`) · `use` |
| [`run`](#run) | `start` · `list` · `logs` · `stop` |
| [`report`](#report) | `create` |
| [`ui`](#ui) | `show` · `serve` · `stop` |

## init

Creates `~/.livetest/` and its database, then prints the next steps: the web UI, or the terminal commands for what's still missing. It asks nothing and starts nothing. The install scripts run it.

## agent

The agents you test.

**`agent add`** joins a fresh room and waits for the agent; it saves the agent only if the agent joins. Give at most one token source. Leave out `--name`, or all three sources, and it asks.

| Flag | |
|---|---|
| `--name NAME` | the agent's LiveKit `agent_name` (what it registered with) |
| `--sandbox ID` | a LiveKit sandbox token server ID |
| `--endpoint URL` | your token endpoint (LiveKit's token request in, `server_url` and `participant_token` out) |
| `--url URL` | your LiveKit server; livetest makes tokens with the API key and secret it asks for |

With no source, pass `--token` to `run start` each time.

**`agent list`** `[--json]` · **`agent remove`** `--name NAME [-y]`

## profile

Who the tester is, as the agent sees them.

**`profile add`**

| Flag | |
|---|---|
| `--profile NAME` | its name |
| `--identity ID` | the tester's participant identity (default `tester`) |
| `--participant-name NAME` | the tester's display name |
| `--attribute KEY=VALUE` | participant attribute, repeatable (`participant.attributes`) |
| `--metadata JSON\|TEXT` | `participant.metadata` |
| `--room-metadata JSON\|TEXT` | `ctx.room.metadata` |
| `--dispatch-metadata JSON\|TEXT` | `ctx.job.metadata` |

**`profile list`** `[--json]` · **`profile remove`** `--profile NAME [-y]`

## scenario

What the tester is told to do, and what to check.

**`scenario add`**

| Flag | |
|---|---|
| `--scenario NAME` | its name |
| `--prompt TEXT` | the tester LLM's whole system prompt |
| `--tag TAG` | repeatable; for `scenario list --tag` |
| `--max-turns N` | end the test after this many turns (default 10) |
| `--max-minutes N` | end the test after this long (default 5) |
| `--tool-called NAME [KEY=VALUE ...]` | expect this tool called, with these arguments; repeatable |
| `--tool-not-called NAME [KEY=VALUE ...]` | expect this tool never called (with these arguments, if given); repeatable |
| `--no-tool-errors` | expect no tool call to fail |
| `--p95-ms MS` · `--p99-ms MS` | reply latency limits, 95th and 99th percentile |

**`scenario list`** `[--tag TAG] [--json]` · **`scenario remove`** `--scenario NAME [-y]`

## plugin

The models the tester uses: an LLM to think, plus an STT and a TTS to hear and speak in voice mode. One plugin of each kind is active.

**`plugin add`** makes one streamed call and saves nothing if it fails. At a terminal it asks for the API key and saves it in `~/.livetest/.env`; scripts set `LIVETEST_KEY_<PLUGIN>` instead. With no flags, it asks for everything.

| Flag | |
|---|---|
| `--plugin NAME` | its name |
| `--kind llm\|stt\|tts` | think (`llm`), hear (`stt`) or speak (`tts`) |
| `--provider openai\|groq\|gemini` | fills in `--base-url` |
| `--base-url URL` | any OpenAI-compatible URL |
| `--model MODEL` | the model's name on that server |
| `--voice-id VOICE` | TTS only: the voice, e.g. `alloy` |

An STT must stream (OpenAI's realtime websocket).

**`plugin list`** `[--json]` · **`plugin use`** `--plugin NAME` · **`plugin remove`** `--plugin NAME [-y]`

## run

Scenarios played against an agent.

**`run start`**

| Flag | |
|---|---|
| `--name NAME` | the agent to test |
| `--scenario ITEM ...` | `7`, `1-20`, a name or `all`; `xN` repeats (`1-20x10`) |
| `--run ID` | the run's ID (default: the next free number) |
| `--profile NAME` | who the tester is (default: a plain participant) |
| `--parallel N` | tests at once (default 4) |
| `--voice` | talk by voice; needs a tester STT and TTS |
| `--start-conversation TEXT` | the tester opens by saying exactly this (default: the agent opens, and must greet within 10 s) |
| `--llm` · `--stt` · `--tts PLUGIN` | a plugin other than the active one |
| `--no-recordings` | voice: don't record |
| `--token TOKEN` | for an agent saved without a token source; runs one test |
| `-y` | don't ask for confirmation |

**`run list`** `[--run ID|latest] [--json]`: runs, or one run's tests and verdicts.
**`run logs`** `--run ID|latest [--test N] [--level LEVEL] [--json]`: a run's timeline; follows while it's live.
**`run stop`** `--run ID|latest [--test N] [-y]`: stop a run or one test.

## report

**`report create`** `--run ID|latest [--test N] [--output FILE] [--no-recordings]` writes one self-contained HTML file that opens offline (default `./run-<run>_report.html`). Recordings are embedded unless you pass `--no-recordings`.

## ui

The web UI, on this machine only (`127.0.0.1`). It does setup and new runs, and shows runs live with their recordings.

- **`ui show`** `[--port PORT]`: opens it in the browser, starting it in the background if needed (default port 8787).
- **`ui serve`** `[--port PORT]`: runs it in this terminal until Ctrl+C.
- **`ui stop`**: stops the background UI. Runs it started stop cleanly first.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | OK; every test passed |
| `1` | a test failed or errored |
| `2` | bad input or config |
| `3` | couldn't connect, or the agent never joined (including no session host) |
| `130` | Ctrl+C (`run start` stops the run first) |

A run with mixed outcomes exits with the first that applies: `3`, then `1`, then `0`.

## Environment variables

Real environment variables override what livetest has saved, which suits CI.

| Variable | |
|---|---|
| `LIVETEST_KEY_<PLUGIN>` | a plugin's API key (`my-groq` → `LIVETEST_KEY_MY_GROQ`) |
| `LIVETEST_AGENT_KEY_<NAME>` · `LIVETEST_AGENT_SECRET_<NAME>` | an agent's API key and secret (`--url` agents) |
| `LIVETEST_LLM` · `LIVETEST_STT` · `LIVETEST_TTS` | the plugin to use, like `--llm` / `--stt` / `--tts` |
| `LIVETEST_TOKEN` · `LIVEKIT_URL` | a raw token and its server, like `--token` |
| `LIVETEST_REF` | install scripts only: the version to install |
