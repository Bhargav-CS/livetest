import asyncio
import json
import sys
import urllib.request
import uuid

from dotenv import load_dotenv
import os

from livekit import rtc
from livekit.agents import Agent, AgentSession, room_io, utils
from livekit.plugins import deepgram, elevenlabs, silero, groq
import logging

from livetest.integrations.otel import setup_telemetry

load_dotenv()
logging.basicConfig(level=logging.INFO)


# Self-hosted `livekit-server --dev` in podman: fixed dev creds, reached via the
# podman machine IP. Overrides the Cloud values .env holds for agent.py.


INSTRUCTIONS = """You are a recruiter talking to Baymax, Bhargav's voice assistant.
Ask short questions about Bhargav's experience, one at a time."""



def fetch_token(room_name: str) -> tuple[str, str]:
    """Same call fe's TokenSource.developmentTokenServer makes; dispatch rides in the token."""
    req = urllib.request.Request(
        "https://cloud-api.livekit.io/api/v2/sandbox/connection-details",
        data=json.dumps({
            "room_name": room_name,
            "participant_identity": "tester",
            "room_config": {"agents": [{"agent_name": "client-agent"}]},
        }).encode(),
        headers={"X-Sandbox-ID": os.environ["LIVEKIT_SANDBOX_ID"],
                 "Content-Type": "application/json",
                 "User-Agent": "alive-tester"},
    )
    body = json.load(urllib.request.urlopen(req))
    return body["server_url"], body["participant_token"]

async def main(seconds: int) -> None:
    async with utils.http_context.open():
        flush = setup_telemetry("tester")
        room_name = f"tester-{uuid.uuid4().hex[:6]}"

        url, token = fetch_token(room_name)
        room = rtc.Room()
        await room.connect(url, token)
        print(f"joined {room_name}")

        session = AgentSession(
            stt=deepgram.STT(model="nova-3", language="en"),
            llm=groq.LLM(
                model="openai/gpt-oss-120b",),
            tts=elevenlabs.TTS(voice_id="hpp4J3VqNfWAUOO0d1Us"),  # not Baymax's voice
            vad=silero.VAD.load(),
        )
        # Roles are from the tester's side: "assistant" = tester, "user" = Baymax.
        session.on("conversation_item_added",
                lambda ev: print(f"{ev.item.role}: {ev.item.text_content}"))

        await session.start(
            room=room,
            agent=Agent(instructions=INSTRUCTIONS),
            # Baymax joins as an AGENT participant; the default kinds skip agents.
            room_options=room_io.RoomOptions(
                participant_kinds=[rtc.ParticipantKind.PARTICIPANT_KIND_AGENT],
                text_output=False,),
        )
        session.say("Hi Baymax, can you tell me about Bhargav?")  # agent.py never speaks first

        # ponytail: fixed duration; end on goodbye/turn count if runs need to vary
        await asyncio.sleep(seconds)
        await session.aclose()
        await room.disconnect()
        if flush:
            flush()



if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 90))