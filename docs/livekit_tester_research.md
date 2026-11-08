# LiveKit agent tester: research notes

Research done 2026-11-04/05 on branch `feature/self-hosted-livekit`. Every result below was tested
against `agent.py` (Baymax, livekit-agents 1.7.1, Python), unless it is marked *untested*.

## Goal

A testing tool for **any** LiveKit agent:

- **Input:** only a token, or a way to get one (a token endpoint, or API key and secret).
- **No agent changes:** it never touches the agent's code, `.env`, telemetry or logging. It must work even when the agent has no traces or logs.
- **Any host:** the agent can run on LiveKit Cloud or on a self-hosted LiveKit server.

## What a tester can do with just a token

Rows marked ✅ were verified on LiveKit Cloud **and** on a self-hosted server (podman, `livekit-server --dev`).

### Join and converse
- ✅ **Join the agent's room as a normal participant** and trigger the dispatch, with the agent dispatch carried inside the token (`room_config.agents`).
- ✅ **Voice conversation:**
  - the tester's own STT, LLM and TTS speak to the agent
  - the agent's replies are heard and transcribed
  - the tester's LLM generates the next turn
- ✅ **Text-only (LLM-only) conversation:**
  - switch the agent's audio in and out off remotely
  - send text straight to the agent's LLM
  - get the whole turn back as structured items
  - 0.3 to 3.4 s per turn, with no TTS
- ✅ **Record the session locally:** stereo `.ogg` file, channel 0 = agent as heard, channel 1 = tester as spoken.

### Observe the agent's tool calls
- ✅ **Every tool call:**
  - name, arguments, output, status (`TC_DONE` / `TC_ERROR` / `TC_CANCELLED`)
  - start and end time, on the agent's own clock
  - works in text and voice mode
- ✅ **Re-create them as OpenTelemetry spans** in the tester's own trace, with real durations (for example `show_resume` 3.36 ms). This needs no agent telemetry.

### Read agent internals (session host requests)
- ✅ **Framework info:** SDK and version (`python`, `1.7.1`).
- ✅ **Agent info:** the full system prompt (7,740 chars for Baymax), the tool list, and the agent's chat context.
- ✅ **Session state:**
  - agent and user state
  - every session option: interruption settings, endpointing delays, `max_tool_steps`, `user_away_timeout`, preemptive generation
- ✅ **Session usage:** per model, provider, model name, input/output/cached tokens (LLM, STT; TTS in voice mode).
- ✅ **Chat history:** the entire session, including turns from before the tester joined, handoffs and config updates.
- ✅ **RTC stats:** the agent's publisher stats. *Contents not examined yet.*

### Live events (session host push)

| Event | Text | Voice | Use |
|---|---|---|---|
| `tool_execution_updated` (started/ended) | ✅ | ✅ | tool timing and spans |
| `function_tools_executed` | ✅ | ✅ | calls and outputs (no per-call timestamps) |
| `conversation_item_added` | ✅ | ✅ | transcript, plus per-message metrics |
| `agent_state_changed` / `user_state_changed` | ✅ | ✅ | listening / thinking / speaking timeline |
| `session_usage_updated` | ✅ | ✅ | cost tracking (62 events in 45 s of voice) |
| `user_input_transcribed` | n/a | ✅ | **what the agent's STT heard**. Example: it heard "Bargov Pottky" for "Bhargav Patki" |
| `eot_prediction` | n/a | ✅ | agent's end-of-turn decisions |
| `error`, `agent_false_interruption`, `overlapping_speech`, `amd_prediction`, `debug_message` | not seen | not seen | subscribed, but none fired in these runs |

**Per-message metrics** (`ChatMessage.metrics`):
- `llm_node_ttft` (0.27 to 0.50 s observed) and `llm_node_tps`
- `started_speaking_at` / `stopped_speaking_at`
- in voice mode also `e2e_latency`, `tts_node_ttfb`, `transcription_delay`, `end_of_turn_delay`

