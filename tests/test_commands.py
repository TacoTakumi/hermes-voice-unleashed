"""Spoken commands: "end session" ends the voice session and "new session" starts a
fresh one, instead of going to Hermes."""

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


# --- the phrase helpers ------------------------------------------------------------------


@pytest.mark.parametrize("said, phrase", [
    ("End the session!", "end the session"),
    ("  End   SESSION. ", "end session"),
    ("end, this session?", "end this session"),
    ("", ""),
])
def test_an_end_phrase_is_normalized_to_lower_case_words_without_punctuation(plugin, said, phrase):
    assert plugin.turns.normalized_phrase(said) == phrase


@pytest.mark.parametrize("said", ["End the session.", "end session", "End this session!"])
def test_the_end_phrases_match_the_end_command(plugin, said):
    assert plugin.turns.spoken_command(said) == plugin.turns.END_SESSION


@pytest.mark.parametrize("said", ["end session now please", "please end the session", "end", "session",
                                  "tell me a story"])
def test_other_text_is_no_end_command(plugin, said):
    assert plugin.turns.spoken_command(said) is None


@pytest.mark.parametrize("said", ["New session!", "start a new session", "Start new session."])
def test_the_new_phrases_match_the_new_command(plugin, said):
    assert plugin.turns.spoken_command(said) == plugin.turns.NEW_SESSION


@pytest.mark.parametrize("said", ["new session please", "start a new session now", "new", "a new session"])
def test_other_text_is_no_new_command(plugin, said):
    assert plugin.turns.spoken_command(said) is None


# --- through the adapter and a fake VoiceManager ------------------------------------------


def fire(hook, message_id, **payload):
    """Call a hook off the loop, inside the session context of the turn answering ``message_id``."""
    def run():
        set_session_vars(platform=PLATFORM, chat_id="phone", message_id=message_id)
        hook(surface=PLATFORM, **payload)

    thread = threading.Thread(target=contextvars.Context().run, args=(run,))
    thread.start()
    thread.join()


class Page:
    """Records what the page receives, in order; ``order`` gets the events the test
    needs to see in sequence: each TTS chunk and page frame the session sends, and
    the moment the page's socket closed."""

    def __init__(self, ws, order):
        self.ws, self.log, self.order = ws, [], order
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            self.log.append(("pcm", message) if isinstance(message, bytes) else ("json", json.loads(message)))
        self.order.append(("closed", self.ws.close_code))

    def of(self, kind):
        return [f for k, f in self.log if k == "json" and f.get("type") == kind]

    @property
    def closed(self):
        return self.task.done()


class Rig:
    def __init__(self, hooks, session, conn, page, messages, order):
        self.hooks, self.session, self.conn, self.page = hooks, session, conn, page
        self.messages, self.order = messages, order

    def tts(self):
        return self.conn.json_frames("tts_stream")

    def tts_text(self):
        return "".join(f["text"] for f in self.tts())


@contextlib.asynccontextmanager
async def rig(plugin, refusal=None):
    async with FakeVoiceManager(turn_engine_refusal=refusal) as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        messages, order = [], []

        async def handle_message(event):
            messages.append(event)

        adapter.handle_message = handle_message
        assert await adapter.connect()
        try:
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=phone") as ws:
                page = Page(ws, order)
                await wait_for(lambda: page.of("ready"))
                await ws.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                session = adapter.session_for("phone")
                _record_order(session, order)
                yield Rig(plugin.stream_hooks, session, conn, page, messages, order)
                page.task.cancel()
        finally:
            await adapter.disconnect()


def _record_order(session, order):
    """Each TTS chunk is recorded as the text its request has so far, so the notice
    shows up as one event once its last word went out."""
    send_tts, send_page = session._send_tts, session.send_page
    texts = {}

    async def recording_tts(chunk):
        await send_tts(chunk)
        if chunk.strip():
            rid = session.reply_request.request_id
            texts[rid] = texts.get(rid, "") + chunk
            order.append(("tts", texts[rid].strip()))

    async def recording_page(payload):
        order.append(("page", payload["type"]))
        await send_page(payload)

    session._send_tts, session.send_page = recording_tts, recording_page


