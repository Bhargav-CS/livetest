"""Being in the agent's room: join, wait for the agent the token dispatched, leave."""

import asyncio
import time
from contextlib import asynccontextmanager

from livekit import api


@asynccontextmanager
async def joined(url: str, token: str, key: str | None = None, secret: str | None = None, wait: float = 30):
    """(room, agent participant) once the agent joined. Leaves on exit; with the API key it also deletes the room,
    which frees the agent's job at once (Cloud caps concurrent jobs). Raises ConnectionError if the tester or the
    agent can't join.
    """
    from livekit import rtc  # slow to import, and most commands never need it

    room = rtc.Room()
    room.register_text_stream_handler("lk.transcription", lambda reader, identity: None)  # else "ignoring" log spam
    came = asyncio.get_running_loop().create_future()

    def seen(p: rtc.RemoteParticipant) -> None:
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT and not came.done():
            came.set_result(p)

    room.on("participant_connected", seen)
    try:
        await room.connect(url, token)
    except rtc.ConnectError as e:
        raise ConnectionError(f"couldn't join {url} ({e.message}); check the URL, key and secret") from None
    try:
        for p in room.remote_participants.values():
            seen(p)
        try:
            agent = await asyncio.wait_for(came, wait)
        except TimeoutError:
            raise ConnectionError(f"the agent never joined within {wait:.0f} s; check its name, that it's deployed, "
                                  "and that it isn't at its concurrent-job limit") from None
        yield room, agent
    finally:
        await room.disconnect()
        if key and secret:
            async with api.LiveKitAPI(url.replace("ws", "http", 1), key, secret) as lk:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room.name))


async def joins(url: str, token: str, key: str | None = None, secret: str | None = None, wait: float = 30) -> float:
    """Seconds until the agent joined, then leaves (see `joined`)."""
    start = time.perf_counter()
    async with joined(url, token, key, secret, wait):
        return time.perf_counter() - start
