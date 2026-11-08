"""Tokens from all four sources, with the profile applied or checked. Offline against a local token endpoint.

Live (deployed Baymax on LiveKit Cloud; joins a fresh room and waits for the agent), skipped unless LIVETEST_LIVE_AGENT is set:

    $env:LIVETEST_LIVE_AGENT="ALIVE"; uv run --env-file .env --env-file D:/alive/alive/.env pytest tests/test_tokens.py
"""

import asyncio
import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import jwt
import pytest
from google.protobuf.json_format import ParseDict
from livekit import api

from livetest.runtime import session, tokens
from livetest.runtime.models import Agent, Profile, key_var

KEY, SECRET = "devkey", "a-test-secret-long-enough-for-hs256"
PRO = Profile(name="pro", room={"metadata": {"tenant": "acme"}}, dispatch={"metadata": {"job": 1}},
              participant={"name": "Priya", "metadata": {"user_id": 42}, "attributes": {"plan": "pro"}})
ALIVE = Agent(name="ALIVE")


def honest(body, key=KEY, secret=SECRET, url="wss://test"):
    """What a token server does with LiveKit's token-endpoint request."""
    t = (api.AccessToken(key, secret).with_identity(body["participant_identity"])
         .with_name(body.get("participant_name", "")).with_metadata(body.get("participant_metadata", ""))
         .with_attributes(body.get("participant_attributes", {}))
         .with_grants(api.VideoGrants(room_join=True, room=body["room_name"]))
         .with_room_config(ParseDict(body.get("room_config", {}), api.RoomConfiguration())))
    return 200, {"server_url": url, "participant_token": t.to_jwt()}


@pytest.fixture
def endpoint():
    """A local token endpoint. `.reply(body) -> (status, json)` answers; `.seen` holds (headers, body) per request."""
    ns = SimpleNamespace(seen=[], reply=honest)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            ns.seen.append((self.headers, body))
            status, out = ns.reply(body)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ns.url = f"http://127.0.0.1:{srv.server_port}/token"
    yield ns
    srv.shutdown()


def raw(**claims):
    """A token someone else made: dispatches ALIVE unless roomConfig says otherwise."""
    return jwt.encode({"sub": "tester", "exp": time.time() + 600, "roomConfig": {"agents": [{"agentName": "ALIVE"}]}}
                      | claims, SECRET)


def test_api_key_mode_applies_the_profile():
    url, token = tokens.connect(Agent(name="ALIVE", url="ws://lk:7880"), "room-1", PRO, key=KEY, secret=SECRET)
    c = jwt.decode(token, SECRET, algorithms=["HS256"])  # signed with the agent's secret
    assert url == "ws://lk:7880"
    assert (c["sub"], c["name"], json.loads(c["metadata"]), c["attributes"]) == ("tester", "Priya", {"user_id": 42},
                                                                                  {"plan": "pro"})
    assert (c["video"]["roomJoin"], c["video"]["room"], c["video"]["canUpdateOwnMetadata"]) == (True, "room-1", True)
    assert json.loads(c["roomConfig"]["metadata"]) == {"tenant": "acme"}
    assert [(a["agentName"], json.loads(a["metadata"])) for a in c["roomConfig"]["agents"]] == [("ALIVE", {"job": 1})]


def test_api_key_mode_needs_the_keys():
    with pytest.raises(ValueError, match="LIVETEST_AGENT_KEY_MY_AGENT"):
        tokens.connect(Agent(name="my-agent", url="ws://lk:7880"), "r", PRO)


def test_endpoint_gets_the_profile(endpoint):
    url, token = tokens.connect(Agent(name="ALIVE", endpoint=endpoint.url), "room-1", PRO)
    assert url == "wss://test" and jwt.decode(token, SECRET, algorithms=["HS256"])["attributes"] == {"plan": "pro"}
    body = endpoint.seen[0][1]
    assert body["room_name"] == "room-1" and body["participant_identity"] == "tester"
    assert body["room_config"]["agents"][0]["agent_name"] == "ALIVE"  # LiveKit's snake_case body
    assert json.loads(body["participant_metadata"]) == {"user_id": 42}


def test_sandbox_is_the_endpoint_with_its_id(endpoint, monkeypatch):
    monkeypatch.setattr(tokens, "SANDBOX", endpoint.url)
    tokens.connect(Agent(name="ALIVE", sandbox="sb-1"), "room-1")
    headers = endpoint.seen[0][0]
    assert headers["X-Sandbox-ID"] == "sb-1"
    assert "urllib" not in headers["User-Agent"]  # Cloudflare 403s urllib's default


def test_endpoint_refusal_shows_its_message(endpoint):
    endpoint.reply = lambda body: (400, {"error": "attributes not allowed"})
    with pytest.raises(ValueError, match="400.*attributes not allowed"):
        tokens.connect(Agent(name="ALIVE", endpoint=endpoint.url), "r", PRO)


def test_endpoint_that_drops_a_field_fails(endpoint):
    endpoint.reply = lambda body: honest({k: v for k, v in body.items() if k != "participant_attributes"})
    with pytest.raises(ValueError, match="participant.attributes.plan"):
        tokens.connect(Agent(name="ALIVE", endpoint=endpoint.url), "r", PRO)


