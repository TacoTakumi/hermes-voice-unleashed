"""Each page gets its own VoiceManager connection, opted in to the turn engine."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"


@pytest.fixture
async def fake_vm():
    async with FakeVoiceManager() as fake:
        yield fake


@pytest.fixture
async def adapter(plugin, fake_vm):
    adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
        "token": TOKEN, "voicemanager_url": fake_vm.url, "host": "127.0.0.1", "port": 0}))
    assert await adapter.connect()
    yield adapter
    await adapter.disconnect()


def _page_url(adapter, client):
    return f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client={client}"


async def _ready(ws) -> dict:
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), 3))
        if frame.get("type") == "ready":
            return frame


async def test_two_pages_get_two_connections_opted_in_before_audio(adapter, fake_vm):
    async with connect(_page_url(adapter, "a")) as page_a, connect(_page_url(adapter, "b")) as page_b:
        assert (await _ready(page_a))["turn_engine"] == "tyger"
        assert (await _ready(page_b))["turn_engine"] == "tyger"
        await page_a.send(b"\x01\x00" * 160)
        await page_b.send(b"\x02\x00" * 160)
        conns = await fake_vm.wait_connections(2)
        await wait_for(lambda: all(c.audio for c in conns))
    for conn in conns:
        first = conn.frames[0]
        assert first == {"type": "turn_engine", "engine": "tyger"}
        assert isinstance(conn.frames[1], bytes)
    assert {conns[0].audio[:2], conns[1].audio[:2]} == {b"\x01\x00", b"\x02\x00"}


async def test_mic_audio_reaches_voicemanager_in_order(adapter, fake_vm):
    chunks = [bytes([i, 0]) * 160 for i in range(1, 6)]
    async with connect(_page_url(adapter, "phone")) as page:
        await _ready(page)
        for chunk in chunks:
            await page.send(chunk)
        (conn,) = await fake_vm.wait_connections(1)
        await wait_for(lambda: len(conn.audio) == sum(map(len, chunks)))
    assert conn.audio == b"".join(chunks)


async def test_closing_the_page_closes_its_voicemanager_connection(adapter, fake_vm):
    async with connect(_page_url(adapter, "phone")) as page:
        await _ready(page)
        (conn,) = await fake_vm.wait_connections(1)
    await asyncio.wait_for(conn.closed.wait(), 2.0)


async def test_refused_turn_engine_leaves_a_classic_session(plugin):
    async with FakeVoiceManager(turn_engine_refusal="turn_engine_unsupported") as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        assert await adapter.connect()
        try:
            async with connect(_page_url(adapter, "phone")) as page:
                assert (await _ready(page))["turn_engine"] is None
                await page.send(b"\x05\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                await wait_for(lambda: conn.audio)
        finally:
            await adapter.disconnect()


async def test_unreachable_voicemanager_tells_the_page(plugin):
    adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
        "token": TOKEN, "voicemanager_url": "http://127.0.0.1:9", "host": "127.0.0.1", "port": 0}))
    assert await adapter.connect()
    try:
        async with connect(_page_url(adapter, "phone")) as page:
            frame = json.loads(await asyncio.wait_for(page.recv(), 3))
    finally:
        await adapter.disconnect()
    assert frame["type"] == "error"
    assert "VoiceManager" in frame["message"]


def test_client_from_url_parses_scheme_host_and_port(plugin):
    client = plugin.session.client_from_url("https://vm.example:9443", "t")
    assert (client.host, client.port, client.use_ssl, client.token) == ("vm.example", 9443, True, "t")
    client = plugin.session.client_from_url("http://127.0.0.1:8250")
    assert (client.host, client.port, client.use_ssl) == ("127.0.0.1", 8250, False)
