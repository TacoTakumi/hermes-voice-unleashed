"""The page and its WebSocket are reachable only with the access token."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from gateway.config import PlatformConfig

TOKEN = "s3cret-token"


def _config(**extra):
    base = {"token": TOKEN, "voicemanager_url": "http://127.0.0.1:1", "host": "127.0.0.1", "port": 0}
    base.update(extra)
    return PlatformConfig(enabled=True, extra=base)


@pytest.fixture
async def adapter(plugin):
    adapter = plugin.adapter.VoiceLiveAdapter(_config())
    calls = []

    async def record(connection, client_id):
        calls.append(client_id)
        async for _ in connection:
            pass

    adapter._run_client = record
    adapter.client_calls = calls
    assert await adapter.connect()
    yield adapter
    await adapter.disconnect()


def _ws_url(adapter, token=None, client=None):
    query = []
    if token is not None:
        query.append(f"token={token}")
    if client is not None:
        query.append(f"client={client}")
    return f"ws://127.0.0.1:{adapter.bound_port}/ws" + ("?" + "&".join(query) if query else "")


@pytest.mark.parametrize("token", [None, "", "wrong"])
async def test_websocket_without_the_right_token_is_refused(adapter, token):
    with pytest.raises(InvalidStatus) as info:
        async with connect(_ws_url(adapter, token)):
            pass
    assert info.value.response.status_code == 401
    await asyncio.sleep(0.05)
    assert adapter.client_calls == []


async def test_websocket_with_the_token_reaches_a_client_session(adapter):
    async with connect(_ws_url(adapter, TOKEN, "kitchen")):
        for _ in range(50):
            if adapter.client_calls:
                break
            await asyncio.sleep(0.01)
    assert adapter.client_calls == ["kitchen"]


async def test_page_needs_the_token(adapter):
    base = f"http://127.0.0.1:{adapter.bound_port}/"
    async with httpx.AsyncClient() as http:
        refused = await http.get(base)
        wrong = await http.get(base, params={"token": "nope"})
        page = await http.get(base, params={"token": TOKEN})
    assert refused.status_code == 401
    assert wrong.status_code == 401
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<html" in page.text


async def test_send_reaches_the_connected_page(adapter):
    async with connect(_ws_url(adapter, TOKEN, "phone")) as ws:
        for _ in range(50):
            if adapter.client_calls:
                break
            await asyncio.sleep(0.01)
        result = await adapter.send("phone", "hello there")
        frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
    assert result.success
    assert frame == {"type": "reply", "text": "hello there"}


async def test_connect_refuses_without_a_token(plugin):
    adapter = plugin.adapter.VoiceLiveAdapter(_config(token=""))
    assert not await adapter.connect()


def test_register_adds_one_platform(plugin):
    class Ctx:
        def __init__(self):
            self.platforms, self.hooks = [], []

        def register_platform(self, **kwargs):
            self.platforms.append(kwargs)

        def register_hook(self, name, callback):
            self.hooks.append(name)

    ctx = Ctx()
    plugin.register(ctx)
    assert [p["name"] for p in ctx.platforms] == ["voicemanager_live"]
    entry = ctx.platforms[0]
    assert entry["adapter_factory"] is plugin.adapter.VoiceLiveAdapter
    assert entry["allowed_users_env"] == "VMLIVE_ALLOWED_USERS"
    assert sorted(ctx.hooks) == ["on_session_end", "on_stream_delta", "on_stream_end", "post_tool_call",
                                 "pre_llm_call", "pre_tool_call"]


def test_gateway_tool_progress_is_off_by_default_for_the_voice_platform(plugin, monkeypatch):
    """The activity panel shows tool calls; the gateway's progress messages would show them twice."""
    from dataclasses import fields

    from gateway import display_config
    from gateway.platform_registry import PlatformEntry, platform_registry

    class Ctx:
        def register_platform(self, **kwargs):
            self.entry = kwargs

        def register_hook(self, name, callback):
            pass

    ctx = Ctx()
    plugin.register(ctx)
    assert ctx.entry["display_overrides"] == {"tool_progress": "off"}
    known = {f.name for f in fields(PlatformEntry)}
    entry = PlatformEntry(**{k: v for k, v in ctx.entry.items() if k in known})
    monkeypatch.setattr(platform_registry, "get", lambda name: entry if name == "voicemanager_live" else None)
    assert display_config.resolve_tool_progress({}, "voicemanager_live") == ("off", False)
    configured = {"display": {"platforms": {"voicemanager_live": {"tool_progress": "all"}}}}
    assert display_config.resolve_tool_progress(configured, "voicemanager_live")[0] == "all"


# GET /sessions: the session list the page shows, answered from a callback the adapter supplies.

ROWS = [{"id": "abc123", "title": None, "preview": "hello there", "last_active": 1_700_000_000.0,
         "message_count": 2, "source": "voicemanager_live", "current": True, "voice_attached": False}]


