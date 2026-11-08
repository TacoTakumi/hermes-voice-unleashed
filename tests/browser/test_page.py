"""The voice page in headless Chrome, against the plugin's server and a fake VoiceManager.

Chrome's fake microphone plays a 440 Hz tone WAV. The test checks that the
plugin gets it as 16 kHz mono PCM, that a reply's audio plays and its
playback edges come back, and that the page shows what was said.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import struct
import sys
import time
import wave
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from fake_vm import FakeVoiceManager, wait_for

playwright_api = pytest.importorskip("playwright.async_api")

TOKEN = "page-token"
TONE_HZ = 440


def _session_rows(now):
    """What GET /sessions answers: a titled session, one with a preview only (the lane's
    current one) and one with neither."""
    return [
        {"id": "sid-titled-111", "title": "Garden plans", "preview": "what should I plant",
         "last_active": now - 300, "message_count": 12, "source": "telegram", "current": False},
        {"id": "sid-untitled-222", "title": None, "preview": "remind me about the dentist",
         "last_active": now - 7200, "message_count": 1, "source": "voicemanager_live", "current": True},
        {"id": "sid-bare-333", "title": None, "preview": "", "last_active": now - 3 * 86400,
         "message_count": 0, "source": "cli", "current": False},
    ]


def _tone_wav(path, seconds=6, rate=48000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(seconds * rate):
            frames += struct.pack("<h", int(12000 * math.sin(2 * math.pi * TONE_HZ * i / rate)))
        w.writeframes(bytes(frames))


def _dominant_hz(pcm: bytes, rate: int) -> float:
    """Frequency from zero crossings, reading ``pcm`` at ``rate``."""
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / rate)


@contextlib.asynccontextmanager
async def _site(tmp_path, init_script=None):
    """A VoiceServer in front of a fake VoiceManager, and a headless Chrome page not yet
    pointed at it. ``init_script`` is a script the page runs before its own, to stub a
    browser API. The server answers GET /sessions with three rows and records the query
    of every /ws request (``ws_requests``) and the client of every session list request
    (``list_requests``). The frames in ``before_ready`` go to the page on every connect
    before the session starts, as the plugin sends its connect frames. With
    ``join_failed_message`` set, a connect gets a join_failed frame with that message and
    the join-failed close instead of a session. A session whose page connection is gone
    lands in ``ended``."""
    plugin = sys.modules["voicemanager_live"]
    from voicemanager_live.server import JOIN_FAILED_CLOSE_CODE, VoiceServer
    from voicemanager_live.session import VoiceSession, client_from_url

    async with FakeVoiceManager() as fake:
        site = SimpleNamespace(fake=fake, sessions=[], ended=[], utterances=[], ws_requests=[], list_requests=[],
                               rows=_session_rows(int(time.time())), before_ready=[], join_failed_message=None)

        async def on_client(connection, client_id, **options):
            site.ws_requests.append(parse_qs(urlsplit(connection.request.path).query))
            if site.join_failed_message is not None:
                await connection.send(json.dumps({"type": "join_failed", "message": site.join_failed_message}))
                await connection.close(code=JOIN_FAILED_CLOSE_CODE, reason="join failed")
                return
            for frame in site.before_ready:
                await connection.send(json.dumps(frame))

            async def on_utterance(text, message_id):
                site.utterances.append((text, message_id))

            session = VoiceSession(connection, client_from_url(fake.url), client_id, on_utterance=on_utterance)
            site.sessions.append(session)
            try:
                await session.run()
            finally:
                site.ended.append(session)

        def on_sessions(client_id):
            site.list_requests.append(client_id)
            return site.rows

        server = VoiceServer("127.0.0.1", 0, TOKEN, on_client, on_sessions=on_sessions)
        await server.start()
        site.url = f"http://127.0.0.1:{server.bound_port}"
        wav = tmp_path / "tone.wav"
        _tone_wav(wav)
        async with playwright_api.async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=os.environ.get("CHROME") or None, headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                      f"--use-file-for-fake-audio-capture={wav}", "--autoplay-policy=no-user-gesture-required"])
            site.page = await browser.new_page(viewport={"width": 390, "height": 760})
            if init_script:
                await site.page.add_init_script(init_script)
            try:
                yield site
            finally:
                await browser.close()
                await server.stop()
        assert plugin  # loaded by conftest


@pytest.fixture
async def stack(tmp_path, request):
    """The page, started in a new session from the chooser, on a VoiceServer in front of
    a fake VoiceManager. An indirect parameter is the init script of ``_site``."""
    async with _site(tmp_path, getattr(request, "param", None)) as site:
        await site.page.goto(f"{site.url}/?token={TOKEN}&client=phone")
        await site.page.click("#new")
        yield site.fake, site.page, site.sessions, site.utterances


@pytest.fixture
async def site(tmp_path, request):
    """The server and a page not yet loaded: the chooser tests pick their own URL. An
    indirect parameter is the init script of ``_site``."""
    async with _site(tmp_path, getattr(request, "param", None)) as site:
        yield site


async def _chooser_rows(page):
    await page.wait_for_function("document.querySelectorAll('#sessions .session').length === 3", timeout=5000)
    return await page.eval_on_selector_all("#sessions .session", "els => els.map(e => e.textContent)")


async def test_the_chooser_shows_new_first_then_the_rows_as_received(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&client=tablet")
    texts = await _chooser_rows(page)
    assert await page.is_visible("#chooser") and await page.is_hidden("#toggle")
    assert site.list_requests == ["tablet"]
    assert await page.evaluate(
        "document.getElementById('new').compareDocumentPosition(document.getElementById('sessions'))"
        " & Node.DOCUMENT_POSITION_FOLLOWING")
    titled, preview, bare = texts
    assert "Garden plans" in titled and "what should I plant" not in titled
    assert "remind me about the dentist" in preview
    names = await page.eval_on_selector_all("#sessions .session .name", "els => els.map(e => e.textContent)")
    assert names[0] == "Garden plans" and names[1] == "remind me about the dentist"
    assert re.search(r"\d", names[2]) and len(names[2]) <= 40  # the last-active time, nothing else
    assert "5 min ago" in titled and "12 lines" in titled and "telegram" in titled
    assert "2 h ago" in preview and "1 line" in preview and "1 lines" not in preview and "voicemanager_live" in preview
    assert "3 days ago" in bare and "0 lines" in bare and "cli" in bare
    current = await page.eval_on_selector_all("#sessions .session", "els => els.map(e => !!e.querySelector('[aria-current=\"true\"]'))")
    assert current == [False, True, False]
    assert "Current" in preview and "Current" not in titled and "Current" not in bare
    for text in texts:
        for row in site.rows:
            assert row["id"] not in text, (row["id"], text)


async def test_tapping_a_chooser_row_connects_with_that_session_and_the_lane(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&client=tablet")
    await _chooser_rows(page)
    await page.click("#sessions .session:nth-child(2) button")
    await site.fake.wait_connections(1, timeout=10)
    assert site.ws_requests[0]["session"] == ["sid-untitled-222"]
    assert site.ws_requests[0]["client"] == ["tablet"]
    assert await page.is_hidden("#chooser") and await page.is_visible("#toggle")
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)


async def test_tapping_new_in_the_chooser_connects_with_session_new(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&client=tablet")
    await _chooser_rows(page)
    await page.click("#new")
    await site.fake.wait_connections(1, timeout=10)
    assert site.ws_requests[0]["session"] == ["new"]
    assert site.ws_requests[0]["client"] == ["tablet"]
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)


async def test_a_session_in_the_page_url_skips_the_chooser(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&client=tablet&session=sid-titled-111")
    await asyncio.sleep(0.3)
    assert await page.is_hidden("#chooser") and await page.is_visible("#toggle")
    assert await page.text_content("#state") == "Tap to start"
    assert site.list_requests == []
    await page.click("#toggle")
    await site.fake.wait_connections(1, timeout=10)
    assert site.ws_requests[0]["session"] == ["sid-titled-111"]
    assert site.ws_requests[0]["client"] == ["tablet"]
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)


async def test_a_reconnect_after_the_chooser_does_not_ask_for_the_session_again(site):
    """The choice is sent once. A reconnect stays on the session the lane is on: asking
    for a new session again would open another one each time."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&client=tablet")
    await _chooser_rows(page)
    await page.click("#new")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)
    await site.sessions[0].page.close()
    await wait_for(lambda: len(site.ws_requests) == 2, timeout=10)
    assert site.ws_requests[0]["session"] == ["new"]
    assert "session" not in site.ws_requests[1]
    assert site.ws_requests[1]["client"] == ["tablet"]