async def _heard(rig, text, refusal=None):
    """VoiceManager hears ``text`` as a finished turn: a turn_end frame with the turn
    engine, a final transcription frame in classic mode."""
    if refusal:
        await rig.conn.send({"type": "transcription", "segment_index": 1, "text": text,
                             "endpoint": True, "is_final": True, "turn_id": 1})
    else:
        await rig.conn.send({"type": "turn_end", "text": text, "turn_id": 0})


async def _playing_reply(rig, refusal=None):
    """An utterance was answered and its reply is playing on the page."""
    if refusal:
        await wait_for(lambda: rig.conn.json_frames("turn_start"))
        await rig.conn.send({"type": "transcription", "segment_index": 0, "text": "tell me a story",
                             "endpoint": True, "is_final": False, "turn_id": 1})
    else:
        await rig.conn.send({"type": "turn_end", "text": "tell me a story", "turn_id": 0})
    await wait_for(lambda: rig.messages)
    message_id = rig.messages[-1].message_id
    fire(rig.hooks.on_stream_delta, message_id, delta="Once upon a time ", kind="text", turn_id="t1",
         iteration=1)
    frames = await wait_for(lambda: rig.tts())
    rid = frames[-1]["request_id"]
    await rig.conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await rig.conn.send(b"\x11\x00" * 240)
    await wait_for(lambda: any(k == "pcm" for k, _ in rig.page.log))
    return message_id, rid


async def _notice_played(rig, text):
    """The notice reached VoiceManager as a request of its own that was ended; the fake
    plays it and reports its end. Returns the notice's request id."""
    rid = await wait_for(lambda: _request_saying(rig, text))
    await wait_for(lambda: any(f["request_id"] == rid and f["done"] for f in rig.tts()))
    await rig.conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await rig.conn.send(b"\x22\x00" * 240)
    await rig.conn.send({"type": "audio_end", "request_id": rid, "reason": "completed"})
    return rid


def _request_saying(rig, text):
    """The id of the TTS request whose chunks so far make up ``text``, if any."""
    texts = {}
    for f in rig.tts():
        texts[f["request_id"]] = texts.get(f["request_id"], "") + f["text"]
    return next((rid for rid, t in texts.items() if t.strip() == text), None)


def _sequence(order, *wanted):
    """The events of ``order`` that are among ``wanted``, in order."""
    return [event for event in order if event in wanted]


@pytest.mark.parametrize("refusal", [None, "turn_engine_unsupported"])
async def test_end_the_session_cancels_the_reply_speaks_the_notice_and_closes_the_page(plugin, refusal):
    async with rig(plugin, refusal) as r:
        message_id, rid = await _playing_reply(r, refusal)
        await _heard(r, "End the session.", refusal)
        notice_rid = await _notice_played(r, "Session ended")
        await wait_for(lambda: r.page.closed)
        assert notice_rid != rid
        assert r.conn.json_frames("tts_cancel")[0]["request_id"] == rid  # the reply was cancelled
        assert r.page.of("clear")
        assert len(r.messages) == 1  # the command never reached Hermes
        assert r.messages[0].text == "tell me a story"
        assert [f["text"] for f in r.page.of("utterance")] == ["tell me a story"]
        assert r.page.of("session_end") == [{"type": "session_end"}]
        close = r.order[-1]
        assert close[0] == "closed"
        assert _sequence(r.order, ("tts", "Session ended"), ("page", "session_end"), close) == [
            ("tts", "Session ended"), ("page", "session_end"), close]
        fire(r.hooks.on_stream_delta, message_id, delta="there was a dragon", kind="text", turn_id="t1",
             iteration=1)
        await asyncio.sleep(0.1)
        assert "dragon" not in r.tts_text()
        await asyncio.wait_for(r.conn.closed.wait(), 2)  # the VoiceManager connection went with it