@pytest.fixture
async def sessions_server(plugin):
    """A voice server whose sessions callback is a fake that records the lane it was asked for."""
    calls = []

    async def no_client(connection, client_id):
        pass

    def rows(client_id):
        calls.append(client_id)
        return ROWS

    server = plugin.server.VoiceServer("127.0.0.1", 0, TOKEN, no_client, on_sessions=rows)
    server.sessions_calls = calls
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_sessions_route_answers_the_callbacks_rows_for_the_lane(sessions_server):
    url = f"http://127.0.0.1:{sessions_server.bound_port}/sessions"
    async with httpx.AsyncClient() as http:
        answer = await http.get(url, params={"token": TOKEN, "client": "tablet"})
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "application/json"
    assert answer.headers["cache-control"] == "no-store"
    assert answer.json() == ROWS
    assert sessions_server.sessions_calls == ["tablet"]


async def test_sessions_route_defaults_to_the_phone_lane(sessions_server):
    url = f"http://127.0.0.1:{sessions_server.bound_port}/sessions"
    async with httpx.AsyncClient() as http:
        answer = await http.get(url, params={"token": TOKEN})
    assert answer.status_code == 200
    assert sessions_server.sessions_calls == ["phone"]


@pytest.mark.parametrize("token", [None, "", "wrong"])
async def test_sessions_route_needs_the_token(sessions_server, token):
    url = f"http://127.0.0.1:{sessions_server.bound_port}/sessions"
    params = {"client": "tablet"}
    if token is not None:
        params["token"] = token
    async with httpx.AsyncClient() as http:
        answer = await http.get(url, params=params)
    assert answer.status_code == 401
    assert sessions_server.sessions_calls == []


async def test_sessions_route_without_a_callback_is_not_found(plugin):
    async def no_client(connection, client_id):
        pass

    server = plugin.server.VoiceServer("127.0.0.1", 0, TOKEN, no_client)
    await server.start()
    try:
        url = f"http://127.0.0.1:{server.bound_port}/sessions"
        async with httpx.AsyncClient() as http:
            answer = await http.get(url, params={"token": TOKEN, "client": "tablet"})
    finally:
        await server.stop()
    assert answer.status_code == 404


def test_adapter_sessions_callback_reads_the_runners_store_and_db(plugin, monkeypatch):
    """The adapter lists sessions from the runner's store and state DB for the lane's key.
    The runner holds the DB behind an async facade; the plain DB is what the reader needs."""
    adapter = plugin.adapter.VoiceLiveAdapter(_config())
    store, db = object(), object()
    adapter.gateway_runner = SimpleNamespace(session_store=store, _session_db=SimpleNamespace(_db=db))
    seen = {}

    def fake_rows(store_, db_, lane_key, limit=20):
        seen.update(store=store_, db=db_, lane_key=lane_key)
        return [{"id": "x"}]

    monkeypatch.setattr(plugin.sessions, "session_rows", fake_rows)
    assert adapter._session_rows("tablet") == [{"id": "x", "voice_attached": False}]
    assert seen["store"] is store
    assert seen["db"] is db
    assert seen["lane_key"] == adapter._source_session_key(adapter._voice_source("tablet"))
    assert seen["lane_key"] != adapter._source_session_key(adapter._voice_source("phone"))


def test_adapter_sessions_callback_marks_the_row_whose_session_has_voice_attached(plugin, monkeypatch):
    """One voice device per session: the row of a session a UI client has voice
    attached to carries voice_attached true, every other row false."""
    adapter = plugin.adapter.VoiceLiveAdapter(_config())
    adapter.gateway_runner = SimpleNamespace(session_store=object(), _session_db=SimpleNamespace(_db=object()))
    adapter._attached["b"] = object()

    def fake_rows(store_, db_, lane_key, limit=20):
        return [{"id": "a", "current": True}, {"id": "b", "current": False}, {"id": "c", "current": False}]

    monkeypatch.setattr(plugin.sessions, "session_rows", fake_rows)
    rows = adapter._session_rows("tablet")
    assert [(row["id"], row["voice_attached"]) for row in rows] == [("a", False), ("b", True), ("c", False)]
    assert rows[0]["current"] is True  # the other fields stay as the reader gave them


def test_adapter_sessions_callback_opens_the_state_db_read_only_without_a_runner_db(plugin, monkeypatch):
    adapter = plugin.adapter.VoiceLiveAdapter(_config())
    adapter.gateway_runner = SimpleNamespace(session_store=object())
    opened = []

    class FakeDB:
        def __init__(self, db_path=None, read_only=False):
            opened.append(read_only)
            self.closed = False

        def close(self):
            self.closed = True

    seen = {}

    def fake_rows(store_, db_, lane_key, limit=20):
        seen["db"] = db_
        return []

    monkeypatch.setattr(plugin.adapter, "SessionDB", FakeDB)
    monkeypatch.setattr(plugin.sessions, "session_rows", fake_rows)
    assert adapter._session_rows("tablet") == []
    assert opened == [True]
    assert isinstance(seen["db"], FakeDB) and seen["db"].closed


async def test_adapter_wires_the_sessions_callback_into_the_server(plugin, monkeypatch):
    adapter = plugin.adapter.VoiceLiveAdapter(_config())

    class FakeServer:
        def __init__(self, host, port, token, on_client, **kwargs):
            self.kwargs = kwargs

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(plugin.adapter, "VoiceServer", FakeServer)
    assert await adapter.connect()
    try:
        assert adapter._server.kwargs["on_sessions"] == adapter._session_rows
    finally:
        await adapter.disconnect()
