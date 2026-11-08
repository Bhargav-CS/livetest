"""How the tester gets in: a token for a room that dispatches the agent, with the profile applied.

Four sources: the agent's API key (livetest makes the token), a LiveKit sandbox, the user's token endpoint
(both take LiveKit's token-endpoint request), or a raw token passed at run time. A token livetest didn't make
is checked against the profile, since its source may have ignored or changed fields.
"""

import json
import time
import urllib.error
import urllib.request

import jwt
from google.protobuf.json_format import MessageToDict
from livekit import api

from livetest.runtime.models import Agent, Profile, key_var

SANDBOX = "https://cloud-api.livekit.io/api/v2/sandbox/connection-details"


def text(m: dict | str | None) -> str | None:
    """LiveKit metadata is a string; a profile's object becomes JSON."""
    return json.dumps(m) if isinstance(m, dict) else m


def same(want, got: str | None) -> bool:
    """A profile value against the token's string; JSON objects compare parsed, so spacing doesn't matter."""
    if not isinstance(want, dict):
        return want == got
    try:
        return json.loads(got or "null") == want
    except ValueError:
        return False


def connect(agent: Agent, room: str, profile: Profile | None = None, *, token: str | None = None,
            url: str | None = None, key: str | None = None, secret: str | None = None) -> tuple[str, str]:
    """(server URL, token) for joining `room` as `profile`. A raw `token` beats the agent's saved source; its
    `url` comes from the caller (flag, LIVEKIT_URL) or the agent's --url.

    Raises ValueError for bad input or a token that doesn't match, ConnectionError when the source can't be reached.
    """
    profile = profile or Profile(name="default")
    p = profile.participant
    config = api.RoomConfiguration(metadata=text(profile.room.metadata), agents=[
        api.RoomAgentDispatch(agent_name=agent.name, metadata=text(profile.dispatch.metadata))])
    if token:
        if not (url := url or agent.url):
            raise ValueError("a raw token needs the server URL: set LIVEKIT_URL or save the agent with --url")
    elif agent.url:
        if not (key and secret):
            raise ValueError(f"no API key and secret for '{agent.name}': add the agent again at a terminal, or set "
                             f"{key_var(agent.name, 'AGENT_KEY')} and {key_var(agent.name, 'AGENT_SECRET')}")
        grants = api.VideoGrants(room_join=True, room=room, can_update_own_metadata=True)  # attributes mid-run, later
        return agent.url, (api.AccessToken(key, secret).with_identity(p.identity).with_name(p.name or "")
                           .with_metadata(text(p.metadata) or "").with_attributes(p.attributes)
                           .with_grants(grants).with_room_config(config).to_jwt())
    elif agent.sandbox or agent.endpoint:
        body = {k: v for k, v in {
            "room_name": room, "participant_identity": p.identity, "participant_name": p.name,
            "participant_metadata": text(p.metadata), "participant_attributes": p.attributes or None,
            "room_config": MessageToDict(config, preserving_proto_field_name=True),
        }.items() if v is not None}
        url, token = fetch(agent.endpoint or SANDBOX, body, {} if agent.endpoint else {"X-Sandbox-ID": agent.sandbox})
    else:
        raise ValueError(f"'{agent.name}' has no token source: pass a token, or add it with --sandbox, --endpoint or --url")
    check(token, agent, profile)
    return url, token


def fetch(url: str, body: dict, headers: dict) -> tuple[str, str]:
    """POST LiveKit's token-endpoint request; returns (server_url, participant_token)."""
    # Cloudflare (in front of the sandbox) 403s urllib's default User-Agent.
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "livetest", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            got = json.load(r)
        return got["server_url"], got["participant_token"]
    except urllib.error.HTTPError as e:
        raise ValueError(f"{url} refused the token request ({e.code}): {e.read().decode(errors='replace')[:300]}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ConnectionError(f"couldn't reach {url} ({getattr(e, 'reason', e)})") from None
    except (ValueError, KeyError, TypeError):
        raise ValueError(f"{url} didn't answer with server_url and participant_token") from None


def check(token: str, agent: Agent, profile: Profile) -> None:
    """Fails unless the token is unexpired, dispatches the agent and carries every field the profile sets."""
    try:
        c = jwt.decode(token, options={"verify_signature": False})  # no secret here; the server verifies it
    except jwt.PyJWTError as e:
        raise ValueError(f"that isn't a LiveKit token ({e})") from None
    if c.get("exp", float("inf")) < time.time():
        raise ValueError(f"the token expired at {time.strftime('%Y-%m-%d %H:%M', time.localtime(c['exp']))}; pass a new one with --token")
    cfg = c.get("roomConfig") or {}
    if not (d := [a for a in cfg.get("agents", []) if a.get("agentName") == agent.name]):
        raise ValueError(f"the token doesn't dispatch '{agent.name}'"
                         + (f"; it dispatches {[a.get('agentName') for a in cfg['agents']]}" if cfg.get("agents") else ""))
    has = {"room": {"metadata": cfg.get("metadata")}, "dispatch": {"metadata": d[0].get("metadata")},
           "participant": {"identity": c.get("sub"), "name": c.get("name"), "metadata": c.get("metadata"),
                           "attributes": c.get("attributes") or {}}}
    wrong = []
    for part, fields in profile.model_dump(exclude_defaults=True, exclude={"name"}).items():  # only what it sets
        for field, want in fields.items():
            got = has[part][field]
            pairs = [(f"{field}.{k}", v, got.get(k)) for k, v in want.items()] if field == "attributes" else [(field, want, got)]
            wrong += [f"{part}.{f}: profile has {w!r}, token has {g!r}" for f, w, g in pairs if not same(w, g)]
    if wrong:
        raise ValueError("the token doesn't match the profile: " + "; ".join(wrong))