async def test_the_page_is_closed_when_the_notice_does_not_end_in_time(plugin, monkeypatch):
    monkeypatch.setattr(plugin.session, "SESSION_END_TIMEOUT_S", 0.2)
    async with rig(plugin) as r:
        await _heard(r, "end session")
        await wait_for(lambda: r.page.of("session_end"))
        assert not r.page.closed
        await wait_for(lambda: r.page.closed)
        assert r.messages == []
        assert _sequence(r.order, ("tts", "Session ended"), ("page", "session_end")) == [
            ("tts", "Session ended"), ("page", "session_end")]
        assert r.order[-1][0] == "closed"


async def test_end_session_now_please_is_an_ordinary_utterance(plugin):
    async with rig(plugin) as r:
        await _heard(r, "end session now please")
        await wait_for(lambda: r.messages)
        assert r.messages[0].text == "end session now please"
        await asyncio.sleep(0.1)
        assert r.page.of("session_end") == []
        assert "Session ended" not in r.tts_text()
        assert not r.page.closed


async def test_new_session_speaks_the_notice_sends_session_new_and_keeps_the_page_open(plugin):
    async with rig(plugin) as r:
        await _heard(r, "New session!")
        await _notice_played(r, "New session")
        await wait_for(lambda: r.page.of("session_new"))
        await asyncio.sleep(0.1)
        assert r.messages == []  # the command never reached Hermes
        assert r.page.of("utterance") == []
        assert r.page.of("session_new") == [{"type": "session_new"}]
        assert r.page.of("session_end") == []
        assert _sequence(r.order, ("tts", "New session"), ("page", "session_new")) == [
            ("tts", "New session"), ("page", "session_new")]
        assert not r.page.closed
        assert not r.conn.closed.is_set()
        # The session still takes the next utterance until the page reconnects.
        await _heard(r, "tell me a joke")
        await wait_for(lambda: r.messages)
        assert r.messages[0].text == "tell me a joke"
        assert [f["text"] for f in r.page.of("utterance")] == ["tell me a joke"]
        assert not r.page.closed


@pytest.mark.parametrize("refusal", [None, "turn_engine_unsupported"])
async def test_new_session_cancels_the_playing_reply_before_the_notice(plugin, refusal):
    async with rig(plugin, refusal) as r:
        message_id, rid = await _playing_reply(r, refusal)
        await _heard(r, "start a new session", refusal)
        notice_rid = await _notice_played(r, "New session")
        await wait_for(lambda: r.page.of("session_new"))
        assert notice_rid != rid
        assert r.conn.json_frames("tts_cancel")[0]["request_id"] == rid  # the reply was cancelled
        assert r.page.of("clear")
        assert len(r.messages) == 1
        assert r.messages[0].text == "tell me a story"
        assert _sequence(r.order, ("tts", "New session"), ("page", "session_new")) == [
            ("tts", "New session"), ("page", "session_new")]
        fire(r.hooks.on_stream_delta, message_id, delta="there was a dragon", kind="text", turn_id="t1",
             iteration=1)
        await asyncio.sleep(0.1)
        assert "dragon" not in r.tts_text()
        assert not r.page.closed
        assert not r.conn.closed.is_set()
        await _heard(r, "tell me a joke", refusal)
        await wait_for(lambda: len(r.messages) == 2)
        assert r.messages[1].text == "tell me a joke"


async def test_new_session_please_is_an_ordinary_utterance(plugin):
    async with rig(plugin) as r:
        await _heard(r, "new session please")
        await wait_for(lambda: r.messages)
        assert r.messages[0].text == "new session please"
        await asyncio.sleep(0.1)
        assert r.page.of("session_new") == []
        assert "New session" not in r.tts_text()
        assert not r.page.closed