### Live view (verified 2026-11-08, self-hosted, `probe_live_events.py` + `probe_live_rec.py`)
- ✅ **Events arrive in real time:** median 9 ms from the agent's `created_at` to the tester (19 events, text mode, same machine). Only the first event of a fresh job is slower (70 to 360 ms). Tool `started` / `ended` arrive 1.5 s apart for a 1.5 s tool, so live tool spans work.
- ❌ **`RecorderIO` as shipped can't be played mid-test:** it encodes every 2.5 s (`WRITE_INTERVAL`), but `av.open(path)` keeps the bytes in ffmpeg's write buffer, so the file stays 0 bytes until close.
- ✅ **A partial `.ogg` does play when the writes reach disk:** with the container opened on an unbuffered file object, a copy of the growing file decoded fine, trailing the live position by about 4 s. `RecorderIO.start()` only accepts a path, so live audio needs either a patched `av.open` or the tester writing its own recording.
- Zero-credit setup: a stub agent that overrides `llm_node` (canned text plus one tool call) and is given an LLM with a fake key, because `AgentSession` skips replies when `llm` is None.

### Which models the agent uses (from saved `inspect_*.json`)
- ✅ `session_usage_updated` / `get_session_usage` name each model the agent runs, with `provider` and `model`, e.g. `api.groq.com` / `openai/gpt-oss-120b`, `Deepgram` / `nova-3`.
- Fields per type:
  - LLM: input / cached / output tokens, split into text, audio and image
  - STT: `audio_duration`
  - TTS: `characters_count`, `audio_duration`
- That's enough to price each part. A self-hosted model shows up under its own host, which a price table won't know.

### Parallel tests (verified 2026-11-08, self-hosted, stub agent, `probe_parallel.py`)
- Text mode, agent and tester on one Windows laptop:

  | N at once | Passed | Agent joined (p50) | Event lag (p50 / max) |
  |---|---|---|---|
  | 1 | 1/1 | 1.8 s | 5 / 120 ms |
  | 4 | 4/4 | 0.9 s | 28 / 455 ms |
  | 8 | 8/8 | 3.1 s | 160 / 467 ms |
  | 16 | 12–15 of 16 | 5–6 s | 0.5–0.6 s / 3.6–4.5 s |

- **The limit is the agent's worker, not the tester.** At 16 the worker logged "no warmed process available" and slow job starts, and some agents never joined within 60 s. Splitting the tester into 2 × 8 processes did worse (10/16), so the tester's single process was not the bottleneck.
- **LiveKit Cloud caps concurrent agent jobs per plan** (5 on Alive's project; seen 2026-11-08 during module 3's live tests). The job slot isn't freed the moment the tester leaves, so about 6 back-to-back sessions within a minute left the next one with no agent. Deleting the room (`DeleteRoom`, needs the API key) frees it at once. With only a token, livetest can't do that, so a run should pace back-to-back tests, and "agent never joined" must say the cap may be the cause.

### Room level (works on any LiveKit agent, even with the session host off)
- ✅ **Agent participant:** identity, kind (`AGENT`), and attributes (`lk.agent.state`, `lk.agent.name`).
- ✅ **Tracks the agent publishes:** for example `roomio_audio`, `audio/red`.
- ✅ **The agent's transcript stream** (`lk.transcription`) and its audio.

### Session context from the token (verified 2026-11-06, `scratchpad/probe_metadata.py`)
- ✅ **Room metadata:** `RoomConfiguration.metadata` in the token becomes the room's metadata when the first join creates the room. Seen by the participant (`room.metadata`) and by the server (`ListRooms`). Self-hosted.
- ✅ **Participant metadata and attributes** from the token (`with_metadata`, `with_attributes`) show up on the participant. Self-hosted.
- ✅ **Dispatch metadata** (`RoomAgentDispatch.metadata`, which agents read as `ctx.job.metadata`) is stored on the dispatch (`ListDispatch`). Self-hosted.
- ✅ **The Cloud sandbox token server passes all of them through:** `participant_metadata`, `participant_attributes`, `room_config.metadata` and `room_config.agents[].metadata` all appear in the issued JWT, along with `canUpdateOwnMetadata: true`. There's no `kind`. The JWT was decoded only; the room wasn't joined.
- ✅ **`set_attributes` after joining** works with `canUpdateOwnMetadata`. **Without the grant it's dropped silently:** no exception, and the attributes stay unchanged.
- `RoomAgentDispatch` also has `attributes`, `restart_policy` and `deployment`; `RoomConfiguration` also has `tags`. *Untested.*

### Observability without agent cooperation
- ✅ **Tester-side traces and logs in Grafana,** under the tester's own service name, all findable by room (`lk.pii.room_name` set on a wrapping `tester_session` span).
- ✅ **Recording playable next to the traces:**
  - a Grafana Text panel in HTML mode with `<audio src=...>` survives the sanitizer
  - `${room}` is filled in from the dashboard variable
  - the file is served over local HTTP
  - verified in headless Edge

