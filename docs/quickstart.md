# Quick start

[← README](../README.md)

This page takes you from install to a first test, then adds voice and profiles. There are two routes: **[in the browser](#in-the-browser)** or **[in the terminal](#in-the-terminal)**. Both use the same data, so you can mix them: set up in the UI and run from CI, or the other way round.

## 1. Install

See [Install](install.md). It finishes with `livetest init`, which lists what's left to set up.

## 2. Before you start

- **Your agent:** its LiveKit `agent_name`, and one way to get a token:
  - a LiveKit sandbox ID
  - your token endpoint
  - your server's URL with its API key and secret
- **A model for the tester:** an LLM that plays your user. Any OpenAI-compatible server works, hosted (OpenAI, Groq, Gemini, Together…) or local (Ollama, vLLM). Voice tests also need a streaming STT and a TTS.

## In the browser

```sh
livetest ui show
```

This opens LiveTest at `http://127.0.0.1:8787`, on this machine only. Until your first run, the Runs page is a **Welcome** checklist. Each step's button opens the right page, and each step shows its terminal command too.

1. **Add agent.** Fill in the agent's name and its token source. LiveTest joins a room once to check that the agent answers, and saves the agent only if it does.
2. **Open Plugins.** There are three cards: **LLM**, **STT** and **TTS**. Pick a provider or enter any OpenAI-compatible URL, give the model and API key, and click **Test connection**. It checks the plugin and saves it as the active one. For text tests, the LLM is enough.
3. **Add scenario.** Write the tester's prompt (who it plays, what it wants, how it talks) and what must happen. See [What a scenario checks](#what-a-scenario-checks).
4. **Start first run.** The **New run** drawer opens:
   - **Agent**, **Profile** (optional) and **Mode** (text or voice)
   - **Scenarios:** tick the ones to run, filter them by tag, and set **×N** to repeat one
   - **More options:** tests in parallel, a tester LLM / STT / TTS other than the active one, **Record each test**, and **The tester opens with** (see [The rules every test follows](#the-rules-every-test-follows))
   - The plan at the bottom shows what will run, with the matching terminal command

The run page fills in live: each test's status, failed checks and agent errors. Open a test to see:
- the checks
- a trace of every turn on both sides (thinking, tool calls, speaking)
- the transcript and events
- token usage on both sides
- what the agent showed: its prompt, tools and session options
- logs, and the recording for voice tests

**Report** downloads a run, or a single test, as one HTML file that opens offline. **New run** in the sidebar starts the next run, and **Re-run failed** and **Run again** pre-fill it.

`livetest ui stop` stops the background UI. Runs it started stop cleanly first.

## In the terminal

Each command also takes `-h`.

### Give the tester an LLM

```sh
livetest plugin add --kind llm --plugin my-groq --provider groq --model openai/gpt-oss-120b
livetest plugin add --kind llm --plugin local-qwen --base-url http://localhost:8000/v1 --model Qwen/Qwen3.5-2B
```

- `--provider` (`openai`, `groq`, `gemini`) fills in the URL. `--base-url` takes any other one.
- At a terminal, livetest asks for the API key without echoing it and saves it in `~/.livetest/.env`. In scripts, set `LIVETEST_KEY_<PLUGIN>` instead (`my-groq` → `LIVETEST_KEY_MY_GROQ`).
- livetest makes one streamed call first and saves nothing if it fails. The first plugin of each kind becomes the active one; `livetest plugin use --plugin NAME` switches.

### Add your agent

```sh
livetest agent add --name my-agent --sandbox my-sandbox-id                       # a LiveKit Cloud sandbox
livetest agent add --name my-agent --endpoint https://example.com/api/token     # your token endpoint
livetest agent add --name my-agent --url wss://my-project.livekit.cloud         # your server's API key and secret
livetest agent add --name my-agent                                              # none: pass --token on each run
```

- **`--endpoint`** must take LiveKit's token request (a JSON POST with `room_name`, `participant_identity`, `room_config` with the agent dispatch, and so on). It must answer with `{"server_url": ..., "participant_token": ...}`, like a LiveKit sandbox does.
- **`--url`** asks for the API key and secret and saves them in `~/.livetest/.env`. livetest makes the tokens itself. In scripts, set `LIVETEST_AGENT_KEY_<NAME>` and `LIVETEST_AGENT_SECRET_<NAME>`. For a local server, use `--url ws://localhost:7880`.
- `agent add` gets a token, joins a fresh room and waits for the agent. It saves the agent only if the agent joins.

### Write a scenario

```sh
livetest scenario add --scenario reschedule --tag booking \
  --prompt 'You want your Tuesday slot moved to Friday. Reply with one short turn.' \
  --tool-called reschedule day=friday --no-tool-errors --p95-ms 1500
```

Each scenario gets a number (see `livetest scenario list`). See [What a scenario checks](#what-a-scenario-checks).

### Run it

```sh
livetest run start --name my-agent --scenario all
livetest run start --name my-agent --scenario 1-5 7 reschedule
livetest run start --name my-agent --scenario 1-20x10       # each one 10 times
```

- livetest shows the plan and asks before starting (`-y` skips that). Tests run 4 at a time; `--parallel N` changes that, but the agent's hosting sets the real limit.
- The exit code is `0` if every test passed, `1` if one failed, `2` for bad input, and `3` if livetest couldn't connect or the agent never joined. CI can use it directly.

Follow or look back:

```sh
livetest run list                       # runs and their scores
livetest run list --run latest          # one run's tests and verdicts
livetest run logs --run latest          # the timeline; follows a live run
livetest run stop --run latest          # stop it; finished tests keep their results
livetest report create --run latest     # one HTML file that opens offline
```

`livetest ui show` opens any run in the browser too.

## What a scenario checks

| Check | In the terminal | Fails when |
|---|---|---|
| tool called | `--tool-called NAME [KEY=VALUE ...]` | the tool was never called with those arguments (a subset, exact values) |
| tool not called | `--tool-not-called NAME [KEY=VALUE ...]` | the tool was called (with those arguments, if given) |
| no tool errors | `--no-tool-errors` | any tool call failed |
| reply latency | `--p95-ms MS`, `--p99-ms MS` | reply latency went over the limit: from the tester's last word (or line sent) to the agent starting to answer |

A test ends after its maximum number of turns (default 10), after its time limit (default 5 minutes), or when the agent leaves.

## The rules every test follows

- **The agent speaks first.** A test fails if the agent doesn't greet within 10 s. If your agent waits for the user, give the tester an opening line: **The tester opens with** in the UI, or `--start-conversation "Hi, I need to reschedule"` in the terminal.
- **The agent must start each reply within 10 s,** or the test ends and fails.
- **The agent's session host must be on.** It is by default. Without it, a test ends as `no_session_host`.

## Voice tests

The tester needs a streaming STT (OpenAI's realtime websocket; a batch STT would add seconds to every turn) and a TTS.

- **In the browser:** fill in the **STT** and **TTS** cards on the Plugins page, then pick **Mode: voice** in New run.
- **In the terminal:**

  ```sh
  livetest plugin add --kind stt --plugin moonshine --base-url http://localhost:9092/v1 --model moonshine-base
  livetest plugin add --kind tts --plugin kokoro --base-url http://localhost:8881/v1 --model kokoro-7m --voice-id af_msa
  livetest run start --name my-agent --scenario 1-5 --voice
  ```

Each voice test is recorded as a stereo `.ogg` file (left: the agent, right: the tester) in `~/.livetest/recordings/`. The test page plays it along the trace. Turn it off with **Record each test** in the UI or `--no-recordings` in the terminal.

## Profiles

A profile sets who the tester is, as the agent sees them. livetest applies it to every token. Without one, the tester joins as a plain participant.

- **In the browser:** Profiles → **Add profile**, then pick it under **Profile** in New run.
- **In the terminal:**

  ```sh
  livetest profile add --profile pro-user --participant-name Priya --attribute plan=pro --attribute lang=en
  livetest profile add --profile acme --room-metadata '{"tenant": "acme"}' --dispatch-metadata '{"job": 1}'
  livetest run start --name my-agent --scenario all --profile pro-user
  ```

| Field | The agent reads it as |
|---|---|
| attributes (`--attribute KEY=VALUE`) | `participant.attributes` |
| metadata (`--metadata`) | `participant.metadata` |
| room metadata (`--room-metadata`) | `ctx.room.metadata` |
| dispatch metadata (`--dispatch-metadata`) | `ctx.job.metadata` |

When the token comes from a sandbox, an endpoint or `--token`, livetest checks that it really carries the profile.
