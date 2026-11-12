"""Runs: pick the scenarios, then run each test, `parallel` at a time. In text mode the tester LLM talks to the agent
through the agent's session host; in voice mode the tester's own STT / LLM / TTS session talks to it over audio, and
the session host only reports. A voice test can be recorded: stereo .ogg, channel 0 the agent as heard, 1 the tester.

Everything lands in the DB as it happens: the agent's session-host events, what the tester said, what the agent
showed (prompt, tools, session options, what it saw of the tester) and log lines.
"""

import asyncio
import contextvars
import logging
import re
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import get_args

from google.protobuf.json_format import MessageToDict
from livekit.protocol.agent_pb import agent_session as pb

from livetest.runtime import checks, session, store
from livetest.runtime.models import Scenario

log = logging.getLogger("livetest.runner")
_current = contextvars.ContextVar("result", default=None)  # the test a log line belongs to

class _ToEvents(logging.Handler):
    def __init__(self, db, result: int):
        super().__init__()
        self.db, self.result = db, result

    def emit(self, record: logging.LogRecord) -> None:
        if _current.get() != self.result:  # another test's line, another thread's, or outside any test
            return
        try:
            store.event(self.db, self.result, "log", record.levelname,
                        {"logger": record.name, "message": record.getMessage()}, record.created)
        except Exception:
            self.handleError(record)


@contextmanager
def logged(db, result: int):
    """Log lines from this context land on `result` as events. Tests in parallel tasks each keep their own."""
    handler, tag = _ToEvents(db, result), _current.set(result)
    logging.getLogger().addHandler(handler)
    try:
        yield
    finally:
        logging.getLogger().removeHandler(handler)
        _current.reset(tag)


def select(items: list[str], scenarios: list[tuple[int, Scenario]]) -> list[tuple[int, Scenario]]:
    """`run start --scenario` items (7, 1-20, a name, all; each may end in xN) → (number, scenario) per test, in
    order. Overlaps add up, ranges skip removed numbers. ValueError for an item that picks nothing."""
    by_number, by_name = dict(scenarios), {s.name: (n, s) for n, s in scenarios}
    out = []
    for item in items:
        what, times = item, 1
        if item not in by_name and (m := re.fullmatch(r"(.+)x(\d+)", item)):  # "fix2" is a name, not fi x2
            what, times = m[1], int(m[2])
        if what == "all":
            picked = scenarios
        elif m := re.fullmatch(r"(\d+)-(\d+)", what):
            picked = [(n, s) for n, s in scenarios if int(m[1]) <= n <= int(m[2])]
        elif what.isdecimal():
            picked = [(int(what), by_number[int(what)])] if int(what) in by_number else []
        else:
            picked = [by_name[what]] if what in by_name else []
        if not picked or not times:
            raise ValueError(f"'{item}' picks no scenario; see: livetest scenario list")
        out += picked * times
    return out


def run_id(v: str) -> str:
    """Letters, digits, - _ . up to 64: it ends up in folder and file names and UI links."""
    if v == "latest" or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", v):
        fix = re.sub(r"[^A-Za-z0-9._-]", "-", v)[:64].strip("-")
        raise ValueError(f"run id '{v}' isn't allowed: letters, digits, - _ . up to 64, and not 'latest'"
                         + (f"; use {fix}" if fix not in ("", "latest", v) else ""))
    return v


async def run(db, tests: list[tuple[int, Scenario, str]], connect, tester, parallel: int = 4,
              key: str | None = None, secret: str | None = None, voice=None, recordings: Path | None = None,
              opening: str | None = None) -> None:
    """A run's queued tests, (result id, scenario, room) each, `parallel` at a time. `connect(room)` gives
    (url, token); `tester` is the tester's LLM and `voice` its (STT, TTS) for voice mode, all shared, and the caller
    closes them. Voice tests are recorded into `recordings` unless it's None. `opening` (`--start-conversation`):
    the tester's first line, as written; None, the agent opens."""
    gate = asyncio.Semaphore(parallel)

    async def one(result: int, scenario: Scenario, room: str) -> None:
        async with gate:
            await run_test(db, result, scenario, room, connect, tester, key, secret, voice, recordings, opening)

    await asyncio.gather(*(one(*t) for t in tests))