HISTORY_LINES = [
    {"role": "user", "text": "what should I plant"},
    {"role": "hermes", "text": "Tomatoes and basil, they like the same bed."},
    {"role": "user", "text": "when do I sow them"},
]

# Installed before the connect: how many turns the log held the moment the page showed
# Listening, so the test can tell the history was drawn before the ready state.
TURNS_AT_LISTENING = """() => {
  window.turnsAtListening = null;
  const state = document.getElementById('state');
  new MutationObserver(() => {
    if (state.textContent === 'Listening' && window.turnsAtListening === null)
      window.turnsAtListening = document.querySelectorAll('#log .turn').length;
  }).observe(state, { childList: true, characterData: true, subtree: true });
}"""


async def _log_turns(page):
    return await page.eval_on_selector_all(
        "#log .turn", "els => els.map(e => [e.className, e.textContent])")


async def test_the_history_frame_shows_its_lines_once_per_connect(site):
    """The history frame draws one turn per line, in order and with the role's style,
    before the page is Listening. A reconnect gets the same frame and the log shows the
    lines once, not twice."""
    page = site.page
    site.before_ready.append({"type": "history", "session_id": "sid-titled-111", "lines": HISTORY_LINES})
    await page.goto(f"{site.url}/?token={TOKEN}&client=tablet&session=sid-titled-111")
    await page.evaluate(TURNS_AT_LISTENING)
    await page.click("#toggle")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)
    expected = [["turn user", "what should I plant"],
                ["turn hermes", "Tomatoes and basil, they like the same bed."],
                ["turn user", "when do I sow them"]]
    assert await _log_turns(page) == expected
    assert await page.evaluate("window.turnsAtListening") == 3
    await site.sessions[0].page.close()
    await wait_for(lambda: len(site.sessions) == 2, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)
    await asyncio.sleep(0.3)
    assert await _log_turns(page) == expected


# The mic streams the page opened and the wake locks it took, kept where a test can read
# them: the page keeps both to itself.
MIC_AND_WAKE_LOCK = """
window.micStreams = [];
const realGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async (constraints) => {
  const stream = await realGetUserMedia(constraints);
  window.micStreams.push(stream);
  return stream;
};
window.wakeLocks = { requested: 0, released: 0 };
Object.defineProperty(navigator, 'wakeLock', { configurable: true, value: {
  request: async () => {
    window.wakeLocks.requested += 1;
    return { release: async () => { window.wakeLocks.released += 1; } };
  },
} });
"""

# Installed once the page is Listening: every state the header shows from then on.
STATES_SEEN = """() => {
  window.statesSeen = [];
  const state = document.getElementById('state');
  new MutationObserver(() => { window.statesSeen.push(state.textContent); })
    .observe(state, { childList: true, characterData: true, subtree: true });
}"""

MIC_TRACK_STATES = "window.micStreams.flatMap(s => s.getAudioTracks().map(t => t.readyState))"


