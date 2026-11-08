"""Each page gets its own VoiceManager connection, opted in to the turn engine."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

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
            # Every connect starts with the history frame; the error comes after it.
            frame = json.loads(await asyncio.wait_for(page.recv(), 3))
            assert frame["type"] == "history"
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


async def _expect_error_then_close(page):
    frames = []
    async with asyncio.timeout(5):
        async for message in page:
            if isinstance(message, str):
                frames.append(json.loads(message))
    return [f for f in frames if f.get("type") == "error"]


async def test_reader_failure_on_turn_start_reaches_the_page(plugin, monkeypatch):
    monkeypatch.setattr(plugin.session, "TURN_START_TIMEOUT_S", 0.3)
    async with FakeVoiceManager(turn_engine_refusal="turn_engine_unsupported", answer_turn_start=False) as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        assert await adapter.connect()
        try:
            async with connect(_page_url(adapter, "phone")) as page:
                errors = await _expect_error_then_close(page)
            (conn,) = fake.connections
            await asyncio.wait_for(conn.closed.wait(), 2)
        finally:
            await adapter.disconnect()
    assert errors and "turn_start" in errors[0]["message"]


async def test_reader_failure_on_dispatch_reaches_the_page(plugin):
    async with FakeVoiceManager() as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))

        async def broken(event):
            raise RuntimeError("gateway refused the message")

        adapter.handle_message = broken
        assert await adapter.connect()
        try:
            async with connect(_page_url(adapter, "phone")) as page:
                await _ready(page)
                await page.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                await conn.send({"type": "turn_end", "text": "hello", "turn_id": 0})
                errors = await _expect_error_then_close(page)
            await asyncio.wait_for(conn.closed.wait(), 2)
        finally:
            await adapter.disconnect()
    assert errors and "gateway refused the message" in errors[0]["message"]


async def test_reader_failure_in_the_reply_finish_reaches_the_page(plugin, monkeypatch):
    """Classic mode: a turn that said nothing opens the next listening turn from the task that
    closes the reply; a turn_start that is never answered there must end the session visibly."""
    monkeypatch.setattr(plugin.session, "TURN_START_TIMEOUT_S", 0.3)
    async with FakeVoiceManager(turn_engine_refusal="turn_engine_unsupported") as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        events = []

        async def handle_message(event):
            events.append(event)

        adapter.handle_message = handle_message
        assert await adapter.connect()
        try:
            async with connect(_page_url(adapter, "phone")) as page:
                await _ready(page)
                await page.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                await wait_for(lambda: conn.json_frames("turn_start"))
                fake.answer_turn_start = False  # VoiceManager stops answering from here on
                await conn.send({"type": "transcription", "segment_index": 0, "text": "hello there",
                                 "endpoint": True, "is_final": False, "turn_id": 1})
                await wait_for(lambda: events)
                await adapter.session_for("phone").on_turn_done(events[0].message_id, "t1", False)
                errors = await _expect_error_then_close(page)
            await asyncio.wait_for(conn.closed.wait(), 2)
        finally:
            await adapter.disconnect()
    assert errors and "turn_start" in errors[0]["message"]


async def test_a_second_page_takes_over_and_the_first_is_told_not_to_return(adapter, fake_vm):
    """One page owns a conversation's mic and speaker. A new page with the same client id takes it
    over; the old page gets a replaced frame and close code 4001, so it does not reconnect."""
    async with connect(_page_url(adapter, "phone")) as old:
        await _ready(old)
        (old_vm,) = await fake_vm.wait_connections(1)
        async with connect(_page_url(adapter, "phone")) as new:
            frames = []
            with contextlib.suppress(ConnectionClosed):
                async with asyncio.timeout(5):
                    async for message in old:
                        frames.append(json.loads(message))
            assert {"type": "replaced"} in frames
            assert old.close_code == 4001
            await asyncio.wait_for(old_vm.closed.wait(), 2)
            assert (await _ready(new))["type"] == "ready"
            await new.send(b"\x07\x00" * 160)
            conns = await fake_vm.wait_connections(2)
            await wait_for(lambda: conns[1].audio)
            assert not conns[1].closed.is_set()


async def _speaking_page(adapter, fake_vm, request_id: str, mic_chunks: int = 0):
    """A ready page with a reply playing: it sent ``mic_chunks`` frames of 160 samples first."""
    page = await connect(_page_url(adapter, "phone"))
    assert (await _ready(page))["turn_engine"] == "tyger"
    (conn,) = await fake_vm.wait_connections(1)
    for _ in range(mic_chunks):
        await page.send(b"\x01\x00" * 160)
    await wait_for(lambda: len(conn.audio) == mic_chunks * 320)
    await conn.send({"type": "audio_start", "request_id": request_id, "sample_rate": 24000})
    return page, conn


async def test_a_playback_frame_with_mic_sample_passes_it_to_voicemanager(adapter, fake_vm):
    page, conn = await _speaking_page(adapter, fake_vm, "r1", mic_chunks=2)
    try:
        await page.send(json.dumps({"type": "playback", "event": "start", "request_id": "r1",
                                    "mic_sample": 12345}))
        await page.send(json.dumps({"type": "playback", "event": "stop", "request_id": "r1",
                                    "mic_sample": 67890, "reply_sample": 4800}))
        await wait_for(lambda: len(conn.json_frames("playback")) == 2)
    finally:
        await page.close()
    start, stop = conn.json_frames("playback")
    assert start == {"type": "playback", "request_id": "r1", "event": "start", "mic_sample": 12345}
    assert stop == {"type": "playback", "request_id": "r1", "event": "stop", "mic_sample": 67890,
                    "reply_sample": 4800}


@pytest.mark.parametrize("extra", [{}, {"mic_sample": -1}, {"mic_sample": True},
                                   {"mic_sample": 1.5}, {"mic_sample": "12"}])
async def test_a_playback_frame_without_a_valid_mic_sample_uses_the_forwarded_count(
        adapter, fake_vm, extra):
    page, conn = await _speaking_page(adapter, fake_vm, "r1", mic_chunks=3)
    try:
        await page.send(json.dumps({"type": "playback", "event": "start", "request_id": "r1", **extra}))
        await page.send(json.dumps({"type": "playback", "event": "stop", "request_id": "r1",
                                    "reply_sample": 2400, **extra}))
        await wait_for(lambda: len(conn.json_frames("playback")) == 2)
    finally:
        await page.close()
    start, stop = conn.json_frames("playback")
    assert start["mic_sample"] == 480
    assert stop["mic_sample"] == 480
    assert stop["reply_sample"] == 2400
