"""Joining a Hermes session from the page.

The owner is the platform's slash admin, and a connect with ``session=<id>``
sends ``/resume <id> --all`` through the adapter's own message path before the
page is ready. The gateway's reply to that command stays off the page, and a
session that could not be joined closes the connection with its own code.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig
from gateway.slash_access import policy_for_source

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"


def _config(**extra):
    base = {"token": TOKEN, "voicemanager_url": "http://127.0.0.1:1", "host": "127.0.0.1", "port": 0}
    base.update(extra)
    return PlatformConfig(enabled=True, extra=base)


class FakeStore:
    """The gateway's session store, as far as the join needs it: the session each lane key
    points at, and a record of the keys asked for."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.peeked = []

    def peek_session_id(self, session_key):
        self.peeked.append(session_key)
        return self.session_id


class Page:
    """Reads every JSON frame the page gets."""

    def __init__(self, ws):
        self.ws, self.frames = ws, []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            if not isinstance(message, bytes):
                self.frames.append(json.loads(message))

    def of(self, kind):
        return [f for f in self.frames if f.get("type") == kind]


@pytest.fixture
async def fake_vm():
    async with FakeVoiceManager() as fake:
        yield fake


@pytest.fixture
async def adapter(plugin, fake_vm):
    """An adapter whose gateway is a recording handle_message: it notes each event, how many
    VoiceManager connections existed when it ran, and answers a command at once through
    send(), as the gateway's slash dispatch does."""
    adapter = plugin.adapter.VoiceLiveAdapter(_config(voicemanager_url=fake_vm.url))
    messages, vm_connections_at_dispatch = [], []

    async def handle_message(event):
        messages.append(event)
        vm_connections_at_dispatch.append(len(fake_vm.connections))
        if event.text.startswith("/"):
            await adapter.send(event.source.chat_id, adapter.gateway_reply)

    adapter.handle_message = handle_message
    adapter.messages = messages
    adapter.vm_connections_at_dispatch = vm_connections_at_dispatch
    adapter.gateway_reply = "Resumed session abc. Conversation restored."
    assert await adapter.connect()
    yield adapter
    await adapter.disconnect()


def _url(adapter, query):
    return f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&{query}"


async def test_a_connect_with_a_session_sends_resume_before_the_page_is_ready(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]
    (event,) = adapter.messages
    assert event.source.chat_id == "tablet"
    assert event.source.platform == adapter.platform
    # The ready frame follows the VoiceManager connection, which did not exist yet.
    assert adapter.vm_connections_at_dispatch == [0]
    assert adapter._session_store.peeked == [adapter._source_session_key(adapter._voice_source("tablet"))]


async def test_a_connect_without_a_session_sends_no_command(adapter, fake_vm):
    adapter._session_store = FakeStore("whatever")
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert adapter.messages == []


async def test_the_reply_to_the_resume_command_is_swallowed_and_a_later_reply_is_not(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        assert page.of("reply") == []
        result = await adapter.send("tablet", "hello there")
        await wait_for(lambda: page.of("reply"))
        page.task.cancel()
    assert result.success
    assert page.of("reply") == [{"type": "reply", "text": "hello there"}]


async def test_a_session_that_was_not_joined_is_refused_with_its_own_close_code(adapter, fake_vm, plugin):
    adapter._session_store = FakeStore("old")
    adapter.gateway_reply = "Session not found: abc"
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("join_failed"))
        await asyncio.wait_for(ws.wait_closed(), 3)
        page.task.cancel()
    assert ws.close_code == plugin.server.JOIN_FAILED_CLOSE_CODE == 4002
    (failed,) = page.of("join_failed")
    assert failed["message"]
    assert page.of("ready") == []
    assert page.of("reply") == []
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]
    assert fake_vm.connections == []


def test_the_owner_is_the_slash_admin_without_configuration(plugin):
    config = _config()
    adapter = plugin.adapter.VoiceLiveAdapter(config)
    assert config.extra["allow_admin_from"] == [plugin.adapter.OWNER]
    gateway_config = SimpleNamespace(platforms={adapter.platform: config})
    policy = policy_for_source(gateway_config, adapter._voice_source("kitchen"))
    assert policy.enabled
    assert policy.is_admin(plugin.adapter.OWNER)


def test_a_configured_admin_list_is_kept(plugin):
    config = _config(allow_admin_from=["alice"])
    plugin.adapter.VoiceLiveAdapter(config)
    assert config.extra["allow_admin_from"] == ["alice"]