async def _listening_from_the_chooser(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&client=phone")
    await _chooser_rows(page)
    await page.click("#new")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)
    assert site.list_requests == ["phone"]
    assert await page.evaluate(MIC_TRACK_STATES) == ["live"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 0}
    await page.evaluate(STATES_SEEN)


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_session_end_frame_stops_the_page_and_shows_the_chooser_again(site):
    """The user ended the session by voice. The page lets go of the mic and the wake lock,
    closes its socket for good and offers the sessions again, freshly listed."""
    page = site.page
    await _listening_from_the_chooser(site)
    await site.sessions[0].send_page({"type": "session_end"})
    await page.wait_for_selector("#chooser:not([hidden])", timeout=5000)
    await wait_for(lambda: len(site.ended) == 1, timeout=10)
    await wait_for(lambda: len(site.list_requests) == 2, timeout=5)
    await asyncio.sleep(1.0)  # long enough for a first reconnect try
    assert await page.evaluate(MIC_TRACK_STATES) == ["ended"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 1}
    assert await page.is_visible("#chooser") and await page.is_hidden("#toggle")
    assert await page.text_content("#state") == "Sessions"
    assert site.list_requests == ["phone", "phone"]
    assert len(site.ws_requests) == 1  # no reconnect
    states = await page.evaluate("window.statesSeen")
    assert "Reconnecting" not in states and "In use on another device" not in states, states
    await _chooser_rows(page)


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_session_new_frame_reconnects_with_session_new(site):
    """The user asked for a new session by voice. The page closes its socket itself and
    opens a new one asking for a new session; the mic stays on and the hand-over path
    (another page took over) stays quiet."""
    page = site.page
    await _listening_from_the_chooser(site)
    await site.sessions[0].send_page({"type": "session_new"})
    await wait_for(lambda: len(site.ws_requests) == 2, timeout=10)
    await wait_for(lambda: len(site.ended) == 1, timeout=10)
    await site.fake.wait_connections(2, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)
    assert site.ws_requests[1]["session"] == ["new"]
    assert site.ws_requests[1]["client"] == ["phone"]
    assert site.ended[0] is site.sessions[0]
    assert await page.evaluate(MIC_TRACK_STATES) == ["live"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 0}
    assert await page.text_content("#toggle") == "Mute microphone"
    assert await page.is_hidden("#chooser")
    states = await page.evaluate("window.statesSeen")
    assert "In use on another device" not in states and "Reconnecting" not in states, states
    assert site.list_requests == ["phone"]


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_join_failed_frame_shows_the_chooser_with_the_message(site):
    """The session the page asked for could not be joined: the server says so and closes
    with its own code. The page shows the chooser with the message and does not reconnect."""
    page = site.page
    site.join_failed_message = "could not join session sid-titled-111: no such session"
    await page.goto(f"{site.url}/?token={TOKEN}&client=phone&session=sid-titled-111")
    await page.evaluate(STATES_SEEN)
    await page.click("#toggle")
    await wait_for(lambda: len(site.ws_requests) == 1, timeout=10)
    await page.wait_for_selector("#chooser:not([hidden])", timeout=5000)
    await wait_for(lambda: len(site.list_requests) == 1, timeout=5)
    await asyncio.sleep(1.0)  # long enough for a first reconnect try
    assert "could not join session sid-titled-111: no such session" in await page.text_content("#detail")
    assert await page.text_content("#state") == "Sessions"
    assert await page.is_visible("#chooser") and await page.is_hidden("#toggle")
    assert site.ws_requests[0]["session"] == ["sid-titled-111"]
    assert len(site.ws_requests) == 1  # no reconnect
    assert site.sessions == []
    assert await page.evaluate(MIC_TRACK_STATES) == ["ended"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 1}
    states = await page.evaluate("window.statesSeen")
    assert "Reconnecting" not in states and "In use on another device" not in states, states
    await _chooser_rows(page)


async def test_mic_reaches_voicemanager_as_16k_mono_pcm(stack):
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    await wait_for(lambda: len(conn.audio) >= 2 * 16000 * 2, timeout=15)  # 2 s at 16 kHz
    assert conn.frames[0] == {"type": "turn_engine", "engine": "tyger"}
    pcm = conn.audio[16000 * 2: 2 * 16000 * 2]  # skip the first second (device warm-up)
    hz = _dominant_hz(pcm, 16000)
    assert abs(hz - TONE_HZ) < 25, f"tone read at 16 kHz is {hz:.0f} Hz"
    assert all(len(f) == 640 for f in conn.frames[1:] if isinstance(f, bytes))  # 20 ms mono frames


async def test_a_turn_plays_its_reply_and_shows_its_text(stack):
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    await conn.send({"type": "transcription", "segment_index": 0, "text": "what is the weather",
                     "endpoint": False, "is_final": False, "turn_id": 0})
    await page.wait_for_function(
        "document.getElementById('transcript').textContent === 'what is the weather'", timeout=5000)
    await conn.send({"type": "turn_end", "text": "what is the weather", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session = sessions[0]
    reply_id = utterances[0][1]
    await session.on_reply_delta(reply_id, "t1", 1, "Sunny and warm, ")
    await session.on_reply_delta(reply_id, "t1", 1, "about twenty degrees.")
    await session.on_turn_done(reply_id, "t1", False)
    frames = await wait_for(lambda: [f for f in conn.json_frames("tts_stream")] if any(
        f.get("done") for f in conn.json_frames("tts_stream")) else None)
    rid = frames[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    tone = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 300 * i / 24000)))
                    for i in range(12000))  # 0.5 s
    for start in range(0, len(tone), 4800):
        await conn.send(tone[start:start + 4800])
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=10)
    assert [f["event"] for f in playback[:2]] == ["start", "stop"]
    assert all(f["request_id"] == rid for f in playback[:2])
    assert playback[1]["reply_sample"] == 12000
    user = await page.text_content("#log .turn.user")
    hermes = await page.text_content("#log .turn.hermes")
    assert user == "what is the weather"
    assert hermes == "Sunny and warm, about twenty degrees."
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=5000)
    if os.environ.get("PAGE_SHOT"):  # a picture of the page, for a look by eye
        await asyncio.sleep(0.3)
        await page.screenshot(path=os.environ["PAGE_SHOT"])