async def run_test(db, result: int, scenario: Scenario, room: str, connect, tester, key: str | None = None,
                   secret: str | None = None, voice=None, recordings: Path | None = None,
                   opening: str | None = None) -> None:
    """Run one queued test in `room` against the agent `connect(room)`'s token dispatches; with the API key the room
    is deleted after. Skips a test stopped while queued. A failing test doesn't raise: the result's status says what
    happened. Cancelled (Ctrl+C), it ends as stopped, like `run stop`.
    """
    if not store.begin(db, result):
        return
    path = recordings / f"test-{store.result(db, result)['test']}.ogg" if voice and recordings else None
    status, ended = "error", None
    with logged(db, result):
        try:
            # Not to_thread: its thread would copy this test's log tag, and SQLite is this thread's only.
            url, token = await asyncio.get_running_loop().run_in_executor(None, connect, room)
            async with session.joined(url, token, key, secret) as (room, agent):
                log.info("agent %s joined room %s", agent.identity, room.name)
                status, ended = await _converse(db, result, room, agent, scenario, tester, voice, path, opening)
        except ConnectionError as e:
            status, ended = "never_joined", str(e)
        except asyncio.CancelledError:
            status, ended = "stopped", "stopped by user"
            raise
        except Exception as e:
            log.exception("test failed", extra={"console": False})  # traceback in errors.log; the summary says so
            ended = str(e) or type(e).__name__
        finally:
            events = store.events(db, result)
            outcomes = checks.evaluate(scenario, ended, events) if status == "done" else None
            # Every status: a stopped or failed test still used tokens.
            store.finish_result(db, result, status, ended, outcomes, checks.usage(events))


async def _stopping(db, result: int) -> None:
    """Returns once `run stop` asked this test to stop."""
    while store.result(db, result)["status"] != "stopping":
        await asyncio.sleep(1)


def _d(msg) -> dict:
    return MessageToDict(msg, preserving_proto_field_name=True)


def _said(items) -> str:
    """The agent's words in these chat items."""
    return " ".join(c.text for i in items if i.HasField("message") and i.message.role == pb.ASSISTANT
                    for c in i.message.content if c.text)


