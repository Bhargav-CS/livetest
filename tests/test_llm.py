"""Live: the tester LLM against a real OpenAI-compatible server (private-ai's vLLM in dev). Skipped unless set:

    LIVETEST_LIVE_LLM_URL=http://<MACHINE_HOST>:8000/v1 LIVETEST_LIVE_LLM_MODEL=Qwen/Qwen3.5-2B uv run pytest tests/test_llm.py
"""

import asyncio
import os

import pytest

from livetest.cli import main
from livetest.platform import llm

URL, MODEL, KEY = (os.getenv(f"LIVETEST_LIVE_LLM_{v}") for v in ("URL", "MODEL", "KEY"))
pytestmark = pytest.mark.skipif(not URL, reason="set LIVETEST_LIVE_LLM_URL and LIVETEST_LIVE_LLM_MODEL")


def test_reply_streams():
    from livekit.agents.llm import ChatContext

    async def chunks():
        model = llm.llm(URL, MODEL, KEY)
        ctx = ChatContext()
        ctx.add_message(role="user", content="Count from 1 to 20.")
        try:
            async with model.chat(chat_ctx=ctx) as stream:
                return [c async for c in stream if c.delta and c.delta.content]
        finally:
            await model.aclose()

    assert len(asyncio.run(chunks())) > 1  # many pieces, not one finished reply


def test_plugin_add_checks_for_real(tmp_path, monkeypatch):
    if KEY:
        monkeypatch.setenv("LIVETEST_KEY_LIVE", KEY)
    assert main(["--db", str(tmp_path / "t.db"), "plugin", "add", "--plugin", "live",
                 "--base-url", URL, "--model", MODEL]) == 0