async def test_reply_finishes_when_audio_end_comes_after_the_audio(stack):
    """VoiceManager paces audio and the text ends only at turn end, so audio_end can
    arrive after every chunk has already played."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    await conn.send({"type": "turn_end", "text": "hello", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.on_reply_delta(reply_id, "t1", 1, "Hi. ")  # a word boundary: whole words go to TTS at once
    rid = (await wait_for(lambda: conn.json_frames("tts_stream")))[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x10\x00" * 4800)  # 0.2 s
    await wait_for(lambda: conn.json_frames("playback"), timeout=10)  # it started playing
    await asyncio.sleep(1.0)  # ... and finished long before the reply's text ends
    await session.on_turn_done(reply_id, "t1", False)
    await wait_for(lambda: any(f.get("done") for f in conn.json_frames("tts_stream")))
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=5)
    assert [f["event"] for f in playback] == ["start", "stop"]
    assert playback[1]["reply_sample"] == 4800
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=5000)


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def _reply_request(conn, page, sessions, utterances, text, ready="Listening"):
    """Say one turn and open its reply on the fake VoiceManager; returns the TTS request id.
    ``ready`` is the state the page shows once it is connected and waiting."""
    await page.wait_for_function(f"document.getElementById('state').textContent === {ready!r}",
                                 timeout=10000)
    await conn.send({"type": "turn_end", "text": text, "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.on_reply_delta(reply_id, "t1", 1, "Hi. ")
    rid = (await wait_for(lambda: conn.json_frames("tts_stream")))[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    return rid


async def _start_frame(conn):
    """The start playback frame, with the mic samples the fake VoiceManager had when it came."""
    seen = {}

    def check():
        frames = conn.json_frames("playback")
        if frames and "sent" not in seen:
            seen["sent"] = len(conn.audio) // 2
        return frames or None

    (start, *_) = await wait_for(check, timeout=10)
    return start, seen["sent"]


async def test_playback_frames_carry_the_mic_sample_of_a_reply_played_to_the_end(stack):
    """The edges are placed on the mic stream: the start near the count of mic samples
    sent when it went out, the stop one reply length after it."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    rid = await _reply_request(conn, page, sessions, utterances, "hello")
    for _ in range(5):
        await conn.send(b"\x10\x00" * 2400)  # 5 x 0.1 s at 24 kHz
    start, sent_at_start = await _start_frame(conn)
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=10)
    start, stop = playback[:2]
    assert (start["event"], stop["event"]) == ("start", "stop")
    assert _is_count(start["mic_sample"]) and _is_count(stop["mic_sample"])
    assert abs(start["mic_sample"] - sent_at_start) <= 8000, (start["mic_sample"], sent_at_start)
    assert stop["reply_sample"] == 12000
    assert abs((stop["mic_sample"] - start["mic_sample"]) - 0.5 * 16000) <= 8000, (start, stop)


async def test_playback_frames_carry_the_mic_sample_of_a_reply_cleared_part_way(stack):
    """A reply cut by a barge stops where it was cut: the mic span matches the played part."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    rid = await _reply_request(conn, page, sessions, utterances, "hello")
    for _ in range(10):
        await conn.send(b"\x10\x00" * 4800)  # 10 x 0.2 s at 24 kHz
    start, _ = await _start_frame(conn)
    await asyncio.sleep(0.5)
    await conn.send({"type": "barge", "request_id": rid, "onset_mic_sample": 0, "mic_sample": 0})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=10)
    start, stop = playback[:2]
    assert (start["event"], stop["event"]) == ("start", "stop")
    assert _is_count(start["mic_sample"]) and _is_count(stop["mic_sample"])
    assert 0 < stop["reply_sample"] < 48000
    assert stop["mic_sample"] >= start["mic_sample"]
    span = stop["mic_sample"] - start["mic_sample"]
    assert abs(span - stop["reply_sample"] * 16000 / 24000) <= 8000, (start, stop)


async def test_playback_frames_of_a_quiet_reply_cleared_after_it_played_out_stop_at_its_last_sample(stack):
    """A reply whose audio has played out, with no audio_end yet (the turn went quiet), is
    cleared by a barge: the stop edge is the end of the last sample heard, not the clear
    time, so the mic span matches the sound and not the silence after it."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    rid = await _reply_request(conn, page, sessions, utterances, "hello")
    for _ in range(5):
        await conn.send(b"\x10\x00" * 2400)  # 5 x 0.1 s at 24 kHz
    start, _ = await _start_frame(conn)
    await asyncio.sleep(1.5)  # well past the 0.5 s of sound: the page is quiet
    await conn.send({"type": "barge", "request_id": rid, "onset_mic_sample": 0, "mic_sample": 0})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=10)
    start, stop = playback[:2]
    assert (start["event"], stop["event"]) == ("start", "stop")
    assert _is_count(start["mic_sample"]) and _is_count(stop["mic_sample"])
    assert stop["reply_sample"] == 12000
    assert stop["mic_sample"] >= start["mic_sample"]
    assert abs((stop["mic_sample"] - start["mic_sample"]) - 0.5 * 16000) <= 8000, (start, stop)


