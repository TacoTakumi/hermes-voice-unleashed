"""Reply text goes from the stream hooks into the page's TTS request as it is written."""

from __future__ import annotations

import asyncio
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


def fire(hook, platform=PLATFORM, chat_id="phone", **payload):
    """Call a hook the way Hermes does: off the loop, inside the turn's session context."""
    def run():
        set_session_vars(platform=platform, chat_id=chat_id)
        hook(**payload)

    thread = threading.Thread(target=contextvars.Context().run, args=(run,))
    thread.start()
    thread.join()


class Page:
    def __init__(self, ws):
        self.ws, self.frames, self.audio = ws, [], b""
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            if isinstance(message, bytes):
                self.audio += message
            else:
                self.frames.append(json.loads(message))

    def of(self, kind):
        return [f for f in self.frames if f.get("type") == kind]


@pytest.fixture
async def rig(plugin):
    async with FakeVoiceManager() as fake:
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
                await conn.send({"type": "turn_end", "text": "tell me something", "turn_id": 0})
                await wait_for(lambda: messages)
                yield plugin.stream_hooks, conn, page
                page.task.cancel()
        finally:
            await adapter.disconnect()


def tts_frames(conn):
    return conn.json_frames("tts_stream")


def ended(conn):
    """The tts_stream frames once the request was ended with done, else None."""
    frames = tts_frames(conn)
    return frames if frames and frames[-1]["done"] else None


async def test_deltas_reach_tts_before_the_turn_ends(rig):
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="Hello there.", kind="text", turn_id="t1", iteration=1,
         surface=PLATFORM)
    first = await wait_for(lambda: tts_frames(conn))
    assert first[0]["text"] == "Hello there."
    assert first[0]["chunking"] == "clause"
    assert first[0]["done"] is False
    fire(hooks.on_stream_delta, delta=" How are you?", kind="text", turn_id="t1", iteration=1,
         surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    frames = await wait_for(lambda: ended(conn))
    assert {f["request_id"] for f in frames} == {frames[0]["request_id"]}
    assert "".join(f["text"] for f in frames) == "Hello there. How are you?"
    await wait_for(lambda: page.of("reply_delta"))
    assert "".join(f["text"] for f in page.of("reply_delta")) == "Hello there. How are you?"


async def test_deltas_that_arrive_after_the_turn_end_are_still_spoken(rig):
    """Each hook runs on its own thread, so the turn's end can overtake its last deltas."""
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="The answer", kind="text", turn_id="t1", iteration=1,
         surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    fire(hooks.on_stream_delta, delta=" is forty", kind="text", turn_id="t1", iteration=1,
         surface=PLATFORM)
    fire(hooks.on_stream_delta, delta="-two.", kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    frames = await wait_for(lambda: ended(conn))
    assert "".join(f["text"] for f in frames) == "The answer is forty-two."
    assert [f["done"] for f in frames] == [False, False, False, True]


async def test_reply_audio_reaches_the_page(rig):
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="Hi.", kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    frames = await wait_for(lambda: ended(conn))
    rid = frames[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x10\x00" * 240)
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    await wait_for(lambda: page.of("audio_end"))
    assert page.of("audio_start")[0] == {"type": "audio_start", "request_id": rid, "sample_rate": 24000}
    assert page.audio == b"\x10\x00" * 240


async def test_other_platforms_and_reasoning_send_nothing(rig):
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, platform="telegram", delta="not for us", kind="text", turn_id="x",
         iteration=1, surface="telegram")
    fire(hooks.on_stream_delta, delta="thinking...", kind="reasoning", turn_id="t1", iteration=1,
         surface=PLATFORM)
    fire(hooks.on_stream_delta, chat_id="someone-else", delta="other chat", kind="text", turn_id="t1",
         iteration=1, surface=PLATFORM)
    await asyncio.sleep(0.2)
    assert tts_frames(conn) == []


async def test_late_deltas_of_a_finished_turn_are_dropped(rig):
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="Done.", kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    await wait_for(lambda: ended(conn))
    count = len(tts_frames(conn))
    fire(hooks.on_stream_delta, delta=" late", kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    await asyncio.sleep(0.2)
    assert len(tts_frames(conn)) == count


async def test_a_turn_with_no_text_ends_the_reply(rig):
    hooks, conn, page = rig
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    await asyncio.sleep(0.1)
    await conn.send({"type": "turn_end", "text": "and now", "turn_id": 0})
    await wait_for(lambda: len(page.of("utterance")) == 2)
    fire(hooks.on_stream_delta, delta="Second.", kind="text", turn_id="t2", iteration=1, surface=PLATFORM)
    frames = await wait_for(lambda: tts_frames(conn))
    assert frames[0]["text"] == "Second."
    assert conn.json_frames("tts_cancel") == []


async def test_a_new_utterance_during_the_quiet_window_gets_its_own_reply(rig):
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="First.", kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    await conn.send({"type": "turn_end", "text": "second", "turn_id": 0})  # inside the quiet window
    await wait_for(lambda: len(page.of("utterance")) == 2)
    fire(hooks.on_stream_delta, delta="Second.", kind="text", turn_id="t2", iteration=1, surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t2", interrupted=False, platform=PLATFORM)
    await asyncio.sleep(0.6)
    frames = tts_frames(conn)
    by_request = {}
    for f in frames:
        by_request.setdefault(f["request_id"], []).append(f)
    second = [fs for fs in by_request.values() if fs[0]["text"] == "Second."]
    assert len(second) == 1 and second[0][-1]["done"] is True
    assert sum(1 for f in frames if f["done"]) == 1  # the first reply was cancelled, never ended twice


async def test_a_retried_stream_in_the_same_iteration_is_spoken(rig):
    """A stream that fails is retried in the same iteration; the retry's text must not be lost."""
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="Paris is", kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    # the stream failed here; Hermes retries the call in the same iteration
    fire(hooks.on_stream_delta, delta="The capital is Paris.", kind="text", turn_id="t1", iteration=1,
         surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    frames = await wait_for(lambda: ended(conn))
    assert [f["text"] for f in frames] == ["Paris is", "The capital is Paris.", ""]
    assert frames[-1]["done"] is True
