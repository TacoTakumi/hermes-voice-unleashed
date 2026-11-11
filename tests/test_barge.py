"""A barge-in stops the reply at once: no more of its audio or its text."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig
from gateway.session_context import set_session_vars

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"
PLATFORM = "voice_unleashed"


def fire(hook, message_id, **payload):
    """Call a hook off the loop, inside the session context of the turn answering ``message_id``."""
    def run():
        set_session_vars(platform=PLATFORM, chat_id="voice", message_id=message_id)
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
async def rig(plugin, refusal=None, barge_state=None):
    async with FakeVoiceManager(turn_engine_refusal=refusal, barge_state=barge_state) as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        messages = []

        async def handle_message(event):
            messages.append(event)

        adapter.handle_message = handle_message
        assert await adapter.connect()
        try:
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=voice") as ws:
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


async def test_classic_words_heard_before_the_next_turn_opens_dispatch_alone(plugin):
    """After a reply ends, the session waits for VoiceManager to answer its turn_start.
    Words heard in that wait are a new question: the answered one does not join them,
    and a late confirmation of the answered segment dispatches nothing."""
    async with rig(plugin, refusal="turn_engine_unsupported") as (hooks, conn, page, messages):
        await wait_for(lambda: conn.json_frames("turn_start"))
        await conn.send({"type": "transcription", "segment_index": 0, "text": "tell me a story",
                         "endpoint": True, "is_final": False, "turn_id": 1})
        await wait_for(lambda: messages)
        message_id, rid = await _start_reply(hooks, conn, page, messages)
        conn.hold_turn_start = True
        fire(hooks.on_session_end, message_id, turn_id="t1", interrupted=False)
        await wait_for(lambda: any(f["request_id"] == rid and f["done"] for f in tts(conn)))
        await conn.send({"type": "audio_end", "request_id": rid, "reason": "completed"})
        await wait_for(lambda: len(conn.json_frames("turn_start")) == 2)  # asked, not yet answered
        clears = len(page.of("clear"))
        await conn.send({"type": "transcription", "segment_index": 0, "text": "tell me a story",
                         "endpoint": True, "is_final": True, "turn_id": 1})
        await conn.send({"type": "transcription", "segment_index": 1, "text": "what time is it",
                         "endpoint": True, "is_final": False, "turn_id": 1})
        await wait_for(lambda: len(messages) == 2)
        assert messages[1].text == "what time is it"
        await conn.send({"type": "turn_started", "turn_id": 2})
        await asyncio.sleep(0.2)
        assert [m.text for m in messages] == ["tell me a story", "what time is it"]
        assert len(page.of("clear")) == clears


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


async def test_audio_start_of_a_reply_cancelled_before_its_audio_is_not_forwarded(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        await conn.send({"type": "turn_end", "text": "tell me a story", "turn_id": 0})
        await wait_for(lambda: messages)
        session = hooks._adapters[-1].session_for("voice")
        fire(hooks.on_stream_delta, messages[-1].message_id, delta="Once upon a time", kind="text",
             turn_id="t1", iteration=1)
        rid = (await wait_for(lambda: tts(conn)))[-1]["request_id"]
        # The user speaks again before VoiceManager starts the reply's audio.
        await conn.send({"type": "turn_end", "text": "no, a different one", "turn_id": 0})
        await wait_for(lambda: len(messages) == 2 and page.of("clear"))
        assert rid in session._dropped
        await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
        await conn.send(b"\x11\x00" * 240)
        await conn.send({"type": "audio_end", "request_id": rid, "reason": "cancelled"})
        await wait_for(lambda: any(f["request_id"] == rid for f in page.of("audio_end")))
        assert [f for f in page.of("audio_start") if f["request_id"] == rid] == []
        assert page.pcm_after_first("clear") == []
        assert rid not in session._dropped


async def _barged_reply(hooks, conn, page, messages):
    """A reply cut by a barge while its Hermes turn still runs; returns its message id."""
    await conn.send({"type": "turn_end", "text": "tell me a story", "turn_id": 0})
    await wait_for(lambda: messages)
    message_id, rid = await _start_reply(hooks, conn, page, messages)
    await conn.send({"type": "barge", "request_id": rid, "onset_mic_sample": 10, "mic_sample": 20})
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "interrupted"})
    await wait_for(lambda: page.of("clear") and page.of("audio_end"))
    return message_id


async def test_a_barge_on_a_running_turn_leaves_the_page_busy_until_the_turn_ends(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        message_id = await _barged_reply(hooks, conn, page, messages)
        session = hooks._adapters[-1].session_for("voice")
        await asyncio.sleep(0.2)
        assert page.of("turn_done") == []  # Hermes still works: the page must not show Listening
        assert session.turn_running and not session.reply_open
        fire(hooks.on_session_end, message_id, turn_id="t1", interrupted=False)
        await wait_for(lambda: page.of("turn_done"))
        assert not session.turn_running
        fire(hooks.on_session_end, message_id, turn_id="t1", interrupted=False)
        await asyncio.sleep(0.2)
        assert len(page.of("turn_done")) == 1


async def test_a_barge_on_the_tail_of_a_finished_turn_ends_the_turn_on_the_page_at_once(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        await conn.send({"type": "turn_end", "text": "tell me a story", "turn_id": 0})
        await wait_for(lambda: messages)
        message_id, rid = await _start_reply(hooks, conn, page, messages)
        fire(hooks.on_session_end, message_id, turn_id="t1", interrupted=False)
        await wait_for(lambda: any(f["request_id"] == rid and f["done"] for f in tts(conn)))
        assert page.of("turn_done") == []  # the reply's audio still plays
        await conn.send({"type": "barge", "request_id": rid, "onset_mic_sample": 10, "mic_sample": 20})
        await wait_for(lambda: page.of("clear") and page.of("turn_done"))


async def test_a_running_turn_still_shows_its_work_after_a_barge_and_speaks_no_more(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        message_id = await _barged_reply(hooks, conn, page, messages)
        sent, shown = len(tts(conn)), len(page.of("reply_delta"))
        fire(hooks.on_stream_delta, message_id, delta="Let me look.", kind="reasoning", turn_id="t1",
             iteration=2)
        fire(hooks.pre_tool_call, message_id, tool_name="terminal", args={"command": "ls"},
             tool_call_id="c1")
        fire(hooks.post_tool_call, message_id, tool_call_id="c1", status="ok", duration_ms=12)
        fire(hooks.on_stream_delta, message_id, delta=" there was a dragon. ", kind="text", turn_id="t1",
             iteration=2)
        fire(hooks.on_stream_end, message_id)
        await wait_for(lambda: page.of("tool_end"))
        assert page.of("reasoning_delta") == [{"type": "reasoning_delta", "text": "Let me look."}]
        assert page.of("tool_start") == [{"type": "tool_start", "id": "c1", "name": "terminal", "summary": "ls"}]
        assert page.of("tool_end") == [{"type": "tool_end", "id": "c1", "status": "ok", "duration_ms": 12}]
        await asyncio.sleep(0.5)  # longer than the quiet window after a stream end
        assert len(tts(conn)) == sent  # the cut reply's text stays unspoken
        assert len(page.of("reply_delta")) == shown
        assert page.of("turn_done") == []


async def test_a_turn_that_a_newer_utterance_replaced_shows_nothing_more(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        old_id = await _barged_reply(hooks, conn, page, messages)
        assert page.of("turn_done") == []
        await conn.send({"type": "turn_end", "text": "stop, a different one", "turn_id": 0})
        await wait_for(lambda: len(messages) == 2)
        new_id = messages[1].message_id
        # The gateway interrupts the old turn for the new message: its lines end on the
        # page before the new utterance's lines start.
        kinds = [f["type"] for k, f in page.log if k == "json"]
        assert kinds.count("turn_done") == 1
        assert kinds.index("turn_done") < len(kinds) - 1 - kinds[::-1].index("utterance")
        fire(hooks.on_stream_delta, old_id, delta="Old thought.", kind="reasoning", turn_id="t1", iteration=2)
        fire(hooks.pre_tool_call, old_id, tool_name="terminal", args={"command": "ls"}, tool_call_id="c1")
        fire(hooks.post_tool_call, old_id, tool_call_id="c1", status="cancelled", duration_ms=5)
        fire(hooks.on_stream_delta, old_id, delta=" still talking. ", kind="text", turn_id="t1", iteration=2)
        fire(hooks.on_session_end, old_id, turn_id="t1", interrupted=True)
        fire(hooks.pre_llm_call, new_id, session_id="s1")
        session = hooks._adapters[-1].session_for("voice")
        await wait_for(lambda: session.turn_running)
        await asyncio.sleep(0.2)
        assert page.of("reasoning_delta") == [] and page.of("tool_start") == [] and page.of("tool_end") == []
        assert [f["text"] for f in tts(conn)] == ["Once upon a "]
        assert len(page.of("turn_done")) == 1  # the new turn still runs


async def test_a_turn_that_fails_after_a_barge_ends_on_the_page(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        message_id = await _barged_reply(hooks, conn, page, messages)
        adapter = hooks._adapters[-1]
        assert page.of("turn_done") == []
        # No end-of-turn hook follows a turn that raised.
        event = SimpleNamespace(source=adapter._voice_source("voice"), message_id=message_id)
        await adapter._notify_turn_error(event, RuntimeError("the handler raised"))
        await wait_for(lambda: page.of("turn_done"))
        assert not adapter.session_for("voice").turn_running


@pytest.mark.parametrize("late", ["a reply delta", "a stream end"])
async def test_a_late_event_of_a_finished_turn_does_not_start_it_again(plugin, late):
    """The end-of-turn hook can overtake the turn's last stream events. A barge in the
    reply's quiet window after such an event must still end the turn on the page."""
    async with rig(plugin) as (hooks, conn, page, messages):
        await conn.send({"type": "turn_end", "text": "tell me a story", "turn_id": 0})
        await wait_for(lambda: messages)
        message_id, rid = await _start_reply(hooks, conn, page, messages)
        session = hooks._adapters[-1].session_for("voice")
        fire(hooks.on_session_end, message_id, turn_id="t1", interrupted=False)
        await wait_for(lambda: not session.turn_running)
        if late == "a reply delta":
            fire(hooks.on_stream_delta, message_id, delta=" there was a dragon. ", kind="text",
                 turn_id="t1", iteration=1)
        else:
            fire(hooks.on_stream_end, message_id)
        await conn.send({"type": "barge", "request_id": rid, "onset_mic_sample": 10, "mic_sample": 20})
        await conn.send({"type": "audio_end", "request_id": rid, "reason": "interrupted"})
        await wait_for(lambda: page.of("clear"))
        await wait_for(lambda: page.of("turn_done"))
        assert not session.turn_running