async def test_playback_frames_of_a_muted_mic_map_to_the_end_of_the_sent_stream(stack):
    """While the mic is muted no mic audio goes out, so both edges of a reply sit at the
    count of samples sent before the mute: the end of the stream the server has."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    await wait_for(lambda: len(conn.audio) > 0, timeout=15)
    await page.click("#toggle")  # mute
    await asyncio.sleep(0.1)  # frames already in flight land
    sent = len(conn.audio) // 2
    rid = await _reply_request(conn, page, sessions, utterances, "hello", ready="Muted")
    for _ in range(5):
        await conn.send(b"\x10\x00" * 2400)  # 5 x 0.1 s at 24 kHz
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=10)
    start, stop = playback[:2]
    assert (start["event"], stop["event"]) == ("start", "stop")
    assert _is_count(start["mic_sample"]) and _is_count(stop["mic_sample"])
    assert len(conn.audio) // 2 == sent  # nothing more was sent while muted
    assert start["mic_sample"] == sent, (start, sent)
    assert stop["mic_sample"] == sent, (stop, sent)
    assert stop["reply_sample"] == 12000


async def test_the_stop_edge_is_clamped_to_the_start_edge(stack):
    """The start edge is extrapolated past the sent count (output latency and the
    scheduling lead). A mute right after the start freezes the count, and a muted stop
    maps to that frozen count, which lies before the start. VoiceManager refuses a stop
    before its start, so the page clamps the stop to the start edge."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    rid = await _reply_request(conn, page, sessions, utterances, "hello")
    # Mute in the same task that sends the start frame: it shows Speaking right after.
    await page.evaluate("""() => {
      const state = document.getElementById('state');
      new MutationObserver((_, obs) => {
        if (state.textContent !== 'Speaking') return;
        obs.disconnect();
        document.getElementById('toggle').click();
      }).observe(state, { childList: true, characterData: true, subtree: true });
    }""")
    for _ in range(5):
        await conn.send(b"\x10\x00" * 2400)  # 5 x 0.1 s at 24 kHz
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=10)
    start, stop = playback[:2]
    assert (start["event"], stop["event"]) == ("start", "stop")
    assert _is_count(start["mic_sample"]) and _is_count(stop["mic_sample"])
    assert await page.evaluate("document.getElementById('toggle').getAttribute('aria-pressed')") == "true"
    await asyncio.sleep(0.1)  # frames in flight before the mute land
    frozen = len(conn.audio) // 2
    assert start["mic_sample"] > frozen, (start, frozen)  # the unclamped stop would sit here
    assert stop["mic_sample"] >= start["mic_sample"], (start, stop)
    assert stop["mic_sample"] == max(start["mic_sample"], frozen) == start["mic_sample"], (start, stop, frozen)
    assert stop["reply_sample"] == 12000


NO_LATENCY_APIS = """
AudioContext.prototype.getOutputTimestamp = () => ({ contextTime: 0, performanceTime: 0 });
Object.defineProperty(AudioContext.prototype, 'outputLatency', { get: () => undefined, configurable: true });
MediaStreamTrack.prototype.getSettings = () => ({});
"""


