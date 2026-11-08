"""What users create: agents (what to test, how to connect), profiles (who the user is, as the agent sees them) and
scenarios (what the tester tries). Everything saved goes through these models first, from the CLI
or the UI, and the UI's forms can be built from their JSON Schema (`model_json_schema()`).
"""

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

# LiveKit metadata is a string; an object here gets JSON-serialized by livetest.
Metadata = dict[str, Any] | str


def _name(v: str) -> str:
    # Same rule as run ids: names end up in selections (`7`, `1-20x10`), file names and URLs.
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
    url: str | None = Field(None, description="LiveKit server URL; key and secret come from env vars")

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


# What a scenario expects; `type` says which check. Built-in checks (never joined, silent, loops) are always on.
Check = Annotated[ToolCheck | NoToolErrors | Latency, Field(discriminator="type")]


class Scenario(Strict):
    """One test: what the tester wants and how they behave. Global: the run picks the agent and profile."""

    name: Name
    goal: str = Field(min_length=1, description="What the tester is trying to get done")
    tags: list[str] = Field([], description="Groups scenarios, e.g. for `scenario list --tag`")
    persona: dict[str, Any] = Field({}, description="How the tester behaves, e.g. mood, language")
    max_turns: int = Field(10, ge=1)
    max_minutes: float = Field(5, gt=0)
    expect: list[Check] = []