## How each piece works

### Getting a token
- **fe-style sandbox token server (LiveKit Cloud):**
  - `POST https://cloud-api.livekit.io/api/v2/sandbox/connection-details`
  - header `X-Sandbox-ID: <id>`
  - body `{"room_name", "participant_identity", "room_config": {"agents": [{"agent_name"}]}}`
  - response gives `server_url` and `participant_token`
  - **Gotcha:** Cloudflare returns 403 (error 1010) to Python urllib's default `User-Agent`. Set any custom User-Agent.
- **Self-hosted:** the user mints a token with `livekit.api.AccessToken(key, secret)`, `.with_room_config(RoomConfiguration(agents=[RoomAgentDispatch(agent_name=...)]))`.
- **SIP-only agents** (from an earlier chat):
  - they need a token with **kind `sip`**, otherwise the agent ignores the participant
  - the sandbox token request has no field for kind, so the client must set it in their own token endpoint, or provide an API key
  - text mode via the session host works with a standard token in most cases

### Why the agent listens to the tester
- `RoomIO` links to the first participant of kind `STANDARD`, `SIP` or `CONNECTOR`, and ignores the rest.
- A token-joined tester is `STANDARD`, so the agent links to it.
- The agent itself is kind `AGENT`, so the tester's own session needs `participant_kinds=[PARTICIPANT_KIND_AGENT]` in voice mode.
- An `AgentServer` worker can't be the tester: it would join as `AGENT`, which the agent ignores.

### Text-only mode: three ways compared

| | A. Simulation attribute | B. Plain `lk.chat` | **C. Session host (`RemoteSession`)** ← recommended |
|---|---|---|---|
| How | Dispatch attribute `lk.simulator.dispatch` = `{"simulation_run_id", "mode": "SIMULATION_MODE_TEXT"}` | Send on `lk.chat`, read `lk.transcription` | `update_io(input_audio_enabled=False, output_audio_enabled=False)`, then `run(text)` |
| Agent TTS | off | **runs every turn** | off |
| Turn time | 0.7 to 2 s | 14 to 22 s (text paced to speech) | 0.3 to 3.4 s |
| Tool calls visible | no | no | **yes, structured** |
| Token needs | control over the dispatch | any | any |
| Agent needs | livekit-agents with simulations | text input on (default) | session host on (default in 1.7.1) |
| Risk | internal simulator mechanism, not meant for third parties | documented, stable | internal module path, but it's LiveKit's own remote-control API |

Fallback order: **C**, then **B** if the session host is off.
- If the agent greets in `on_enter`, that greeting still uses TTS once, before `update_io` lands.
- `console --text` is not an option: it runs the agent locally in a terminal, with no room.

### Session host basics
- **Library:** `livekit.agents.voice.remote_session`. The client is `RemoteSession.from_room(room, agent_identity)`, then `start()` and `wait_for_ready()`.
- **Transport:** room byte streams on topic `lk.agent.session`, with protobuf messages from `livekit.protocol.agent_pb.agent_session`. This is why it works on both Cloud and self-hosted servers.
- **Requests without a helper method** (`get_session_usage`, `get_rtc_stats`) can be sent as raw `SessionRequest` through `_send_request`.
- **Agent side:** `session_host` defaults to True and accepts requests from **any** participant in the room.

## Gotchas found while testing

