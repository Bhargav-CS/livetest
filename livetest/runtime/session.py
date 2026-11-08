"""Being in the agent's room: join, wait for the agent the token dispatched, leave."""

import asyncio
import time

from livekit import api


async def joins(url: str, token: str, key: str | None = None, secret: str | None = None, wait: float = 30) -> float:
    """Seconds until the agent joined. Leaves right after; with the API key it also deletes the room, which frees
    the agent's job at once (Cloud caps concurrent jobs). Raises ConnectionError if the tester or the agent can't join.
    """
    from livekit import rtc  # slow to import, and most commands never need it

    room = rtc.Room()
    came = asyncio.get_running_loop().create_future()

    def seen(p: rtc.RemoteParticipant) -> None:
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT and not came.done():
            came.set_result(None)

    room.on("participant_connected", seen)
    start = time.perf_counter()
    try:
        await room.connect(url, token)
    except rtc.ConnectError as e:
        raise ConnectionError(f"couldn't join {url} ({e.message}); check the URL, key and secret") from None
    try:
        for p in room.remote_participants.values():
            seen(p)
        await asyncio.wait_for(came, wait)
        return time.perf_counter() - start
    except TimeoutError:
        raise ConnectionError(f"the agent never joined within {wait:.0f} s; check its name, that it's deployed, "
                              "and that it isn't at its concurrent-job limit") from None
    finally:
        await room.disconnect()
        if key and secret:
            async with api.LiveKitAPI(url.replace("ws", "http", 1), key, secret) as lk:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room.name))