def test_unreachable_endpoint():
    with socket.socket() as s:  # a port nothing listens on once closed
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}/token"
    with pytest.raises(ConnectionError):
        tokens.connect(Agent(name="ALIVE", endpoint=dead), "r")


def test_raw_token_used_as_given():
    token = raw(name="Priya", metadata='{"user_id":42}', attributes={"plan": "pro", "extra": "x"},
                roomConfig={"metadata": '{"tenant": "acme"}', "agents": [{"agentName": "ALIVE", "metadata": '{"job":1}'}]})
    assert tokens.connect(ALIVE, "r", PRO, token=token, url="wss://x") == ("wss://x", token)  # JSON compared parsed
    assert tokens.connect(Agent(name="ALIVE", url="ws://saved"), "r", token=token)[0] == "ws://saved"
    assert tokens.connect(ALIVE, "r", token=raw(sub="bob"), url="wss://x")  # no profile: identity not checked
    with pytest.raises(ValueError, match="LIVEKIT_URL"):
        tokens.connect(ALIVE, "r", token=token)  # no server URL anywhere


@pytest.mark.parametrize("token, error", [
    (raw(attributes={"plan": "free"}), "participant.attributes.plan"),
    (raw(attributes={"plan": "pro"}, metadata='{"user_id": 7}'), "participant.metadata"),
    (raw(exp=time.time() - 60), "expired.*--token"),
    (raw(roomConfig={"agents": [{"agentName": "other"}]}), "doesn't dispatch 'ALIVE'"),
    (raw(roomConfig={}), "doesn't dispatch 'ALIVE'"),
    ("not-a-token", "isn't a LiveKit token"),
])
def test_raw_token_checked(token, error):
    with pytest.raises(ValueError, match=error):
        tokens.connect(ALIVE, "r", PRO, token=token, url="wss://x")


def test_no_token_source():
    with pytest.raises(ValueError, match="token"):
        tokens.connect(ALIVE, "r")


# Live: deployed Baymax on LiveKit Cloud. Keys come from Alive's .env via --env-file, never copied here.
LIVE = os.getenv("LIVETEST_LIVE_AGENT")
LK_URL, LK_KEY, LK_SECRET, SANDBOX_ID = (os.getenv(v) for v in (
    "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_SANDBOX_ID"))
live = pytest.mark.skipif(not LIVE, reason="set LIVETEST_LIVE_AGENT (and Alive's .env)")
cloud = pytest.mark.skipif(not (LK_URL and LK_KEY and LK_SECRET), reason="needs LIVEKIT_URL / API_KEY / API_SECRET")


def joins(url, token):
    """Join, wait for the agent the token dispatched, and return what the room shows the tester."""
    async def go():
        # With the key the room is deleted after, which ends the agent's job now (Cloud caps concurrent jobs).
        async with session.joined(url, token, LK_KEY, LK_SECRET) as (room, _):
            me = room.local_participant
            return json.loads(room.metadata), json.loads(me.metadata), dict(me.attributes)

    return asyncio.run(go())


def room():
    return f"livetest-{uuid.uuid4().hex[:8]}"  # fresh, so room metadata applies


def seen_as_pro(url, token):
    assert joins(url, token) == ({"tenant": "acme"}, {"user_id": 42}, {"plan": "pro"})


@live
@cloud
def test_live_api_key_and_raw():
    agent = Agent(name=LIVE, url=LK_URL)
    url, token = tokens.connect(agent, room(), PRO, key=LK_KEY, secret=LK_SECRET)
    assert tokens.connect(Agent(name=LIVE), "-", PRO, token=token, url=url) == (url, token)  # same token, raw path
    seen_as_pro(url, token)


@live
@cloud
def test_live_agent_add_joins(tmp_path, monkeypatch):
    from livetest.cli import main

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    for name in (LIVE, "nobody-livetest"):  # keys from env, as in CI; rooms deleted after
        monkeypatch.setenv(key_var(name, "AGENT_KEY"), LK_KEY)
        monkeypatch.setenv(key_var(name, "AGENT_SECRET"), LK_SECRET)
    db = str(tmp_path / "livetest.db")
    assert main(["--db", db, "agent", "add", "--name", LIVE, "--url", LK_URL]) == 0
    assert main(["--db", db, "agent", "add", "--name", "nobody-livetest", "--url", LK_URL]) == 3  # never joins: 30 s


@live
@pytest.mark.skipif(not SANDBOX_ID, reason="needs LIVEKIT_SANDBOX_ID")
def test_live_sandbox():
    seen_as_pro(*tokens.connect(Agent(name=LIVE, sandbox=SANDBOX_ID), room(), PRO))


@live
@cloud
def test_live_endpoint(endpoint):
    endpoint.reply = lambda body: honest(body, LK_KEY, LK_SECRET, LK_URL)  # stands in for a user's token endpoint
    seen_as_pro(*tokens.connect(Agent(name=LIVE, endpoint=endpoint.url), room(), PRO))
