"""A barge-in stops the reply at once: no more of its audio or its text."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig
from gateway.session_context import set_session_vars

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"
PLATFORM = "voicemanager_live"


def fire(hook, message_id, **payload):
    """Call a hook off the loop, inside the session context of the turn answering ``message_id``."""
    def run():
        set_session_vars(platform=PLATFORM, chat_id="phone", message_id=message_id)
        hook(surface=PLATFORM, **payload) if hook.__name__ != "on_session_end" else hook(
            platform=PLATFORM, **payload)

    thread = threading.Thread(target=contextvars.Context().run, args=(run,))
    thread.start()
    thread.join()


class Page:
    """Records what the page receives, in order: ("json", frame) or ("pcm", bytes)."""

    def __init__(self, ws):
        self.ws, self.log = ws, []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            self.log.append(("pcm", message) if isinstance(message, bytes) else ("json", json.loads(message)))

    def of(self, kind):
        return [f for k, f in self.log if k == "json" and f.get("type") == kind]

    def pcm_after_first(self, kind):
        index = next(i for i, (k, f) in enumerate(self.log) if k == "json" and f.get("type") == kind)
        return [f for k, f in self.log[index + 1:] if k == "pcm"]


@contextlib.asynccontextmanager
async def rig(plugin, refusal=None):
    async with FakeVoiceManager(turn_engine_refusal=refusal) as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        messages = []

        async def handle_message(event):
            messages.append(event)

        adapter.handle_message = handle_message
        assert await adapter.connect()
        try:
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=phone") as ws:
                page = Page(ws)
                await wait_for(lambda: page.of("ready"))
                await ws.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                yield plugin.stream_hooks, conn, page, messages
                page.task.cancel()
        finally:
            await adapter.disconnect()


def tts(conn):
    return conn.json_frames("tts_stream")


async def _start_reply(hooks, conn, page, messages, text="Once upon a time"):
    message_id = messages[-1].message_id
    fire(hooks.on_stream_delta, message_id, delta=text, kind="text", turn_id="t1", iteration=1)
    frames = await wait_for(lambda: tts(conn))
    rid = frames[-1]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x11\x00" * 240)
    await wait_for(lambda: any(k == "pcm" for k, _ in page.log))
    return message_id, rid


async def test_turn_engine_barge_clears_the_page_and_drops_the_reply(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        await conn.send({"type": "turn_end", "text": "tell me a story", "turn_id": 0})
        await wait_for(lambda: messages)
        message_id, rid = await _start_reply(hooks, conn, page, messages)
        sent = len(tts(conn))
        await conn.send({"type": "barge", "request_id": rid, "onset_mic_sample": 10, "mic_sample": 20})
        await conn.send(b"\x22\x00" * 240)  # in flight when the server stopped
        await conn.send({"type": "audio_end", "request_id": rid, "reason": "interrupted"})
        await wait_for(lambda: page.of("clear") and page.of("audio_end"))
        fire(hooks.on_stream_delta, message_id, delta=" there was a dragon", kind="text", turn_id="t1",
             iteration=1)
        await asyncio.sleep(0.2)
        assert page.pcm_after_first("clear") == []
        assert len(tts(conn)) == sent
        assert conn.json_frames("tts_cancel") == []  # the server cancelled it itself
        await conn.send({"type": "turn_end", "text": "stop, a different one", "turn_id": 0})
        await wait_for(lambda: len(messages) == 2)
        assert messages[1].text == "stop, a different one"


async def test_classic_talk_over_cancels_tts_and_clears_the_page(plugin):
    async with rig(plugin, refusal="turn_engine_unsupported") as (hooks, conn, page, messages):
        await wait_for(lambda: conn.json_frames("turn_start"))
        await conn.send({"type": "transcription", "segment_index": 0, "text": "tell me a story",
                         "endpoint": True, "is_final": False, "turn_id": 1})
        await wait_for(lambda: messages)
        message_id, rid = await _start_reply(hooks, conn, page, messages)
        clears = len(page.of("clear"))
        await conn.send({"type": "transcription", "segment_index": 1, "text": "wait",
                         "endpoint": False, "is_final": False, "turn_id": 1})
        await wait_for(lambda: conn.json_frames("tts_cancel"))
        assert conn.json_frames("tts_cancel")[0]["request_id"] == rid
        await wait_for(lambda: len(page.of("clear")) > clears)
        await conn.send(b"\x33\x00" * 240)  # pcm the server had already sent
        await asyncio.sleep(0.2)
        cleared_at = max(i for i, (k, f) in enumerate(page.log) if k == "json" and f.get("type") == "clear")
        assert [f for k, f in page.log[cleared_at + 1:] if k == "pcm"] == []


async def test_text_of_a_cancelled_reply_never_reaches_the_next_one(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        await conn.send({"type": "turn_end", "text": "first question", "turn_id": 0})
        await wait_for(lambda: messages)
        old_id, rid = await _start_reply(hooks, conn, page, messages, "First answer")
        await conn.send({"type": "turn_end", "text": "second question", "turn_id": 0})
        await wait_for(lambda: len(messages) == 2)
        new_id = messages[1].message_id
        assert new_id != old_id
        fire(hooks.on_stream_delta, old_id, delta=" still talking", kind="text", turn_id="t1", iteration=1)
        fire(hooks.on_stream_delta, new_id, delta="Second answer", kind="text", turn_id="t2", iteration=1)
        await wait_for(lambda: len({f["request_id"] for f in tts(conn)}) == 2)
        await asyncio.sleep(0.1)
        texts = [f["text"] for f in tts(conn)]
        assert texts == ["First ", "Second "]  # whole words only; nothing of the cancelled reply after it
