"""The tester's STT: any OpenAI-compatible server with a realtime (websocket) transcription endpoint. Streaming only:
a batch STT would add seconds to every turn. Outside a worker its websocket needs `utils.http_context.open()`
(LiveKit's documented way for plugins in scripts)."""

import asyncio
import logging


def stt(base_url: str, model: str, key: str | None):
    from livekit.plugins import openai  # slow import, like llm.py

    return openai.STT(base_url=base_url, model=model, api_key=key or "none", use_realtime=True)


async def check(base_url: str, model: str, key: str | None) -> float:
    """0 once the server took 1 s of silence over its websocket. Raises ConnectionError if it can't stream."""
    from livekit import rtc
    from livekit.agents import APIConnectOptions, utils

    quiet = logging.getLogger("livekit.plugins.openai")  # it logs its own "Error in _run"; ours says why
    quiet.disabled = True
    async with utils.http_context.open():
        client = stt(base_url, model, key)
        stream = client.stream(conn_options=APIConnectOptions(max_retry=0))
        for _ in range(50):
            stream.push_frame(rtc.AudioFrame(b"\0" * 960, 24000, 1, 480))
        try:
            # ponytail: no error within 5 s = it streams; a server that takes the websocket then drops audio passes
            await asyncio.wait_for(anext(stream), 5)
        except TimeoutError:
            return 0
        except Exception as e:  # aiohttp's own errors, not APIError
            raise ConnectionError(f"it doesn't stream ({e}); livetest needs a realtime (websocket) STT") from None
        finally:
            await stream.aclose()
            await client.aclose()
            quiet.disabled = False
        return 0  # silence came back as an event: it streams
