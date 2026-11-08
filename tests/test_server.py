"""The page and its WebSocket are reachable only with the access token."""

from __future__ import annotations

import asyncio
import json

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
