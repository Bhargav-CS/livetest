"""What users create: agents (what to test, how to connect), profiles (who the user is, as the agent sees them) and
scenarios (the tester's prompt and what to check). Everything saved goes through these models first, from the CLI
or the UI, and the UI's forms can be built from their JSON Schema (`model_json_schema()`).
"""

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

# LiveKit metadata is a string; an object here gets JSON-serialized by livetest.
Metadata = dict[str, Any] | str


def _name(v: str) -> str:
    # Names end up in selections (`7`, `1-20x10`), file names and URLs; a leading letter keeps them apart from numbers.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,63}", v):
        raise ValueError(f"'{v}' isn't a valid name: start with a letter, then letters, digits, - _ . (up to 64)")
    return v


Name = Annotated[str, AfterValidator(_name)]


class Strict(BaseModel):
    # Unknown keys are errors, so a typo like "metdata" is flagged instead of silently ignored.
    model_config = ConfigDict(extra="forbid")


class Agent(Strict):
    """An agent under test and how to get a token for it. Secrets never go here.

    No connection set means a raw token is passed at run time.
    """

    name: str = Field(min_length=1, description="The agent's LiveKit agent_name; livetest dispatches it by this name")
    sandbox: str | None = Field(None, description="LiveKit sandbox token server ID")
    endpoint: str | None = Field(None, description="Your token endpoint URL")
    url: str | None = Field(None, description="LiveKit server URL; livetest makes tokens with the agent's key and secret")

    @model_validator(mode="after")
    def one_connection(self):
        if sum(x is not None for x in (self.sandbox, self.endpoint, self.url)) > 1:
            raise ValueError("pick only one of sandbox, endpoint, url")
        return self


class Room(Strict):
    metadata: Metadata | None = Field(None, description="Agent reads it as ctx.room.metadata")


class Participant(Strict):
    identity: str = "tester"
    name: str | None = None
    metadata: Metadata | None = Field(None, description="Agent reads it as participant.metadata")
    attributes: dict[str, str] = Field({}, description="Agent reads it as participant.attributes")


class Dispatch(Strict):
    metadata: Metadata | None = Field(None, description="Agent reads it as ctx.job.metadata")


class Profile(Strict):
    """Who the user is, as the agent sees them: room, participant and job metadata.

    Global, not tied to an agent: any scenario of any agent can use one. Not called "session"
    because LiveKit already uses that for the agent's AgentSession.
    """

    name: Name = Field(description="What `run start --profile` calls it")
    room: Room = Room()
    participant: Participant = Participant()
    dispatch: Dispatch = Dispatch()


class ToolCheck(Strict):
    type: Literal["tool_called", "tool_not_called"]
    name: str = Field(description="The tool's name")
    args: dict[str, Any] = Field({}, description="Expected arguments")


class NoToolErrors(Strict):
    type: Literal["no_tool_errors"]


class Latency(Strict):
    type: Literal["latency"]
    p95_ms: int | None = Field(None, gt=0, description="Reply latency limit, 95th percentile")
    p99_ms: int | None = Field(None, gt=0, description="Reply latency limit, 99th percentile")

    @model_validator(mode="after")
    def some_limit(self):
        if self.p95_ms is None and self.p99_ms is None:
            raise ValueError("a latency check needs p95_ms or p99_ms")
        return self


# What a scenario expects; `type` says which check. These are the only checks: livetest adds none of its own.
Check = Annotated[ToolCheck | NoToolErrors | Latency, Field(discriminator="type")]


class Plugin(Strict):
    """What the tester thinks, hears (voice mode) or speaks with: any OpenAI-compatible server. One per kind is active.
    Its key is never stored here."""

    name: Name = Field(description="What `plugin use --plugin` calls it")
    kind: Literal["llm", "stt", "tts"] = "llm"
    base_url: str = Field(min_length=1, description="The server's OpenAI-compatible base URL")
    model: str = Field(min_length=1)
    voice_id: str | None = Field(None, description="TTS only: the voice to speak with")

    @model_validator(mode="after")
    def _voice_is_tts(self):
        if self.voice_id and self.kind != "tts":
            raise ValueError("only a tts plugin has a voice id")
        return self


def key_var(name: str, what: str = "KEY") -> str:
    """The env var holding a secret: plugin my-groq → LIVETEST_KEY_MY_GROQ; agents use AGENT_KEY / AGENT_SECRET."""
    return f"LIVETEST_{what}_" + re.sub(r"\W", "_", name).upper()


class Scenario(Strict):
    """One test: the tester's prompt and what to check. Global: the run picks the agent and profile."""

    name: Name
    prompt: str = Field(min_length=1, description="The tester LLM's whole system prompt, sent as written")
    tags: list[str] = Field([], description="Groups scenarios, e.g. for `scenario list --tag`")
    max_turns: int = Field(10, ge=1)
    max_minutes: float = Field(5, gt=0)
    expect: list[Check] = []
