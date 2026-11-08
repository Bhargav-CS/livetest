"""One text-mode test: the tester LLM talks to the agent through the agent's session host.

Everything lands in the DB as it happens: the agent's session-host events, what the tester said, what the agent
showed (prompt, tools, session options, what it saw of the tester) and log lines.
"""

import asyncio
import contextvars
import json
import logging
from contextlib import contextmanager
from typing import get_args

import jwt
from google.protobuf.json_format import MessageToDict

from livetest.platform import llm
from livetest.runtime import session, store
from livetest.runtime.models import Profile, Scenario

log = logging.getLogger("livetest.runner")
_current = contextvars.ContextVar("result", default=None)  # the test a log line belongs to

TESTER = """You are playing a user of a voice agent, to test it. Stay in character; never say you are testing.
Your goal: {goal}
{persona}Reply with only what you say next: one short turn."""


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


async def run_text(db, scenario: Scenario, url: str, token: str, tester, profile: Profile | None = None,
                    key: str | None = None, secret: str | None = None) -> int:
    """Run one scenario against the agent the token dispatches; returns its result's id. `tester` is the tester's
    LLM (the caller closes it); with the API key the room is deleted after. A failing test doesn't raise: the
    result's status says what happened.
    """
    room_name = jwt.decode(token, options={"verify_signature": False}).get("video", {}).get("room", "")
    result = store.start_result(db, scenario, profile, room_name)
    status, ended = "error", None
    with logged(db, result):
        try:
            async with session.joined(url, token, key, secret) as (room, agent):
                log.info("agent %s joined room %s", agent.identity, room_name)
                status, ended = await _converse(db, result, room, agent, scenario, tester)
        except ConnectionError as e:
            status, ended = "never_joined", str(e)
        except Exception as e:
            log.exception("test failed")
            ended = str(e) or type(e).__name__
        finally:
            store.finish_result(db, result, status, ended)
    return result


def _d(msg) -> dict:
    return MessageToDict(msg, preserving_proto_field_name=True)


def _said(items) -> str:
    """The agent's words in these chat items."""
    from livekit.protocol.agent_pb import agent_session as pb

    return " ".join(c.text for i in items if i.HasField("message") and i.message.role == pb.ASSISTANT
                    for c in i.message.content if c.text)


async def _converse(db, result: int, room, agent, scenario: Scenario, tester) -> tuple[str, str]:
    """(status, ended) once the conversation is over."""
    from livekit.agents.llm import ChatContext
    from livekit.agents.utils.aio import cancel_and_wait
    from livekit.agents.voice.remote_session import RemoteSession, RemoteSessionEventTypes

    left = asyncio.get_running_loop().create_future()  # 1.7.1 sends no "session ended" event, so the agent leaving is the sign
    room.on("participant_disconnected", lambda p: p.identity == agent.identity and not left.done() and left.set_result(None))
    remote = RemoteSession.from_room(room, agent.identity)
    for name in get_args(RemoteSessionEventTypes):
        remote.on(name, lambda ev, name=name: store.event(db, result, "agent", name, _d(ev)))
    await remote.start()
    try:
        try:
            await remote.wait_for_ready(timeout=10)
        except TimeoutError:
            return "no_session_host", "the agent's session host didn't answer in 10 s; it may run with session_host=False"
        for name, ask in (("framework", remote.get_framework_info), ("agent_info", remote.get_agent_info),
                          ("session_state", remote.get_session_state)):
            store.event(db, result, "tester", name, _d(await ask()))
        history = await remote.get_chat_history()
        store.event(db, result, "tester", "chat_history", _d(history))
        me = room.local_participant
        store.event(db, result, "tester", "seen", {"room_metadata": room.metadata, "metadata": me.metadata,
                                                   "attributes": dict(me.attributes),
                                                   "agent_attributes": dict(agent.attributes)})
        await remote.update_io(input_audio_enabled=False, output_audio_enabled=False)

        # The tester's side of the conversation: the agent is "user", the tester is "assistant".
        ctx = ChatContext()
        persona = f"How you behave: {json.dumps(scenario.persona)}\n" if scenario.persona else ""
        ctx.add_message(role="system", content=TESTER.format(goal=scenario.goal, persona=persona))
        ctx.add_message(role="user", content=_said(history.items) or "(the call connected; you speak first)")

        talk = asyncio.create_task(_turns(db, result, remote, tester, ctx, scenario.max_turns))
        done, _ = await asyncio.wait({talk, left}, timeout=scenario.max_minutes * 60,
                                     return_when=asyncio.FIRST_COMPLETED)
        await cancel_and_wait(talk)
        if left.done():
            return "done", "agent_left"
        return "done", talk.result() if talk in done else "max_minutes"
    finally:
        await remote.aclose()


async def _turns(db, result: int, remote, tester, ctx, n: int) -> str:
    for _ in range(n):
        line = await llm.reply(tester, ctx)
        store.event(db, result, "tester", "tester_turn", {"text": line})
        ctx.add_message(role="assistant", content=line)
        ctx.add_message(role="user", content=_said((await remote.run(line)).items) or "(no reply)")
    return "max_turns"
