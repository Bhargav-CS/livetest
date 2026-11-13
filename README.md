# livetest

**End-to-end tests for any LiveKit agent, from just a token.**

livetest joins your agent's room as a user, talks to it by text or voice, and records everything the agent does: what it said, the tools it called, its latency, its token usage, and a stereo recording. Then it checks the results against your scenario.

- **Black box:** you don't change the agent's code, and it doesn't rely on the agent's telemetry.
- **Any host:** LiveKit Cloud or your own LiveKit server.
- **Your own models:** the tester thinks, hears and speaks through any OpenAI-compatible server, hosted or local.
- **Results stay on your machine:** a local database, a web UI and HTML reports that open offline.

## Install

```sh
curl -LsSf https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.sh | sh     # macOS / Linux
```

```powershell
irm https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.ps1 | iex          # Windows
```

You don't need Python. To upgrade, run the same line again. To pick a version or uninstall, see [Install](docs/install.md).

## Quick start

```sh
livetest ui show
```

This opens the web UI at `http://127.0.0.1:8787` (on this machine only). Until your first run, it shows a **Welcome** page with four steps:

1. **Add agent:** your agent's LiveKit `agent_name` and how to get a token (a sandbox ID, your token endpoint, or your server's URL). LiveTest joins a room once to check that the agent answers.
2. **Open Plugins:** pick the tester's LLM, plus an STT and a TTS if you want voice tests. **Test connection** checks each one and saves it.
3. **Add scenario:** who the tester plays, what it wants, and what must happen (tools called, latency limits).
4. **Start first run:** one conversation in text mode is the quickest first check.

Runs stream in live. Each test has its transcript, a trace of every turn, tool calls, usage, and the recording for voice tests. **Report** downloads it as one HTML file that opens offline.

Each step also shows its terminal command. The [Quick start](docs/quickstart.md) gives both routes, in the browser and in the terminal (for scripts and CI), plus voice tests and profiles.

## Concepts

| | |
|---|---|
| **Agent** | Your LiveKit agent: its `agent_name` and how livetest gets a token for it (a LiveKit sandbox, your token endpoint, your server's API key, or a raw token). `agent add` joins once to prove it works before saving it. |
| **Plugin** | A model the *tester* uses: an LLM to think, plus an STT and a TTS for voice tests. Any OpenAI-compatible URL works. One plugin of each kind is active. Keys go in `~/.livetest/.env`, never in the database. |
| **Scenario** | What the tester is told to do (its prompt), when to stop (`--max-turns`, `--max-minutes`), and what must happen: tools called or not called, no tool errors, and reply latency limits. |
| **Profile** | Who the tester is, as the agent sees them: identity, name, participant attributes and metadata, room metadata and dispatch metadata. Optional. |
| **Run** | Scenarios played against an agent, in parallel. Each one is a **test** with a status, a verdict and its checks. The agent must start each reply within 10 s. |
| **Results** | Everything goes into `~/.livetest/livetest.db`: transcript, tool calls, agent state, metrics, usage, logs, and voice recordings. View them with `livetest ui show`, `run logs`, or `report create`. |

## Requirements

- Agents built with **Python livekit-agents 1.7.x**. Other versions and Node agents are untested.
- The agent's **session host** must be on. It is by default; `session.start(..., session_host=False)` turns it off. livetest reads almost everything through it. Without it, a test ends as `no_session_host`.
- For the tester, an **OpenAI-compatible LLM**. For voice tests, also a streaming STT (OpenAI's realtime websocket) and a TTS.

## Docs

- [Install](docs/install.md): what the install script does, picking a version, upgrading, uninstalling
- [Quick start](docs/quickstart.md): your first test, text and voice
- [Commands](docs/commands.md): every command and its flags, exit codes, environment variables

Every command also takes `-h`, for example `livetest run start -h`.
