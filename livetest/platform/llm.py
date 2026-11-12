"""The tester's LLM: any OpenAI-compatible server through LiveKit's OpenAI plugin, which always streams.

The same object talks in text mode and thinks inside the tester's voice AgentSession.
"""

import time

# Base URLs for --provider; anything else is the user's own server (vLLM, Ollama, LM Studio, ...).
PRESETS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}


def llm(base_url: str, model: str, key: str | None):
    from livekit.plugins import openai  # imported here: it's slow, and most commands never need it

    return openai.LLM(base_url=base_url, model=model, api_key=key or "none")  # local servers take any key


async def check(base_url: str, model: str, key: str | None) -> float:
    """Seconds to the first streamed token. Raises ConnectionError saying why when it doesn't answer."""
    from livekit.agents import APIConnectOptions, APIError
    from livekit.agents.llm import ChatContext

    ctx = ChatContext()
    ctx.add_message(role="user", content="Reply with OK.")
    client = llm(base_url, model, key)
    start = time.perf_counter()
    try:
        async with client.chat(chat_ctx=ctx, conn_options=APIConnectOptions(max_retry=0)) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    return time.perf_counter() - start
    except APIError as e:
        raise ConnectionError(e.message) from None
    finally:
        await client.aclose()
    raise ConnectionError("it answered with no text")