async def _converse(db, result: int, room, agent, scenario: Scenario, tester, voice, path: Path | None,
                    opening: str | None) -> tuple[str, str]:
    """(status, ended) once the conversation is over."""
    from livekit.agents.utils.aio import cancel_and_wait
    from livekit.agents.voice.remote_session import RemoteSession, RemoteSessionEventTypes

    left = asyncio.get_running_loop().create_future()  # 1.7.1 sends no "session ended" event, so the agent leaving is the sign
    room.on("participant_disconnected", lambda p: p.identity == agent.identity and not left.done() and left.set_result(None))
    remote = RemoteSession.from_room(room, agent.identity)
    for name in get_args(RemoteSessionEventTypes):
        remote.on(name, lambda ev, name=name: store.event(db, result, "agent", name, _d(ev)))
    speaking = asyncio.Event()  # the agent started its reply
    remote.on("agent_state_changed", lambda ev: ev.agent_state_changed.new_state == pb.AS_SPEAKING and speaking.set())
    greeted = asyncio.get_running_loop().create_future()  # the agent's first words: its greeting, when it opens
    remote.on("conversation_item_added", lambda ev: (i := ev.conversation_item_added.item).message.role == pb.ASSISTANT
              and not greeted.done() and greeted.set_result(_said([i])))
    await remote.start()
    try:
        try:
            await remote.wait_for_ready(timeout=10)
        except TimeoutError:
            return "no_session_host", "the agent's session host didn't answer in 10 s; it may run with session_host=False"
        for name, ask in (("framework", remote.get_framework_info), ("agent_info", remote.get_agent_info),
                          ("session_state", remote.get_session_state)):
            got = await ask()
            store.event(db, result, "tester", name, _d(got))
        if got.agent_state == pb.AS_SPEAKING:  # session_state, asked last: the agent is mid-greeting already
            speaking.set()
        history = await remote.get_chat_history()
        store.event(db, result, "tester", "chat_history", _d(history))
        me = room.local_participant
        store.event(db, result, "tester", "seen", {"room_metadata": room.metadata, "metadata": me.metadata,
                                                   "attributes": dict(me.attributes),
                                                   "agent_attributes": dict(agent.attributes)})
        greeting = _said(history.items)  # the agent greeted before the tester got here
        if voice:  # the agent keeps its audio; the session host only reports
            talk = _voice(db, result, room, agent, remote, scenario.prompt, scenario.max_turns, tester, voice, path,
                          speaking, greeted, opening, greeting)
        else:
            await remote.update_io(input_audio_enabled=False, output_audio_enabled=False)
            talk = _turns(db, result, remote, tester, scenario.prompt, scenario.max_turns, speaking, greeted, opening,
                          greeting)
        talk = asyncio.create_task(talk)
        stop = asyncio.create_task(_stopping(db, result))
        done, _ = await asyncio.wait({talk, left, stop}, timeout=scenario.max_minutes * 60,
                                     return_when=asyncio.FIRST_COMPLETED)
        await cancel_and_wait(talk, stop)
        if stop in done:
            return "stopped", "stopped by user"
        if talk in done and not talk.exception() and talk.result() == "no_reply":
            await asyncio.wait({left}, timeout=2)  # an agent hanging up mid-turn isn't silent: it's still shutting down
        if left.done():
            return "done", "agent_left"
        return "done", talk.result() if talk in done else "max_minutes"
    finally:
        await remote.aclose()


async def _greeted(speaking: asyncio.Event, greeted: asyncio.Future) -> bool:
    """Whether the agent started its greeting within REPLY_S: speaking, or its words came (an agent without a TTS
    never enters AS_SPEAKING)."""
    spoke = asyncio.ensure_future(speaking.wait())
    done, _ = await asyncio.wait({spoke, greeted}, timeout=checks.REPLY_S, return_when=asyncio.FIRST_COMPLETED)
    spoke.cancel()
    return bool(done)


async def _turns(db, result: int, remote, tester, prompt: str, n: int, speaking: asyncio.Event, greeted,
                 opening: str | None, greeting: str) -> str:
    """How the conversation ended: max_turns, no_greeting (the agent was to open, and didn't), or no_reply once the
    agent doesn't start a reply within REPLY_S. The tester's LLM gets only `prompt` and the agent's words; `opening` is
    its first line, as written. Its usage lands as one `tester_usage` event."""
    from livekit.agents.llm import ChatContext

    if not (opening or greeting):
        if not await _greeted(speaking, greeted):
            return "no_greeting"
        greeting = await greeted
    # The tester's side of the conversation: the agent is "user", the tester is "assistant".
    ctx = ChatContext()
    ctx.add_message(role="system", content=prompt)
    if greeting:
        ctx.add_message(role="user", content=greeting)
    used = Counter()
    try:
        for i in range(n):
            if i == 0 and opening:
                line = opening
            else:
                said = await tester.chat(chat_ctx=ctx).collect()
                line = said.text
                if u := said.usage:  # None from a server that doesn't report it
                    used.update(input_tokens=u.prompt_tokens, input_cached_tokens=u.prompt_cached_tokens,
                                output_tokens=u.completion_tokens)
            store.event(db, result, "tester", "tester_turn", {"text": line})
            ctx.add_message(role="assistant", content=line)
            speaking.clear()
            run, spoke = asyncio.ensure_future(remote.run(line)), asyncio.ensure_future(speaking.wait())
            await asyncio.wait({run, spoke}, timeout=checks.REPLY_S, return_when=asyncio.FIRST_COMPLETED)
            spoke.cancel()
            if not (speaking.is_set() or run.done()):
                run.cancel()
                return "no_reply"
            try:
                items = (await run).items
            except (RuntimeError, TimeoutError):  # the agent gave up on the turn, e.g. its LLM's 429 after its retries
                return "no_reply"
            if reply := _said(items):
                ctx.add_message(role="user", content=reply)
        return "max_turns"
    finally:
        store.event(db, result, "tester", "tester_usage", {"usage": [
            {"kind": "llm", "provider": tester.provider, "model": tester.model, **used}] if used else []})