| Gotcha | Fix |
|---|---|
| Plugins (ElevenLabs, Deepgram) outside a worker fail with "http session outside of a job context" | Wrap the tester in `async with utils.http_context.open():` |
| Turn detector (`EnglishModel`) needs a job's inference executor | Use VAD-only turn detection in the tester |
| Tester's own voice `AgentSession` also hosts `lk.agent.session`, so `RemoteSession` fails with "byte stream handler already set" | `session.start(..., session_host=False)` |
| "ignoring text stream lk.transcription" log spam on both sides | Tester: `text_output=False`, plus a no-op `register_text_stream_handler("lk.transcription", ...)` |
| `function_tools_executed` has no per-call timestamps (spans landed at time 0) | Time tool calls from the `tool_execution_updated` started/ended pair |
| Session host replies use protobuf enums (`role == ASSISTANT`, i.e. 3), not strings | Compare against `livekit.protocol.agent_pb.agent_session.ASSISTANT` |
| `conversation_item_added` also fires for `AgentHandoff`, which has no `.role` | Check `ev.item.type == "message"` first |
| ElevenLabs library voices return 402 on this plan (`21m00Tcm4TlvDq8ikWAM`) | Use a premade voice; `JBFqnCBsd6RMkjVDRZzb` works |
| Built-in `RecorderIO` only starts inside a job | Wire it by hand: `record_input`, `record_output`, `start(output_path=...)`, `aclose()` |
| Tester sessions outside a job carry no `lk.pii.room_name` | Wrap the session in `tracer.start_as_current_span("tester_session", attributes={"lk.pii.room_name": room})` |
| Groq free tier is 8,000 tokens/min, shared by tester and agent | About 3 turns/min with Baymax's resume prompt; use a separate key or a self-hosted LLM for the tester |
| `set_attributes` without the `canUpdateOwnMetadata` grant is silently ignored | Read the attributes back after setting them, and fail if they didn't change |
| stdlib `http.server` has no HTTP Range support, so browsers can't seek in served audio | Serve recordings with aiohttp `FileResponse` |
| **Windows + livekit-agents:** jobs run as threads in one process, and every job end calls `_shutdown_telemetry()` (`job.py:352`), so an agent's OTel exports only its **first** session per process | Restart the agent between tests, or fix it in the agent. The tester's own spans don't depend on this |

## Security observation

Anyone with a token for an agent's room (for the portfolio, any visitor via the public sandbox token server) can:
- read the agent's **full system prompt and tool list**
- **inject turns** through the session host

That's the same feature the tester relies on. The agent can turn it off with `session.start(..., session_host=False)`.

## Existing tools (searched 2026-11-05)

| Tool | What it is | Token only? | Self-hosted | Tool calls / internals |
|---|---|---|---|---|
| [LiveKit Agent Simulations](https://docs.livekit.io/testing/simulations/) (`lk agent simulate text/audio`) | First-party; LLM simulated users plus a judge; text or audio | No: needs an authenticated CLI and a Cloud project; by default it starts your agent locally | Runs on LiveKit Cloud | Exports chat contexts; uses mechanism A (dispatch attribute) |
| [LiveKit Agents test framework](https://docs.livekit.io/agents/start/testing/) (pytest/Vitest) | First-party unit tests, text, turn by turn | No: imports the agent's code | n/a | Yes, but needs the code |
| [quangdang46/livekit_agent_simulator](https://github.com/quangdang46/livekit_agent_simulator) (`lks`) | Black-box AI caller over WebRTC/SIP, forensic logs, MCP; MIT, pre-1.0, 7 stars | No: needs URL, API key and secret, and agent name | Likely | Only via data-message patterns you configure; no text mode |
| [deepeval LiveKit connector](https://deepeval.com/integrations/voice-agent-connectors/livekit) | Joins the room as a participant and trades audio | Needs project credentials to mint tokens | Not stated | Not stated |
| [Future AGI agent-simulate](https://docs.futureagi.com/docs/cookbook/simulate-sdk) | Simulated customer through a LiveKit room | Runs your agent as a task | Needs a LiveKit server | Not stated |
| voicetest, EVA-Bench, Pipecat eval, Voice Lab, promptfoo ([comparison](https://www.cekura.ai/discover/open-source-voice-agent-testing-tools)) | Imported agents or text-only scoring | No | n/a | No |

**Gap this tester fills:** no existing tool found works from **only a token** and also reads the agent's tool calls and internals through the session host. The others need API credentials, CLI auth, Cloud, or the agent's code.

## Probe scripts (repo `scratchpad/`)

- **`probe_tester.py`** (`sim` / `chat` / `voice` / `host`): text mode via A/B/C, and voice with recorder plus session-host events.
- **`probe_inspect.py`:** everything readable from the session host, plus tool spans. `PROBE_SELFHOST=ws://<host>:7880` mints a self-hosted token. Dumps to `scratchpad/inspect_*.json`.
- **`probe_span.py`:** checks that a wrapping span makes child spans findable by room.
- **`spans.sh <room>`:** span-name counts for a room's trace in Tempo.
- **`probe_live_agent.py` / `probe_live_events.py`:** zero-credit stub agent, plus a tester that measures event lag.
- **`probe_live_rec.py`** (and `probe_live_rec_writes.py`, which logs the writes): whether a recording can be read while it's still being written.

## Not tested yet

- Agents with `session_host=False`, other livekit-agents versions, and the Node SDK
- Contents of the RTC stats
- `error` / false-interruption events (none fired)
- Real SIP calls
- A Grafana dashboard combining audio, traces and logs as one saved dashboard (each piece was verified separately)
