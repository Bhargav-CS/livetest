# Quick start

[← README](../README.md)

This page takes you from install to a first passing test, then adds voice and profiles. Each command also takes `-h`.

## 1. Install

See [Install](install.md). It finishes with `livetest init`, which lists what's left to set up. Run `livetest init` again at any time to see what's still missing.

> [!TIP]
> Prefer a browser? `livetest ui show` does everything on this page: adding agents, plugins and scenarios, and starting runs.

## 2. Give the tester an LLM

The tester is an LLM that plays your user. Any OpenAI-compatible server works:

```sh
livetest plugin add --kind llm --plugin my-groq --provider groq --model openai/gpt-oss-120b
livetest plugin add --kind llm --plugin local-qwen --base-url http://localhost:8000/v1 --model Qwen/Qwen3.5-2B
```

- `--provider` (`openai`, `groq`, `gemini`) fills in the URL. `--base-url` takes any other one.
- At a terminal, livetest asks for the API key without echoing it and saves it in `~/.livetest/.env`. In scripts, set `LIVETEST_KEY_<PLUGIN>` instead (`my-groq` → `LIVETEST_KEY_MY_GROQ`).
- livetest makes one streamed call first and saves nothing if it fails. The first plugin of each kind becomes the active one; `livetest plugin use --plugin NAME` switches.

## 3. Add your agent

Give its LiveKit `agent_name` and one way to get a token:

```sh
livetest agent add --name my-agent --sandbox my-sandbox-id                       # a LiveKit Cloud sandbox
livetest agent add --name my-agent --endpoint https://example.com/api/token     # your token endpoint
livetest agent add --name my-agent --url wss://my-project.livekit.cloud         # your server's API key and secret
livetest agent add --name my-agent                                              # none: pass --token on each run
```

- **`--endpoint`** must take LiveKit's token request (a JSON POST with `room_name`, `participant_identity`, `room_config` with the agent dispatch, and so on). It must answer with `{"server_url": ..., "participant_token": ...}`, like a LiveKit sandbox does.
- **`--url`** asks for the API key and secret and saves them in `~/.livetest/.env`. livetest makes the tokens itself. In scripts, set `LIVETEST_AGENT_KEY_<NAME>` and `LIVETEST_AGENT_SECRET_<NAME>`. For a local server, use `--url ws://localhost:7880`.
- `agent add` gets a token, joins a fresh room and waits for the agent. It saves the agent only if the agent joins.

## 4. Write a scenario

A scenario is the tester's whole system prompt, plus what must happen:

```sh
livetest scenario add --scenario reschedule --tag booking \
  --prompt 'You want your Tuesday slot moved to Friday. Reply with one short turn.' \
  --tool-called reschedule day=friday --no-tool-errors --p95-ms 1500
```

| Check | Fails when |
|---|---|
| `--tool-called NAME [KEY=VALUE ...]` | the tool was never called with those arguments (a subset, exact values) |
| `--tool-not-called NAME [KEY=VALUE ...]` | the tool was called (with those arguments, if given) |
| `--no-tool-errors` | any tool call failed |
| `--p95-ms MS`, `--p99-ms MS` | reply latency went over the limit: from the tester's last word (or line sent) to the agent starting to answer |

A test ends after `--max-turns` (default 10), after `--max-minutes` (default 5), or when the agent leaves. Each scenario gets a number (see `livetest scenario list`), and a run picks scenarios by number, range, name or `all`.

## 5. Run it

```sh
livetest run start --name my-agent --scenario all
livetest run start --name my-agent --scenario 1-5 7 reschedule
livetest run start --name my-agent --scenario 1-20x10       # each one 10 times
```

- livetest shows the plan and asks before starting (`-y` skips that). Tests run 4 at a time; `--parallel N` changes that, but the agent's hosting sets the real limit.
- **The agent speaks first.** A test fails if the agent doesn't greet within 10 s. If your agent waits for the user, pass `--start-conversation "Hi, I need to reschedule"`.
- **The agent must start each reply within 10 s,** or the test ends and fails.
- The exit code is `0` if every test passed, `1` if one failed, `2` for bad input, and `3` if livetest couldn't connect or the agent never joined. CI can use it directly.

Follow or look back:

```sh
livetest run list                       # runs and their scores
livetest run list --run latest          # one run's tests and verdicts
livetest run logs --run latest          # the timeline; follows a live run
livetest run stop --run latest          # stop it; finished tests keep their results
```

## 6. See the results

```sh
livetest ui show                        # the web UI on 127.0.0.1:8787, in the background
livetest report create --run latest     # one HTML file that opens offline
```

Each test page has the checks, a trace of every turn (tester and agent: thinking, tool calls, speaking), the transcript, events, usage in tokens on both sides, what the agent showed (its prompt, tools and session options), logs, and the recording for voice tests.

## Voice tests

Add a streaming STT and a TTS for the tester, then pass `--voice`:

```sh
livetest plugin add --kind stt --plugin moonshine --base-url http://localhost:9092/v1 --model moonshine-base
livetest plugin add --kind tts --plugin kokoro --base-url http://localhost:8881/v1 --model kokoro-7m --voice-id af_msa
livetest run start --name my-agent --scenario 1-5 --voice
```

- The STT must stream over OpenAI's realtime websocket. A batch STT would add seconds to every turn.
- Each voice test is recorded as a stereo `.ogg` file (left: the agent, right: the tester) in `~/.livetest/recordings/`. Pass `--no-recordings` to skip that.
- `--llm`, `--stt` and `--tts` pick a plugin other than the active one for a single run.

## Profiles

A profile sets who the tester is, as the agent sees them. livetest applies it to every token:

```sh
livetest profile add --profile pro-user --participant-name Priya --attribute plan=pro --attribute lang=en
livetest profile add --profile acme --room-metadata '{"tenant": "acme"}' --dispatch-metadata '{"job": 1}'
livetest run start --name my-agent --scenario all --profile pro-user
```

| Flag | The agent reads it as |
|---|---|
| `--attribute KEY=VALUE` | `participant.attributes` |
| `--metadata` | `participant.metadata` |
| `--room-metadata` | `ctx.room.metadata` |
| `--dispatch-metadata` | `ctx.job.metadata` |

When the token comes from a sandbox, an endpoint or `--token`, livetest checks that the token really carries the profile.
