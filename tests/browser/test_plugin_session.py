"""The Desktop plugin's voice session, run in headless Chrome.

Turning voice on attaches the focused chat to the voice plugin: the socket
names the chat's stored id, the microphone goes out on it, each spoken
utterance is submitted to the chat by its runtime id, and the spoken part of
each reply (its <say> block) goes back as say frames. Turning voice off, moving
to another chat, unloading the plugin or losing the socket ends it.

The harness page loads desktop/plugin.js as the app does, with a host the test
controls; a fake voice server here speaks the plugin's WebSocket frames.
Chrome's fake microphone plays a tone WAV.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import math
import os
import struct
import threading
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

playwright_api = pytest.importorskip("playwright.async_api")
ws_server = pytest.importorskip("websockets.asyncio.server")

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "session-token"
FRAME_BYTES = 640


def _tone_wav(path, seconds=6, rate=48000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(seconds * rate):
            frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
        w.writeframes(bytes(frames))


async def wait_for(predicate, timeout: float = 5.0, step: float = 0.02):
    """Poll ``predicate`` (sync or async) until it returns a truthy value."""
    async with asyncio.timeout(timeout):
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                return result
            await asyncio.sleep(step)


class _Conn:
    """One client socket on the fake server."""

    def __init__(self, ws):
        self.ws = ws
        parts = urlsplit(ws.request.path)
        self.path = parts.path
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.bin_bytes = 0
        self.bin_sizes: set[int] = set()
        self.jsons: list[dict] = []
        self.closed = False

    async def send(self, frame: dict) -> None:
        await self.ws.send(json.dumps(frame))

    def says(self) -> list[dict]:
        return [f for f in self.jsons if f.get("type") in ("say", "say_end")]


class FakeVoiceServer:
    """Accepts plugin sockets and records what they send."""

    def __init__(self):
        self.conns: list[_Conn] = []
        self._server = None
        self.url = ""

    async def _handle(self, ws):
        conn = _Conn(ws)
        self.conns.append(conn)
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    conn.bin_bytes += len(message)
                    conn.bin_sizes.add(len(message))
                else:
                    conn.jsons.append(json.loads(message))
        except Exception:
            pass
        finally:
            conn.closed = True

    async def __aenter__(self):
        self._server = await ws_server.serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(_QuietHandler, directory=str(ROOT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def tone_wav(tmp_path_factory):
    wav = tmp_path_factory.mktemp("mic") / "tone.wav"
    _tone_wav(wav)
    return wav


@pytest.fixture
async def voice():
    async with FakeVoiceServer() as server:
        yield server


@pytest.fixture
async def page(site, tone_wav):
    pw = await playwright_api.async_playwright().start()
    browser = await pw.chromium.launch(
        executable_path=os.environ.get("CHROME") or None, headless=True,
        args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
              f"--use-file-for-fake-audio-capture={tone_wav}", "--autoplay-policy=no-user-gesture-required"])
    page = await browser.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    await page.goto(f"{site}/tests/browser/session_harness.html")
    await page.wait_for_function("() => window.harnessReady || window.harnessError", timeout=10000)
    harness_error = await page.evaluate("window.harnessError || ''")
    assert not harness_error, harness_error
    try:
        yield page
    finally:
        await browser.close()
        await pw.stop()
    assert not errors, errors


async def _boot(page, voice, address=None, token=TOKEN, runtime="rt-1", stored="st-1"):
    prefs = {"address": voice.url if address is None else address, "token": token}
    await page.evaluate("args => window.boot(args)", {"prefs": prefs, "runtime": runtime, "stored": stored})


async def _on(page, voice, count=1):
    """Toggle voice on and wait for the socket and the first mic frames."""
    await page.evaluate("window.clickToggle()")
    await wait_for(lambda: len(voice.conns) >= count)
    conn = voice.conns[count - 1]
    await conn.send({"type": "ready", "turn_engine": True})
    await wait_for(lambda: conn.bin_bytes >= 10 * FRAME_BYTES, timeout=10)
    return conn


async def _fire(page, type_, session_id, payload):
    await page.evaluate("([t, s, p]) => window.fire(t, s, p)", [type_, session_id, payload])


async def _stream(page, session_id, turn_id, chunks, segment=0, start=True, complete=True):
    """A reply as the gateway streams it: message.start, stamped deltas, message.complete."""
    if start:
        await _fire(page, "message.start", session_id, {"turn_id": turn_id})
    offset = 0
    for text in chunks:
        await _fire(page, "message.delta", session_id,
                    {"text": text, "turn_id": turn_id, "segment": segment, "offset": offset})
        offset += len(text)
    if complete:
        await _fire(page, "message.complete", session_id, {"text": "".join(chunks), "status": "complete"})


async def _quiet(seconds=0.3):
    await asyncio.sleep(seconds)


async def test_the_desktop_half_shows_under_the_plugin_name(page, voice):
    await _boot(page, voice)
    shown = await page.evaluate(
        "({id: window.plugin.default.id, name: window.plugin.default.name, settings: window.settingsPage.title})")
    assert shown == {"id": "voice-unleashed", "name": "Voice Unleashed", "settings": "Voice Unleashed"}


async def test_toggle_on_attaches_with_the_stored_id_and_the_token(page, voice):
    await _boot(page, voice)
    assert (await page.evaluate("window.toggleView()"))["pressed"] is False
    conn = await _on(page, voice)
    assert conn.path == "/ws"
    assert conn.query["token"] == TOKEN
    assert conn.query["attach"] == "st-1"
    assert conn.bin_sizes == {FRAME_BYTES}
    view = await wait_for(lambda: page.evaluate("window.toggleView().icon === 'icon:Mic' && window.toggleView()"))
    assert view["pressed"] is True and view["label"] == "Turn voice off"
    constraints = await page.evaluate("window.mics[0].constraints.audio")
    assert constraints["autoGainControl"] is True
    assert constraints["echoCancellation"] is True and constraints["noiseSuppression"] is True
    assert constraints["channelCount"] == 1


async def test_without_an_address_or_token_voice_stays_off_with_a_notice(page, voice):
    await _boot(page, voice, token="")
    await page.evaluate("window.clickToggle()")
    await _quiet()
    assert voice.conns == []
    assert await page.evaluate("window.notices.length") == 1
    assert await page.evaluate("window.mics.length") == 0
    assert (await page.evaluate("window.toggleView()"))["pressed"] is False


async def test_an_utterance_submits_one_prompt_with_the_runtime_id(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await conn.send({"type": "utterance", "text": "what is the time"})
    await wait_for(lambda: page.evaluate("window.submits().length"))
    await _quiet()
    submits = await page.evaluate("window.submits()")
    assert submits == [{"method": "prompt.submit",
                        "params": {"session_id": "rt-1", "text": "what is the time", "voice_turn": True}}]


async def test_reply_deltas_send_only_the_say_text_with_one_reply_key(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await _fire(page, "message.start", "rt-other", {"turn_id": "x1"})
    await _stream(page, "rt-1", "t1", ["<sa", "y>It is ", "noon.</s", "ay> The full ", "answer."], complete=False)
    await _stream(page, "rt-other", "x1", ["<say>Not this one.</say>"], start=False)
    await _fire(page, "message.complete", "rt-1", {"text": "", "status": "complete"})
    await wait_for(lambda: any(f["type"] == "say_end" for f in conn.jsons))
    await _quiet()
    says = conn.says()
    keys = {f["reply"] for f in says}
    assert len(keys) == 1 and all(isinstance(k, str) and k for k in keys)
    assert "".join(f.get("text", "") for f in says if f["type"] == "say") == "It is noon."
    assert says[-1]["type"] == "say_end"
    assert all(f.get("text") for f in says if f["type"] == "say")  # no empty say frames
    assert [f["type"] for f in says].count("say_end") == 1


async def test_a_say_block_still_open_at_complete_ends_with_say_end(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await _stream(page, "rt-1", "t1", ["<say>Done", " now</"])
    await wait_for(lambda: any(f["type"] == "say_end" for f in conn.jsons))
    says = conn.says()
    assert [(f["type"], f.get("text")) for f in says] == [("say", "Done"), ("say", " now"), ("say_end", None)]


async def test_each_reply_gets_its_own_key(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await _stream(page, "rt-1", "t1", ["<say>One.</say> rest"])
    await _stream(page, "rt-1", "t2", ["<say>Two.</say> rest"])
    await wait_for(lambda: [f["type"] for f in conn.jsons].count("say_end") == 2)
    says = conn.says()
    first, second = says[0]["reply"], says[-1]["reply"]
    assert first != second
    assert [(f["type"], f["reply"], f.get("text")) for f in says] == [
        ("say", first, "One."), ("say_end", first, None), ("say", second, "Two."), ("say_end", second, None)]


async def test_a_replayed_delta_is_not_spoken_twice(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await _fire(page, "message.start", "rt-1", {"turn_id": "t1"})
    for offset, text in ((0, "<say>Hel"), (8, "lo"), (8, "lo"), (0, "<say>Hel"), (10, " you.</say>")):
        await _fire(page, "message.delta", "rt-1", {"text": text, "turn_id": "t1", "segment": 0, "offset": offset})
    await _fire(page, "message.complete", "rt-1", {"status": "complete"})
    await wait_for(lambda: any(f["type"] == "say_end" for f in conn.jsons))
    assert "".join(f.get("text", "") for f in conn.says()) == "Hello you."


async def _cut_by_utterance(page, conn, text, epoch=None):
    """An utterance while a turn streams; returns once its prompt.submit is held."""
    count = await page.evaluate("window.submits().length")
    frame = {"type": "utterance", "text": text}
    if epoch is not None:
        frame["epoch"] = epoch
    await conn.send(frame)
    await wait_for(lambda: page.evaluate(f"window.submits().length > {count}"))


@pytest.mark.parametrize("cut", ["clear", "queued"])
async def test_a_clear_or_a_queued_utterance_silences_the_rest_of_the_turn(page, voice, cut):
    # A clear alone redirects nothing, and an utterance Hermes queues is
    # answered by a turn of its own: the turn that was cut stays silent.
    await _boot(page, voice)
    conn = await _on(page, voice)
    await page.evaluate("window.holdSubmits = true")
    await _stream(page, "rt-1", "t1", ["<say>First part. "], complete=False)
    await wait_for(lambda: conn.says())
    if cut == "clear":
        await conn.send({"type": "clear", "request_id": "q1"})
    else:
        await _cut_by_utterance(page, conn, "stop")
        await page.evaluate("window.answerSubmit({ status: 'queued' })")
    await _quiet()
    await _fire(page, "message.delta", "rt-1", {"text": "Second part.</say>", "turn_id": "t1", "segment": 0,
                                                "offset": 17})
    await _fire(page, "message.delta", "rt-1", {"text": "<say>Later.</say>", "turn_id": "t1", "segment": 1,
                                                "offset": 0})
    await _fire(page, "message.complete", "rt-1", {"status": "interrupted"})
    await _quiet()
    says = conn.says()
    assert [(f["type"], f.get("text")) for f in says] == [("say", "First part. ")]
    # The next reply is spoken again.
    await _stream(page, "rt-1", "t2", ["<say>Fresh.</say>"])
    await wait_for(lambda: any(f["type"] == "say_end" for f in conn.jsons))
    assert [f.get("text") for f in conn.says()[1:]] == ["Fresh.", None]


async def test_a_redirected_utterance_speaks_the_new_answer_of_the_same_turn(page, voice):
    # Hermes answers a prompt sent while a reply streams by redirecting that
    # turn: the model request is cancelled and the answer streams on in the
    # same turn and segment, with no event between the two, its offsets going
    # on from the old answer's and its first text after a paragraph break.
    await _boot(page, voice)
    conn = await _on(page, voice)
    await page.evaluate("window.holdSubmits = true")
    offset = 0

    async def delta(text):
        nonlocal offset
        await _fire(page, "message.delta", "rt-1", {"text": text, "turn_id": "t1", "segment": 0, "offset": offset})
        offset += len(text)

    await _fire(page, "message.start", "rt-1", {"turn_id": "t1"})
    await delta("<say>First part. ")
    await wait_for(lambda: conn.says())
    await _cut_by_utterance(page, conn, "tell me about the weather", epoch=2)
    # The old answer goes on until Hermes has taken the utterance in.
    await delta("Second part.</say> Old screen text")
    await page.evaluate("window.answerSubmit({ status: 'redirected' })")
    await _quiet()
    # A replay of the old answer is not the new one.
    await _fire(page, "message.delta", "rt-1", {"text": "<say>First part. ", "turn_id": "t1", "segment": 0,
                                                "offset": 0})
    await delta("\n\n<say>It is sunny.</say>")
    await delta(" The full forecast.")
    await _fire(page, "message.complete", "rt-1", {"status": "complete"})
    await wait_for(lambda: any(f["type"] == "say_end" for f in conn.jsons))
    await _quiet()
    says = conn.says()
    assert [(f["type"], f.get("text"), f.get("epoch")) for f in says] == [
        ("say", "First part. ", 0), ("say", "It is sunny.", 2), ("say_end", None, 2)]
    assert says[1]["reply"] == says[2]["reply"] != says[0]["reply"]


async def test_a_steered_utterance_speaks_the_turns_next_segment(page, voice):
    # During a tool call Hermes steers the turn: the answer is the text after
    # the tools, the turn's next segment.
    await _boot(page, voice)
    conn = await _on(page, voice)
    await page.evaluate("window.holdSubmits = true")
    await _stream(page, "rt-1", "t1", ["<say>Looking.</say> Checking the files."], complete=False)
    await _fire(page, "message.interim", "rt-1", {"text": "<say>Looking.</say> Checking the files.",
                                                  "already_streamed": True, "turn_id": "t1", "segment": 0})
    await wait_for(lambda: any(f["type"] == "say_end" for f in conn.jsons))
    await _cut_by_utterance(page, conn, "only the tests", epoch=1)
    await page.evaluate("window.answerSubmit({ status: 'redirected' })")
    await _quiet()
    await _stream(page, "rt-1", "t1", ["\n\n<say>Only the tests.</say> Done."], segment=1, start=False)
    await wait_for(lambda: [f["type"] for f in conn.jsons].count("say_end") == 2)
    says = conn.says()
    assert [(f["type"], f.get("text"), f.get("epoch")) for f in says] == [
        ("say", "Looking.", 0), ("say_end", None, 0), ("say", "Only the tests.", 1), ("say_end", None, 1)]
    assert says[2]["reply"] == says[3]["reply"] != says[0]["reply"]


async def test_a_replayed_earlier_segment_of_a_resumed_turn_is_not_spoken_again(page, voice):
    # After a resume the turn's segments get new keys; a replay of a segment that
    # was spoken before the cut (a reconnect) must still not be spoken twice.
    await _boot(page, voice)
    conn = await _on(page, voice)
    await page.evaluate("window.holdSubmits = true")
    await _stream(page, "rt-1", "t1", ["<say>Looking.</say> Checking the files."], complete=False)
    await _fire(page, "message.interim", "rt-1", {"text": "<say>Looking.</say> Checking the files.",
                                                  "already_streamed": True, "turn_id": "t1", "segment": 0})
    await wait_for(lambda: any(f["type"] == "say_end" for f in conn.jsons))
    await _cut_by_utterance(page, conn, "only the tests", epoch=1)
    await page.evaluate("window.answerSubmit({ status: 'steered' })")
    await _quiet()
    await _stream(page, "rt-1", "t1", ["\n\n<say>Only the tests.</say> Done."], segment=1, start=False)
    await wait_for(lambda: [f["type"] for f in conn.jsons].count("say_end") == 2)
    await _stream(page, "rt-1", "t1", ["<say>Looking.</say> Checking the files."], start=False, complete=False)
    await _quiet()
    assert [f.get("text") for f in conn.says() if f["type"] == "say"] == ["Looking.", "Only the tests."]


async def test_say_frames_carry_the_epoch_of_the_last_utterance_or_clear(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)

    def say_ends(count):
        return lambda: [f["type"] for f in conn.jsons].count("say_end") == count

    await _stream(page, "rt-1", "t0", ["<say>Typed.</say>"])
    await wait_for(say_ends(1))
    await conn.send({"type": "utterance", "text": "hello", "epoch": 3})
    await _quiet()
    await _stream(page, "rt-1", "t1", ["<say>One.</say>"])
    await wait_for(say_ends(2))
    await conn.send({"type": "clear", "request_id": None, "epoch": 4})
    await conn.send({"type": "clear", "request_id": None})  # no epoch: the last one stays
    await _quiet()
    await _stream(page, "rt-1", "t2", ["<say>Two.</say>"])
    await wait_for(say_ends(3))
    assert [(f["type"], f.get("text"), f.get("epoch")) for f in conn.says()] == [
        ("say", "Typed.", 0), ("say_end", None, 0), ("say", "One.", 3), ("say_end", None, 3),
        ("say", "Two.", 4), ("say_end", None, 4)]


async def test_a_reattach_starts_again_at_epoch_zero(page, voice):
    await _boot(page, voice)
    first = await _on(page, voice)
    await first.send({"type": "clear", "request_id": None, "epoch": 7})
    await _quiet()
    await page.evaluate("window.host.state.focusedStoredSessionId.set('st-1-next')")
    await wait_for(lambda: first.closed and len(voice.conns) == 2)
    second = voice.conns[1]
    await _stream(page, "rt-1", "t1", ["<say>Again.</say>"])
    await wait_for(lambda: any(f["type"] == "say_end" for f in second.jsons))
    assert [f.get("epoch") for f in second.says()] == [0, 0]


async def test_switching_chats_closes_the_socket_and_submits_nothing_for_the_old_chat(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await page.evaluate("""() => {
      window.oldSocket = window.sockets[window.sockets.length - 1]
      window.host.state.focusedStoredSessionId.set('st-2')
      window.host.state.focusedSessionId.set('rt-2')
    }""")
    await wait_for(lambda: conn.closed)
    # A late frame on the old socket is ignored.
    await page.evaluate("""() => window.oldSocket.dispatchEvent(
      new MessageEvent('message', { data: JSON.stringify({ type: 'utterance', text: 'late' }) }))""")
    await _stream(page, "rt-1", "t1", ["<say>Old chat.</say>"])
    await _quiet(0.5)
    assert await page.evaluate("window.submits()") == []
    assert len(voice.conns) == 1  # never attached to the other chat
    assert (await page.evaluate("window.toggleView()"))["pressed"] is False
    tracks = await page.evaluate("window.mics[0].stream.getTracks().map(t => t.readyState)")
    assert set(tracks) == {"ended"}


async def test_a_stored_id_change_in_the_same_chat_reattaches(page, voice):
    await _boot(page, voice)
    first = await _on(page, voice)
    await page.evaluate("window.host.state.focusedStoredSessionId.set('st-1-next')")
    await wait_for(lambda: first.closed and len(voice.conns) == 2)
    second = voice.conns[1]
    assert second.query["attach"] == "st-1-next" and second.query["token"] == TOKEN
    await wait_for(lambda: second.bin_bytes >= 10 * FRAME_BYTES, timeout=10)
    await second.send({"type": "utterance", "text": "still here"})
    await wait_for(lambda: page.evaluate("window.submits().length"))
    submits = await page.evaluate("window.submits()")
    assert [s["params"]["session_id"] for s in submits] == ["rt-1"]
    assert (await page.evaluate("window.toggleView()"))["pressed"] is True


async def test_toggle_off_closes_the_socket_and_stops_the_audio(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await page.evaluate("window.clickToggle()")
    await wait_for(lambda: conn.closed)
    assert (await page.evaluate("window.toggleView()"))["pressed"] is False
    tracks = await page.evaluate("window.mics[0].stream.getTracks().map(t => t.readyState)")
    assert set(tracks) == {"ended"}
    await page.wait_for_function("() => window.contexts.every(c => c.state === 'closed')", timeout=2000)
    sent = conn.bin_bytes
    await _quiet()
    assert conn.bin_bytes == sent
    # Voice can be turned on again.
    await _on(page, voice, count=2)


async def test_unloading_the_plugin_ends_the_session(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await page.evaluate("window.dispose()")
    await wait_for(lambda: conn.closed)
    tracks = await page.evaluate("window.mics[0].stream.getTracks().map(t => t.readyState)")
    assert set(tracks) == {"ended"}


async def test_replaced_turns_voice_off_without_reconnecting(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await conn.send({"type": "replaced"})
    await conn.ws.close(code=4001, reason="replaced")
    await wait_for(lambda: page.evaluate("!window.toggleView().pressed"))
    await _quiet(1.0)
    assert len(voice.conns) == 1
    assert await page.evaluate("window.notices.length") >= 1
    tracks = await page.evaluate("window.mics[0].stream.getTracks().map(t => t.readyState)")
    assert set(tracks) == {"ended"}


async def test_close_code_4001_alone_turns_voice_off_without_reconnecting(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await conn.ws.close(code=4001, reason="replaced")
    await wait_for(lambda: page.evaluate("!window.toggleView().pressed"))
    await _quiet(1.0)
    assert len(voice.conns) == 1


async def test_the_server_closing_the_socket_ends_the_session(page, voice):
    await _boot(page, voice)
    conn = await _on(page, voice)
    await conn.ws.close(code=1011, reason="gone")
    await wait_for(lambda: page.evaluate("!window.toggleView().pressed"))
    await _stream(page, "rt-1", "t1", ["<say>Nobody hears this.</say>"])
    await _quiet()
    assert conn.says() == []
    assert await page.evaluate("window.notices.length") >= 1
