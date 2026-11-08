"""Stub agent for test_runner.py: canned replies, one 1.5 s tool, leaves on "bye". Spends nothing.

test_runner.py starts it; by hand:

    LIVEKIT_URL=ws://<podman ip>:7880 LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=secret uv run tests/stub_agent.py dev
"""

import asyncio

from livekit.agents import Agent, AgentSession, JobContext, WorkerOptions, cli, function_tool, get_job_context, llm
from livekit.plugins import openai


class Stub(Agent):
    def __init__(self):
        super().__init__(instructions="Stub agent for livetest's runner tests.")

    @function_tool
    async def lookup(self) -> str:
        """Look something up."""
        await asyncio.sleep(1.5)  # a tool with a visible duration
        return "found it"

    async def llm_node(self, chat_ctx, tools, model_settings):
        await asyncio.sleep(0.3)  # pretend thinking
        last = chat_ctx.items[-1]
        text = (last.text_content or "") if last.type == "message" else ""
        if last.type == "function_call_output":
            yield "Done, I found it."
        elif "tool" in text:
            yield llm.ChatChunk(id="c", delta=llm.ChoiceDelta(role="assistant", tool_calls=[
                llm.FunctionToolCall(name="lookup", arguments="{}", call_id=f"call_{len(chat_ctx.items)}")]))
        elif "bye" in text:
            asyncio.get_running_loop().call_later(0.5, get_job_context().shutdown)
            yield "Goodbye."
        else:
            yield "Hello, this is a canned reply."


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    # Fake key: llm_node is overridden, so this LLM is never called. AgentSession skips replies without one.
    await AgentSession(llm=openai.LLM(api_key="not-a-key")).start(agent=Stub(), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, agent_name="livetest-stub"))