@pytest.mark.parametrize("stack", [NO_LATENCY_APIS], indirect=True)
async def test_playback_frames_carry_mic_sample_without_latency_apis(stack):
    """A browser without an output timestamp, output latency or a track latency still gets
    integer mic_sample values that place the edges on the mic stream."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    assert await page.evaluate("new AudioContext().getOutputTimestamp().performanceTime") == 0
    assert await page.evaluate("new AudioContext().outputLatency === undefined")
    rid = await _reply_request(conn, page, sessions, utterances, "hello")
    for _ in range(5):
        await conn.send(b"\x10\x00" * 2400)  # 5 x 0.1 s at 24 kHz
    start, sent_at_start = await _start_frame(conn)
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=10)
    start, stop = playback[:2]
    assert (start["event"], stop["event"]) == ("start", "stop")
    assert _is_count(start["mic_sample"]) and _is_count(stop["mic_sample"])
    assert abs(start["mic_sample"] - sent_at_start) <= 8000, (start["mic_sample"], sent_at_start)
    assert stop["reply_sample"] == 12000
    assert stop["mic_sample"] >= start["mic_sample"]
    assert abs((stop["mic_sample"] - start["mic_sample"]) - 0.5 * 16000) <= 8000, (start, stop)


async def test_the_page_shows_working_while_the_turn_runs_between_spoken_parts(stack):
    """A reply that speaks, then goes quiet while Hermes runs tools, shows Working, not
    Speaking or Listening, until the turn is over."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    state = "document.getElementById('state').textContent"
    await page.wait_for_function(f"{state} === 'Listening'", timeout=10000)
    await conn.send({"type": "turn_end", "text": "check the weather", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await page.wait_for_function(f"{state} === 'Thinking'", timeout=5000)
    await session.on_reply_delta(reply_id, "t1", 1, "Let me check. ")
    rid = (await wait_for(lambda: conn.json_frames("tts_stream")))[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x10\x00" * 4800)  # 0.2 s
    await page.wait_for_function(f"{state} === 'Speaking'", timeout=5000)
    await page.wait_for_function(f"{state} === 'Working'", timeout=5000)  # the audio ran out
    await asyncio.sleep(0.5)
    assert await page.text_content("#state") == "Working"
    await session.on_reply_delta(reply_id, "t1", 2, "It is sunny.")
    await session.on_turn_done(reply_id, "t1", False)
    await wait_for(lambda: any(f.get("done") for f in conn.json_frames("tts_stream")))
    await conn.send(b"\x10\x00" * 4800)
    await page.wait_for_function(f"{state} === 'Speaking'", timeout=5000)
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    await page.wait_for_function(f"{state} === 'Listening'", timeout=5000)

async def test_gateway_messages_never_replace_the_streamed_reply(stack):
    """Tool progress and other sent messages show as their own lines; the final send
    of text already streamed adds nothing."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    await conn.send({"type": "turn_end", "text": "weather please", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.on_reply_delta(reply_id, "t1", 1, "Let me check.")
    await session.send_page({"type": "reply", "text": "web_search: weather today"})
    await session.on_reply_delta(reply_id, "t1", 2, "It is sunny.")
    await session.send_page({"type": "reply", "text": "It is sunny."})
    await page.wait_for_function("document.querySelectorAll('#log .turn.hermes').length >= 3", timeout=5000)
    await asyncio.sleep(0.2)
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert texts == ["Let me check.", "web_search: weather today", "It is sunny."]


async def test_a_replaced_page_stops_reconnecting_until_used_here(stack):
    fake, page, sessions, utterances = stack
    await fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    import websockets
    port = page.url.split(":")[2].split("/")[0]
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={TOKEN}&client=phone") as other:
        await page.wait_for_function(
            "document.getElementById('state').textContent === 'In use on another device'", timeout=5000)
        await fake.wait_connections(2, timeout=5)  # the page's own session, then the new client's
        await asyncio.sleep(2.0)
        assert len(fake.connections) == 2  # the replaced page did not come back by itself
        assert await page.text_content("#toggle") == "Use here"
        await page.click("#toggle")
        await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                     timeout=10000)
        frames = []
        try:
            async with asyncio.timeout(5):
                async for message in other:
                    if isinstance(message, str):
                        frames.append(message)
        except websockets.exceptions.ConnectionClosed as closed:
            assert closed.rcvd.code == 4001
        assert any('"replaced"' in f for f in frames)
    assert await page.text_content("#toggle") == "Mute microphone"


async def _listening(fake, page):
    (conn,) = await fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    return conn


async def test_the_activity_panel_shows_each_tool_call(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    assert await page.is_hidden("#activity")  # nothing to show yet
    await session.send_page({"type": "tool_start", "id": "c1", "name": "terminal", "summary": "ls -la"})
    await page.wait_for_selector("#tools .tool.running")
    line = await page.text_content("#tools .tool")
    assert "terminal" in line and "ls -la" in line
    await session.send_page({"type": "tool_end", "id": "c1", "status": "ok", "duration_ms": 1234})
    await page.wait_for_selector("#tools .tool.done")
    assert "1.2 s" in await page.text_content("#tools .tool.done")
    await session.send_page({"type": "tool_start", "id": "c2", "name": "web_search", "summary": "weather"})
    await session.send_page({"type": "tool_end", "id": "c2", "status": "error", "duration_ms": 50})
    await page.wait_for_selector("#tools .tool.failed")
    for i in range(10):
        await session.send_page({"type": "tool_start", "id": f"m{i}", "name": "read_file", "summary": f"f{i}"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 12")
    await asyncio.sleep(0.2)
    box = await page.evaluate("""() => {
        const t = document.getElementById('tools'), li = t.querySelector('.tool');
        const last = t.lastElementChild.getBoundingClientRect(), r = t.getBoundingClientRect();
        return { h: t.clientHeight, line: li.getBoundingClientRect().height, scroll: t.scrollHeight,
                 lastInView: last.bottom <= r.bottom + 1 && last.top >= r.top - 1 };
    }""")
    assert box["h"] <= 6.5 * box["line"] and box["scroll"] > box["h"]
    assert box["lastInView"]
    await page.click("#fold")
    assert await page.is_hidden("#tools")
    await page.click("#fold")
    assert await page.is_visible("#tools")
    log = await page.text_content("#log")
    assert "terminal" not in log and "web_search" not in log


async def test_a_finished_turn_folds_its_tool_lines_into_a_summary_line(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "check two things"})
    for call in ("c1", "c2"):
        await session.send_page({"type": "tool_start", "id": call, "name": "terminal", "summary": call})
        await session.send_page({"type": "tool_end", "id": call, "status": "ok", "duration_ms": 10})
    await session.send_page({"type": "turn_done"})
    await page.wait_for_selector("#tools .group")
    summary = await page.text_content("#tools .group")
    assert "2 tools" in summary and re.search(r"\b\d+:\d\d\b", summary)
    assert await page.evaluate("[...document.querySelectorAll('#tools .tool')].every(li => li.hidden)")
    await page.click("#tools .group button")
    assert await page.evaluate("[...document.querySelectorAll('#tools .tool')].every(li => !li.hidden)")
    await session.send_page({"type": "utterance", "text": "and one more"})
    await session.send_page({"type": "tool_start", "id": "c3", "name": "read_file", "summary": "c3"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 3")
    order = await page.eval_on_selector_all("#tools > li", "els => els.map(e => e.className)")
    assert order[0].startswith("group") and order[-1] == "tool running"


async def test_working_and_thinking_show_the_elapsed_time_and_tool_count(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    assert await page.is_hidden("#elapsed")
    await session.send_page({"type": "utterance", "text": "run the tests"})
    for call in ("c1", "c2"):
        await session.send_page({"type": "tool_start", "id": call, "name": "terminal", "summary": call})
    await page.wait_for_function("(document.getElementById('elapsed').textContent || '').includes('2 tools')")
    first = await page.text_content("#elapsed")
    await asyncio.sleep(2.2)  # the label ticks twice a second: more than a whole second passes
    second = await page.text_content("#elapsed")

    def secs(text):
        m, s = re.search(r"(\d+):(\d\d)", text).groups()
        return int(m) * 60 + int(s)

    assert secs(second) > secs(first)
    assert await page.text_content("#state") == "Thinking"
    await session.send_page({"type": "turn_done"})
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'")
    assert await page.is_hidden("#elapsed")


async def test_the_reasoning_line_folds_and_opens(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "why is the sky blue"})
    assert await page.is_hidden("#reasoning")
    for text in ("The user asks about the sky. ", "Rayleigh scattering explains it. ", "Keep it short"):
        await session.send_page({"type": "reasoning_delta", "text": text})
    await page.wait_for_function("document.getElementById('reason-line').textContent === 'Keep it short'")
    assert await page.is_visible("#reasoning") and await page.is_hidden("#reason-full")
    await page.click("#reason-fold")
    full = await page.text_content("#reason-full")
    assert await page.is_visible("#reason-full")
    assert full == "The user asks about the sky. Rayleigh scattering explains it. Keep it short"
    await page.click("#reason-fold")
    assert await page.is_hidden("#reason-full")
    await session.send_page({"type": "turn_done"})
    await page.wait_for_function("document.getElementById('reason-line').textContent.startsWith('Reasoned for ')")
    assert re.fullmatch(r"Reasoned for \d+:\d\d", await page.text_content("#reason-line"))
    await session.send_page({"type": "utterance", "text": "thanks"})
    await page.wait_for_function("document.getElementById('reasoning').hidden")


async def test_the_reasoning_line_shows_the_newest_line_without_punctuation(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "plan the release"})
    for text in ("Steps\n- bump the version\n", "- tag it\n", "- push the mirror"):
        await session.send_page({"type": "reasoning_delta", "text": text})
    await page.wait_for_function("document.getElementById('reason-line').textContent === '- push the mirror'")


async def _fills_width(page):
    return await page.evaluate("""() => {
        const b = document.getElementById('toggle').getBoundingClientRect();
        return { scroll: document.documentElement.scrollWidth, width: window.innerWidth,
                 button: b.left >= 0 && b.right <= window.innerWidth };
    }""")


async def test_long_lines_keep_the_page_at_the_screen_width(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "think hard"})
    await session.send_page({"type": "reasoning_delta", "text": "word " * 60})
    await session.send_page({"type": "tool_start", "id": "c1", "name": "terminal",
                             "summary": "x" * 40})
    await page.wait_for_selector("#tools .tool")
    for width, height in ((390, 760), (1200, 800)):
        await page.set_viewport_size({"width": width, "height": height})
        await asyncio.sleep(0.2)
        box = await _fills_width(page)
        assert box["scroll"] <= box["width"], (width, box)
        assert box["button"], (width, box)


async def test_a_full_activity_area_leaves_room_for_the_log(stack):
    fake, page, sessions, utterances = stack
    await page.set_viewport_size({"width": 390, "height": 664})
    await _listening(fake, page)
    session = sessions[0]
    bare = await page.evaluate("document.querySelector('header').getBoundingClientRect().height")
    await session.send_page({"type": "utterance", "text": "do a lot"})
    await session.send_page({"type": "reasoning_delta", "text": "A long thought. " * 40})
    for i in range(12):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 12")
    await page.click("#reason-fold")
    await asyncio.sleep(0.2)
    box = await page.evaluate("""() => {
        const m = document.querySelector('main').getBoundingClientRect();
        const b = document.getElementById('toggle').getBoundingClientRect();
        return { log: m.height, button: b.top >= 0 && b.bottom <= window.innerHeight };
    }""")
    assert box["log"] >= 120, box
    assert box["button"], box
    await page.click("#reason-fold")
    await page.click("#fold")
    await asyncio.sleep(0.2)
    folded = await page.evaluate("document.querySelector('header').getBoundingClientRect().height")
    assert folded <= bare + 120, (bare, folded)  # the time line, the reasoning line and the fold control


async def test_each_tool_status_shows_its_own_words(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    words = {"error": "failed", "timeout": "timed out", "blocked": "blocked", "cancelled": "cancelled"}
    for i, status in enumerate(words):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": status})
        await session.send_page({"type": "tool_end", "id": f"c{i}", "status": status, "duration_ms": 100})
    await page.wait_for_function("document.querySelectorAll('#tools .tool.failed').length === 4")
    texts = await page.eval_on_selector_all("#tools .tool .status", "els => els.map(e => e.textContent)")
    assert [t.split(" 0.1 s")[0] for t in texts] == list(words.values())


async def _turn_in_flight(session, page):
    await session.send_page({"type": "utterance", "text": "long job"})
    await session.send_page({"type": "reasoning_delta", "text": "Working on it. Still going"})
    await session.send_page({"type": "tool_start", "id": "c1", "name": "terminal", "summary": "make"})
    await page.wait_for_selector("#tools .tool.running")


async def _turn_was_ended(page):
    await page.wait_for_selector("#tools .group")
    assert "stopped" in await page.text_content("#tools .tool")
    assert await page.locator("#tools .tool.running").count() == 0
    assert (await page.text_content("#reason-line")).startswith("Reasoned for ")
    assert await page.is_hidden("#elapsed")


async def test_a_reconnect_ends_the_turn_on_the_page(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    await _turn_in_flight(sessions[0], page)
    await sessions[0].page.close()
    await wait_for(lambda: len(sessions) == 2, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    await _turn_was_ended(page)


async def test_a_replaced_page_ends_its_turn_on_reconnect(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    await _turn_in_flight(sessions[0], page)
    import websockets
    port = page.url.split(":")[2].split("/")[0]
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={TOKEN}&client=phone"):
        await page.wait_for_function(
            "document.getElementById('state').textContent === 'In use on another device'", timeout=5000)
        await _turn_was_ended(page)


async def _newest_tool_in_view(page):
    return await page.evaluate("""() => {
        const li = [...document.querySelectorAll('#tools .tool')].pop().getBoundingClientRect();
        const h = document.querySelector('header').getBoundingClientRect();
        return li.height > 0 && li.top >= h.top - 1 && li.bottom <= h.bottom + 1
            && li.top >= 0 && li.bottom <= window.innerHeight;
    }""")


async def test_the_newest_tool_line_stays_in_view_on_a_short_screen(stack):
    fake, page, sessions, utterances = stack
    await page.set_viewport_size({"width": 390, "height": 664})
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "busy"})
    for i in range(6):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 6")
    await asyncio.sleep(0.2)
    assert await _newest_tool_in_view(page)
    await session.send_page({"type": "reasoning_delta", "text": "A long thought. " * 40})
    await page.wait_for_selector("#reasoning:not([hidden])")
    await page.click("#reason-fold")
    await session.send_page({"type": "tool_start", "id": "c6", "name": "terminal", "summary": "step 6"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 7")
    await asyncio.sleep(0.2)
    assert await _newest_tool_in_view(page)
    box = await page.evaluate("""() => ({ log: document.querySelector('main').getBoundingClientRect().height,
        button: document.getElementById('toggle').getBoundingClientRect().bottom <= window.innerHeight })""")
    assert box["log"] >= 120 and box["button"], box


async def test_the_newest_tool_line_stays_in_view_when_the_list_shrinks(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    for width, height in ((390, 664), (360, 640)):
        await page.set_viewport_size({"width": width, "height": height})
        await session.send_page({"type": "utterance", "text": f"busy at {width}"})
        for i in range(6):
            await session.send_page({"type": "tool_start", "id": f"{width}-{i}", "name": "terminal",
                                     "summary": f"step {i}"})
        await page.wait_for_function(
            "([...document.querySelectorAll('#tools .tool')].pop() || {textContent: ''}).textContent"
            ".includes('step 5')")
        await session.send_page({"type": "reasoning_delta", "text": "A long thought. " * 40})
        await page.wait_for_selector("#reasoning:not([hidden])")
        await asyncio.sleep(0.2)
        assert await _newest_tool_in_view(page), (width, "line")
        await page.click("#reason-fold")
        await asyncio.sleep(0.2)
        assert await _newest_tool_in_view(page), (width, "text open")
        await page.click("#reason-fold")
        await session.send_page({"type": "turn_done"})
    # A user who scrolled the list up to read is not pulled back down.
    await page.click("#tools .group button")
    await page.hover("#tools")
    await page.mouse.wheel(0, -2000)
    await page.wait_for_function("document.getElementById('tools').scrollTop === 0")
    await page.set_viewport_size({"width": 390, "height": 600})
    await asyncio.sleep(0.2)
    assert await page.evaluate("document.getElementById('tools').scrollTop") == 0


async def test_reasoning_and_tools_never_overlap_on_a_landscape_phone(stack):
    fake, page, sessions, utterances = stack
    await page.set_viewport_size({"width": 740, "height": 360})
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "sideways"})
    await session.send_page({"type": "reasoning_delta", "text": "A long thought. " * 40})
    for i in range(3):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 3")
    await page.click("#reason-fold")
    await asyncio.sleep(0.3)
    boxes = await page.evaluate("""() => {
        const r = (id) => { const b = document.getElementById(id).getBoundingClientRect(); return [b.top, b.bottom]; };
        const h = document.querySelector('header');
        return { line: r('reason-fold'), text: r('reason-full'), fold: r('fold'), tools: r('tools'),
                 scrolls: h.scrollHeight > h.clientHeight };
    }""")
    order = [boxes["line"], boxes["text"], boxes["fold"], boxes["tools"]]
    for upper, lower in zip(order, order[1:]):
        assert upper[1] <= lower[0] + 1, boxes  # each part ends before the next begins
    assert all(b[1] - b[0] > 10 for b in order), boxes  # none squeezed to nothing
    assert boxes["scrolls"], boxes


async def test_a_new_tool_line_never_scrolls_the_window(stack):
    fake, page, sessions, utterances = stack
    await page.set_viewport_size({"width": 900, "height": 560})
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "wide"})
    await session.send_page({"type": "reasoning_delta", "text": "A long thought. " * 60})
    await page.wait_for_selector("#reasoning:not([hidden])")
    await page.click("#reason-fold")
    await page.evaluate("window.scrollTo(0, 0)")
    for i in range(4):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 4")
    await asyncio.sleep(0.3)
    assert await page.evaluate("window.scrollY") == 0


async def test_the_header_stays_where_the_user_scrolled_it(stack):
    fake, page, sessions, utterances = stack
    await page.set_viewport_size({"width": 740, "height": 360})
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "sideways"})
    for i in range(4):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await session.send_page({"type": "reasoning_delta", "text": "A long thought. " * 20})
    await page.wait_for_selector("#reasoning:not([hidden])")
    await asyncio.sleep(0.2)
    await page.evaluate("document.querySelector('header').scrollTop = 0")
    await session.send_page({"type": "reasoning_delta", "text": "More thought. "})
    await page.evaluate("document.getElementById('reason-fold').click()")  # no scroll into view
    await session.send_page({"type": "tool_start", "id": "c9", "name": "terminal", "summary": "step 9"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 5")
    await asyncio.sleep(0.3)
    assert await page.evaluate("document.querySelector('header').scrollTop") == 0
    assert await page.evaluate("window.scrollY") == 0


async def test_folded_tool_lines_take_no_room_and_open_at_their_summary(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "many steps"})
    for i in range(10):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
        await session.send_page({"type": "tool_end", "id": f"c{i}", "status": "ok", "duration_ms": 5})
    await session.send_page({"type": "turn_done"})
    await page.wait_for_selector("#tools .group")
    await asyncio.sleep(0.2)
    drawn = "[...document.querySelectorAll('#tools .tool')].map(li => li.getBoundingClientRect().height)"
    assert all(h == 0 for h in await page.evaluate(drawn))
    await page.evaluate("document.querySelector('#tools .group button').click()")
    await asyncio.sleep(0.3)
    assert all(h > 0 for h in await page.evaluate(drawn))
    view = await page.evaluate("""() => {
        const t = document.getElementById('tools').getBoundingClientRect();
        const b = document.querySelector('#tools .group button').getBoundingClientRect();
        const list = document.getElementById('tools');
        return { inView: b.top >= t.top - 1 && b.bottom <= t.bottom + 1,
                 atEnd: list.scrollTop + list.clientHeight >= list.scrollHeight - 4 };
    }""")
    assert view["inView"] and not view["atEnd"], view
    await session.send_page({"type": "tool_start", "id": "n1", "name": "read_file", "summary": "next"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 11")
    await asyncio.sleep(0.2)
    assert await _newest_tool_in_view(page)