async def test_a_turn_end_tells_the_page_that_the_heard_words_are_gone(plugin):
    """Words shown as heard that lead to no utterance: the page must not go on showing
    them as being heard."""
    async with rig(plugin) as (hooks, conn, page, messages):
        await conn.send({"type": "transcription", "segment_index": 0, "text": "...", "is_final": False})
        await wait_for(lambda: [f for f in page.of("transcript") if f["text"]])
        await conn.send({"type": "turn_end", "text": "", "turn_id": 0})
        await wait_for(lambda: page.of("transcript")[-1]["text"] == "")
        assert messages == [] and page.of("utterance") == []


async def test_the_barge_state_of_the_server_reaches_the_page_after_its_ready_frame(plugin):
    """The server says when it cannot hear a barge yet (it still learns the user's
    voice) and when it can. The page is told both, in order, after it is ready."""
    async with rig(plugin, barge_state="learning") as (hooks, conn, page, messages):
        await wait_for(lambda: page.of("barge_state"))
        kinds = [f["type"] for k, f in page.log if k == "json"]
        assert kinds.index("ready") < kinds.index("barge_state"), kinds
        assert page.of("barge_state") == [{"type": "barge_state", "state": "learning"}]
        await conn.send({"type": "barge_state", "state": "x"})  # a state the page does not know
        await conn.send({"type": "barge_state", "state": "ready", "voice_s": 3.0})
        await wait_for(lambda: len(page.of("barge_state")) == 2)
        assert page.of("barge_state")[1] == {"type": "barge_state", "state": "ready"}


