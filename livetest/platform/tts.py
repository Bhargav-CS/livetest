"""The tester's TTS: any OpenAI-compatible `/audio/speech` server."""

import time


def tts(base_url: str, model: str, key: str | None, voice_id: str | None = None):
    from livekit.plugins import openai  # slow import, like llm.py

    return openai.TTS(base_url=base_url, model=model, api_key=key or "none", **({"voice": voice_id} if voice_id else {}))


async def check(base_url: str, model: str, key: str | None, voice_id: str | None = None) -> float:
    """Seconds to the first audio. Raises ConnectionError saying why when it doesn't answer."""
    from livekit.agents import APIConnectOptions, APIError

    client = tts(base_url, model, key, voice_id)
    start = time.perf_counter()
    try:
        async with client.synthesize("OK.", conn_options=APIConnectOptions(max_retry=0)) as stream:
            async for _ in stream:
                return time.perf_counter() - start
    except APIError as e:
        raise ConnectionError(e.message) from None
    finally:
        await client.aclose()
    raise ConnectionError("it answered with no audio")
