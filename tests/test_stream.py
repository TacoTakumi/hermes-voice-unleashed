"""Reply text goes from the stream hooks into the page's TTS request as it is written."""

from __future__ import annotations

import asyncio
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
PLATFORM = "voicemanager_live"


def fire(hook, platform=PLATFORM, chat_id="voice", **payload):
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
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=voice") as ws:
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
    fire(hooks.on_stream_delta, delta="Hello there. ", kind="text", turn_id="t1", iteration=1,
         surface=PLATFORM)
    first = await wait_for(lambda: tts_frames(conn))
    assert first[0]["text"] == "Hello there. "
    assert first[0]["chunking"] == "clause"
    assert first[0]["done"] is False
    fire(hooks.on_stream_delta, delta="How are you?", kind="text", turn_id="t1", iteration=1,
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
    assert frames[-1]["done"] is True and not any(f["done"] for f in frames[:-1])


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



async def test_the_page_gets_turn_done_after_the_reply_audio_ends(rig):
    """The page shows Hermes at work until the turn and its reply audio are over."""
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="Hi. ", kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    rid = (await wait_for(lambda: tts_frames(conn)))[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x10\x00" * 240)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    await wait_for(lambda: ended(conn))
    await asyncio.sleep(0.2)
    assert page.of("turn_done") == []  # the turn ended, but its reply is still playing
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    await wait_for(lambda: page.of("turn_done"))
    kinds = [f["type"] for f in page.frames]
    assert kinds.index("audio_end") < kinds.index("turn_done")


async def _flushed_early(hooks, conn):
    """Speak a first part, then have VoiceManager end the request on its own, as its
    flush timeout does when tools run for longer than it waits for text."""
    fire(hooks.on_stream_delta, delta="Let me check. ", kind="text", turn_id="t1", iteration=1,
         surface=PLATFORM)
    rid = (await wait_for(lambda: tts_frames(conn)))[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x10\x00" * 240)
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    return rid


async def test_text_after_a_flush_timeout_goes_into_a_new_request(rig):
    hooks, conn, page = rig
    first = await _flushed_early(hooks, conn)
    await wait_for(lambda: page.of("audio_end"))
    fire(hooks.on_stream_delta, delta="It is sunny.", kind="text", turn_id="t1", iteration=2,
         surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    frames = await wait_for(lambda: ended(conn))
    later = [f for f in frames if f["request_id"] != first]
    assert "".join(f["text"] for f in later) == "It is sunny."
    assert later[-1]["done"] is True and not any(f["done"] for f in frames if f["request_id"] == first)
    assert page.of("turn_done") == []
    await conn.send({"type": "audio_end", "request_id": later[0]["request_id"], "reason": "complete"})
    await wait_for(lambda: page.of("turn_done"))


async def test_a_turn_with_nothing_more_after_a_flush_timeout_still_ends(rig):
    hooks, conn, page = rig
    await _flushed_early(hooks, conn)
    await wait_for(lambda: page.of("audio_end"))
    assert page.of("turn_done") == []  # the turn still runs
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    await wait_for(lambda: page.of("turn_done"))
    assert not any(f["done"] for f in tts_frames(conn))  # nothing sent to the ended request

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
    fire(hooks.on_session_end, turn_id="t2", interrupted=False, platform=PLATFORM)
    frames = await wait_for(lambda: ended(conn))
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


def deliver(hooks, name, **payload):
    """Deliver one Hermes hook event to whatever the plugin registered for it, as Hermes does."""
    callback = hooks.HOOKS.get(name)
    if callback is not None:
        fire(callback, **payload)


async def test_a_retried_stream_in_the_same_iteration_is_spoken(rig):
    """A stream that fails is retried in the same iteration; the retry's text must not be lost."""
    hooks, conn, page = rig
    deliver(hooks, "on_stream_delta", delta="Paris is", kind="text", turn_id="t1", iteration=1,
            surface=PLATFORM)
    # The first attempt failed: Hermes ends that stream unfinished, then retries the call.
    deliver(hooks, "on_stream_end", final_text="", finished=False, error="connection reset", turn_id="t1",
            iteration=1, surface=PLATFORM)
    await asyncio.sleep(0.1)
    deliver(hooks, "on_stream_delta", delta="The capital is Paris.", kind="text", turn_id="t1", iteration=1,
            surface=PLATFORM)
    deliver(hooks, "on_session_end", turn_id="t1", interrupted=False, platform=PLATFORM)
    frames = await wait_for(lambda: ended(conn))
    assert "".join(f["text"] for f in frames) == "Paris isThe capital is Paris."  # the retry is spoken
    assert frames[-1]["done"] is True


def voicemanager_join(frames):
    """How VoiceManager's clause mode rebuilds a request's text from its chunks."""
    return " ".join(f["text"].strip() for f in frames if f["text"].strip())


async def test_tts_gets_whole_words_so_voicemanager_joins_them_right(rig):
    """Model tokens split words and leave a sentence's period alone; VoiceManager joins
    stripped chunks with spaces, so the plugin must send whole words only."""
    hooks, conn, page = rig
    tokens = ["The sub", "systems", " are", " CAS", "-", "1", " and", " tri", "age", ".\n\n", "One",
              ":", " Standing", " by", "."]
    for token in tokens:
        fire(hooks.on_stream_delta, delta=token, kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    frames = await wait_for(lambda: ended(conn))
    texts = [f["text"] for f in frames]
    assert "".join(texts) == "".join(tokens)  # nothing lost or added
    assert voicemanager_join(frames) == "The subsystems are CAS-1 and triage. One: Standing by."
    assert all(t[-1].isspace() for t in texts[:-2])  # every chunk before the end stops at a word boundary
    assert not any(t.strip() and all(not c.isalnum() for c in t.strip()) for t in texts)  # no punctuation alone
    await wait_for(lambda: "".join(f["text"] for f in page.of("reply_delta")) == "".join(tokens))


# --- tool calls on the page ------------------------------------------------------------

LONG_COMMAND = "pytest -q tests/test_stream.py   -k 'tool and not slow' --maxfail=1"


async def test_tool_calls_reach_the_page_as_start_and_end_frames(rig):
    hooks, conn, page = rig
    results = []

    def pre(**payload):
        results.append(hooks.HOOKS["pre_tool_call"](**payload))

    fire(pre, tool_name="terminal", args={"command": LONG_COMMAND}, tool_call_id="c1", turn_id="t1",
         session_id="s1")
    deliver(hooks, "post_tool_call", tool_name="terminal", args={"command": LONG_COMMAND}, result="ok",
            tool_call_id="c1", turn_id="t1", session_id="s1", duration_ms=1234, status="ok")
    deliver(hooks, "pre_tool_call", tool_name="web_search", args={"query": "weather"}, tool_call_id="c2",
            turn_id="t1", session_id="s1")
    deliver(hooks, "post_tool_call", tool_name="web_search", args={"query": "weather"}, result="",
            tool_call_id="c2", turn_id="t1", session_id="s1", duration_ms=50, status="error",
            error_message="no network")
    await wait_for(lambda: len(page.of("tool_end")) == 2)
    assert results == [None]  # the blocking hook never vetoes
    start, second = page.of("tool_start")
    assert start["id"] == "c1" and start["name"] == "terminal"
    assert len(start["summary"]) <= 40 and start["summary"].startswith("pytest -q tests/test_stream.py -k")
    assert second == {"type": "tool_start", "id": "c2", "name": "web_search", "summary": "weather"}
    assert page.of("tool_end") == [
        {"type": "tool_end", "id": "c1", "status": "ok", "duration_ms": 1234},
        {"type": "tool_end", "id": "c2", "status": "error", "duration_ms": 50}]


async def test_tool_calls_of_another_chat_or_a_closed_reply_send_nothing(rig):
    hooks, conn, page = rig
    deliver(hooks, "pre_tool_call", chat_id="someone-else", tool_name="terminal", args={"command": "ls"},
            tool_call_id="c1", turn_id="t1", session_id="s1")
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    await wait_for(lambda: page.of("turn_done"))
    deliver(hooks, "pre_tool_call", tool_name="terminal", args={"command": "ls"}, tool_call_id="c2",
            turn_id="t1", session_id="s1")
    await asyncio.sleep(0.2)
    assert page.of("tool_start") == []


async def test_an_attached_session_gets_no_tool_frames(plugin):
    from voicemanager_live.session import VoiceSession

    class Page:
        sent = []

        async def send(self, message):
            self.sent.append(message)

    session = VoiceSession(Page(), client=None, attached=True)
    session._open_reply()
    await session.on_tool_start("", "c1", "terminal", "ls")
    await session.on_tool_end("", "c1", "ok", 5)
    assert Page.sent == []


# --- reasoning on the page -------------------------------------------------------------

async def test_reasoning_reaches_the_page_but_not_tts(rig):
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="The user wants the weather. ", kind="reasoning", turn_id="t1",
         iteration=1, surface=PLATFORM)
    fire(hooks.on_stream_delta, chat_id="someone-else", delta="other chat", kind="reasoning", turn_id="t1",
         iteration=1, surface=PLATFORM)
    await wait_for(lambda: page.of("reasoning_delta"))
    await asyncio.sleep(0.2)
    assert page.of("reasoning_delta") == [{"type": "reasoning_delta", "text": "The user wants the weather. "}]
    assert page.of("reply_delta") == [] and tts_frames(conn) == []
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    await wait_for(lambda: page.of("turn_done"))
    fire(hooks.on_stream_delta, delta="late", kind="reasoning", turn_id="t1", iteration=1, surface=PLATFORM)
    await asyncio.sleep(0.2)
    assert len(page.of("reasoning_delta")) == 1  # the reply is closed


async def test_every_tool_status_hermes_reports_reaches_the_page(rig):
    hooks, conn, page = rig
    statuses = ["ok", "error", "timeout", "blocked", "cancelled", None, "something-new"]
    for i, status in enumerate(statuses):
        deliver(hooks, "pre_tool_call", tool_name="terminal", args={}, tool_call_id=f"c{i}", turn_id="t1",
                session_id="s1")
        deliver(hooks, "post_tool_call", tool_name="terminal", args={}, result="", tool_call_id=f"c{i}",
                turn_id="t1", session_id="s1", duration_ms=1, status=status)
    await wait_for(lambda: len(page.of("tool_end")) == len(statuses))
    assert [f["status"] for f in page.of("tool_end")] == [
        "ok", "error", "timeout", "blocked", "cancelled", "ok", "ok"]


# --- text before a tool call -----------------------------------------------------------

async def test_the_last_word_before_a_tool_call_is_spoken_at_the_stream_end(rig):
    """A model response that ends with a tool call ends its text without a space; its last
    word must not wait for the next text or the end of the turn."""
    hooks, conn, page = rig
    fire(hooks.on_stream_delta, delta="Acknowledged. Standing", kind="text", turn_id="t1", iteration=1,
         surface=PLATFORM)
    deliver(hooks, "on_stream_end", final_text="Acknowledged. Standing by.", finished=True, error=None,
            turn_id="t1", iteration=1, surface=PLATFORM)  # may overtake the last delta
    fire(hooks.on_stream_delta, delta=" by.", kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    frames = await wait_for(lambda: tts_frames(conn) if "".join(
        f["text"] for f in tts_frames(conn)).endswith("by.") else None, timeout=1.0)
    assert "".join(f["text"] for f in frames) == "Acknowledged. Standing by."
    assert not any(f["done"] for f in frames)  # the turn goes on: the request stays open


def _session(hooks):
    return hooks._adapters[-1].session_for("voice")


TURN_EVENTS = {
    "the start of the model call": lambda hooks: fire(hooks.pre_llm_call, session_id="s1"),
    "reply text": lambda hooks: fire(hooks.on_stream_delta, delta="Hello. ", kind="text", turn_id="t1",
                                     iteration=1, surface=PLATFORM),
    "reasoning": lambda hooks: fire(hooks.on_stream_delta, delta="Thinking.", kind="reasoning",
                                    turn_id="t1", iteration=1, surface=PLATFORM),
    "a tool call": lambda hooks: fire(hooks.pre_tool_call, tool_name="terminal", args={"command": "ls"},
                                      tool_call_id="c1"),
    "the end of a model response": lambda hooks: fire(hooks.on_stream_end, surface=PLATFORM),
}


@pytest.mark.parametrize("event", TURN_EVENTS)
async def test_the_turn_runs_from_its_first_hook_event(rig, event):
    hooks, conn, page = rig
    session = _session(hooks)
    assert session.reply_open and not session.turn_running  # heard, nothing of the turn yet
    TURN_EVENTS[event](hooks)
    await wait_for(lambda: session.turn_running)


async def test_an_event_of_another_platform_does_not_start_the_turn(rig):
    hooks, conn, page = rig
    session = _session(hooks)
    fire(hooks.pre_llm_call, platform="telegram", session_id="s1")
    fire(hooks.on_stream_delta, platform="telegram", delta="Hi. ", kind="text", turn_id="t9", iteration=1)
    await asyncio.sleep(0.1)
    assert not session.turn_running


async def test_the_turn_runs_no_more_once_it_is_done_or_cancelled(rig):
    hooks, conn, page = rig
    session = _session(hooks)
    fire(hooks.pre_llm_call, session_id="s1")
    await wait_for(lambda: session.turn_running)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)
    await wait_for(lambda: not session.turn_running)
    await conn.send({"type": "turn_end", "text": "and another thing", "turn_id": 0})
    await wait_for(lambda: len(page.of("utterance")) == 2)
    assert not session.turn_running  # a new utterance starts with no turn running
    fire(hooks.pre_llm_call, session_id="s1")
    await wait_for(lambda: session.turn_running)
    await session.cancel_reply("test")
    assert not session.turn_running


async def test_a_reply_frame_sent_while_the_turn_runs_says_so(rig):
    hooks, conn, page = rig
    adapter = hooks._adapters[-1]
    await adapter.send("voice", "The gateway is busy, try again later.")
    await wait_for(lambda: page.of("reply"))
    assert page.of("reply")[-1] == {"type": "reply", "text": "The gateway is busy, try again later."}
    fire(hooks.pre_llm_call, session_id="s1")
    await wait_for(lambda: _session(hooks).turn_running)
    await adapter.send("voice", "Switching to the fallback provider.")
    await wait_for(lambda: len(page.of("reply")) == 2)
    assert page.of("reply")[-1] == {"type": "reply", "text": "Switching to the fallback provider.",
                                    "turn_running": True}
    # The turn's own reply names the utterance it answers. When the turn failed, its
    # error text comes this way and no turn_done follows: it must end the turn.
    await adapter.send("voice", "The provider gave an error.", reply_to=_session(hooks).reply_id)
    await wait_for(lambda: len(page.of("reply")) == 3)
    assert page.of("reply")[-1] == {"type": "reply", "text": "The provider gave an error."}


async def test_a_turn_error_from_the_gateway_ends_the_turn_state(rig):
    hooks, conn, page = rig
    adapter, session = hooks._adapters[-1], _session(hooks)
    fire(hooks.pre_llm_call, session_id="s1")
    await wait_for(lambda: session.turn_running)
    event = SimpleNamespace(source=adapter._voice_source("voice"), message_id=session.reply_id)
    await adapter._notify_turn_error(event, RuntimeError("the handler raised"))
    assert not session.turn_running
    await adapter.send("voice", "Sorry, that turn failed.")
    await wait_for(lambda: page.of("reply"))
    assert "turn_running" not in page.of("reply")[-1]


async def test_an_event_of_another_reply_does_not_start_the_turn(rig):
    hooks, conn, page = rig
    session = _session(hooks)
    assert session.reply_id and session.reply_open
    await session.on_turn_start("another-reply")
    await session.on_reply_delta("another-reply", "t0", 1, "Late text. ")
    await session.on_reasoning_delta("another-reply", "Late thought.")
    await session.on_tool_start("another-reply", "c0", "terminal", "ls")
    await session.on_stream_end("another-reply")
    assert not session.turn_running
    assert page.of("reply_delta") == [] and page.of("tool_start") == []


async def _reply_request(hooks, conn):
    """A reply has text at the TTS: returns its request id."""
    fire(hooks.on_stream_delta, delta="Basic bread uses four ingredients. ", kind="text", turn_id="t1",
         iteration=1, surface=PLATFORM)
    frames = await wait_for(lambda: tts_frames(conn))
    return frames[0]["request_id"]


async def test_a_reply_that_ends_with_no_audio_tells_the_page_that_speech_failed(rig, caplog):
    hooks, conn, page = rig
    rid = await _reply_request(hooks, conn)
    with caplog.at_level("WARNING"):
        await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
        await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
        await wait_for(lambda: page.of("speech_failed"))
    assert page.of("speech_failed") == [{"type": "speech_failed", "request_id": rid}]
    assert any("no audio came" in r.getMessage() for r in caplog.records)


async def test_a_reply_with_audio_reports_no_audio_failure(rig):
    hooks, conn, page = rig
    rid = await _reply_request(hooks, conn)
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x11\x00" * 240)
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    await wait_for(lambda: page.of("audio_end"))
    await asyncio.sleep(0.1)
    assert page.of("speech_failed") == []


async def test_a_cancelled_reply_reports_no_audio_failure(rig):
    hooks, conn, page = rig
    rid = await _reply_request(hooks, conn)
    await _session(hooks).cancel_reply("test")
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "cancelled"})
    await wait_for(lambda: page.of("audio_end"))
    await asyncio.sleep(0.1)
    assert page.of("speech_failed") == []


async def test_a_notice_that_ends_with_no_audio_tells_the_page_that_speech_failed(rig):
    hooks, conn, page = rig
    session = _session(hooks)
    await session.cancel_reply("test")
    await session.notice("New session")
    rid = (await wait_for(lambda: tts_frames(conn)))[-1]["request_id"]
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    await wait_for(lambda: page.of("speech_failed"))
    assert page.of("speech_failed")[-1]["request_id"] == rid