async def test_a_server_that_sends_no_barge_state_gives_the_page_none(plugin):
    async with rig(plugin) as (hooks, conn, page, messages):
        await conn.send({"type": "turn_end", "text": "hello", "turn_id": 0})
        await wait_for(lambda: messages)
        assert page.of("barge_state") == []


LOST = {"type": "error", "error_type": "turn_engine_lost", "message": "the turn engine is gone"}


async def test_a_lost_turn_engine_ends_the_learning_state_on_the_page(plugin):
    """Without the turn engine speech stops a reply again, and the server says no more
    about its barge state: the page must not go on saying that barge-in is off."""
    async with rig(plugin, barge_state="learning") as (hooks, conn, page, messages):
        await wait_for(lambda: page.of("barge_state"))
        await conn.send(LOST)
        await wait_for(lambda: len(page.of("barge_state")) == 2)
        assert page.of("barge_state")[1] == {"type": "barge_state", "state": "ready"}


async def test_a_lost_turn_engine_says_nothing_of_barge_state_when_none_was_learning(plugin):
    async with rig(plugin, barge_state="learning") as (hooks, conn, page, messages):
        await conn.send({"type": "barge_state", "state": "ready"})
        await wait_for(lambda: len(page.of("barge_state")) == 2)
        await conn.send(LOST)
        await wait_for(lambda: conn.json_frames("turn_start"))
        await asyncio.sleep(0.2)
        assert len(page.of("barge_state")) == 2
    async with rig(plugin) as (hooks, conn, page, messages):
        await conn.send(LOST)
        await wait_for(lambda: conn.json_frames("turn_start"))
        await asyncio.sleep(0.2)
        assert page.of("barge_state") == []