async def _voice(db, result: int, room, agent, remote, prompt: str, n: int, tester, voice, path: Path | None,
                 speaking: asyncio.Event, greeted: asyncio.Future, opening: str | None, greeting: str) -> str:
    """Like `_turns`, by voice: the tester's own session speaks and listens; `path` gets its recording."""
    from livekit import rtc
    from livekit.agents import Agent, AgentSession, StopResponse, inference, room_io, utils
    from livekit.agents.llm import ChatContext
    from livekit.agents.voice.recorder_io import RecorderIO

    said, replied, turns = asyncio.Queue(), asyncio.Event(), []
    ctx = ChatContext()
    if greeting:
        ctx.add_message(role="user", content=greeting)

    class Tester(Agent):
        async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
            if len(turns) >= n:  # its last turn is said: hear the reply out, say nothing more
                raise StopResponse()

    def item(ev) -> None:
        if ev.item.type != "message":
            return
        if ev.item.role == "assistant":  # added once the tester stopped speaking: the reply gap starts here
            turns.append(ev.item.text_content)
            store.event(db, result, "tester", "tester_turn", {"text": ev.item.text_content})
            speaking.clear()
            replied.clear()
            said.put_nowait(None)
        elif ev.item.role == "user":
            store.event(db, result, "tester", "tester_heard", {"text": ev.item.text_content})  # its STT of the agent

    remote.on("conversation_item_added", lambda ev: ev.conversation_item_added.item.message.role == pb.ASSISTANT
              and replied.set())
    session = AgentSession(stt=voice[0], llm=tester, tts=voice[1], vad=inference.VAD())
    session.on("conversation_item_added", item)
    rec = None
    async with utils.http_context.open():  # plugins outside a worker (LiveKit's documented way)
        try:
            await session.start(Tester(instructions=prompt, chat_ctx=ctx), room=room, record=False,
                                session_host=False,  # else it'd host lk.agent.session itself
                                room_options=room_io.RoomOptions(
                                    participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_AGENT],
                                    participant_identity=agent.identity, text_output=False))
            if path:
                rec = RecorderIO(agent_session=session)  # only starts by itself inside a job, so wired by hand
                session.input.audio = rec.record_input(session.input.audio)
                session.output.audio = rec.record_output(session.output.audio)
                path.parent.mkdir(parents=True, exist_ok=True)
                await rec.start(output_path=path)
            if opening:
                session.say(opening)
            elif greeting:  # it greeted before the tester could hear it
                session.generate_reply()
            elif not await _greeted(speaking, greeted):  # else the session hears it out and replies by itself
                return "no_greeting"
            for _ in range(n):
                await said.get()
                try:
                    await asyncio.wait_for(speaking.wait(), checks.REPLY_S)
                except TimeoutError:
                    return "no_reply"
            await replied.wait()
            return "max_turns"
        finally:
            store.event(db, result, "tester", "tester_usage", {"usage": [
                {"kind": u.type.removesuffix("_usage"), **u.model_dump(exclude={"type"}, exclude_defaults=True)}
                for u in session.usage.model_usage]})
            if rec:
                await rec.aclose()
                store.recorded(db, result, str(path), rec.recording_started_at)
            await session.aclose()
