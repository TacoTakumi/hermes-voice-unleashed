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
    """What GET /sessions answers: a titled session (the one a UI client has voice attached
    to), one with a preview only (the one the lane is on) and one with neither."""
    return [
        {"id": "sid-titled-111", "title": "Garden plans", "preview": "what should I plant",
         "last_active": now - 300, "message_count": 12, "source": "telegram", "current": False,
         "voice_attached": True},
        {"id": "sid-untitled-222", "title": None, "preview": "remind me about the dentist",
         "last_active": now - 7200, "message_count": 1, "source": "voice_unleashed", "current": True,
         "voice_attached": False},
        {"id": "sid-bare-333", "title": None, "preview": "", "last_active": now - 3 * 86400,
         "message_count": 0, "source": "cli", "current": False, "voice_attached": False},
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
    (``list_requests``); the page records the URL of every HTTP request it makes
    (``http_requests``). The frames in ``before_ready`` go to the page on every connect
    before the session starts, as the plugin sends its connect frames. With
    ``join_failed_message`` set, a connect gets a join_failed frame with that message and
    the join-failed close instead of a session. A session whose page connection is gone
    lands in ``ended``."""
    plugin = sys.modules["voice_unleashed"]
    from voice_unleashed.server import JOIN_FAILED_CLOSE_CODE, VoiceServer
    from voice_unleashed.session import VoiceSession, client_from_url

    async with FakeVoiceManager() as fake:
        site = SimpleNamespace(fake=fake, sessions=[], ended=[], utterances=[], ws_requests=[], list_requests=[],
                               http_requests=[], rows=_session_rows(int(time.time())), before_ready=[],
                               join_failed_message=None)

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
        site.server = server  # a test may stop it early; a second stop is a no-op
        wav = tmp_path / "tone.wav"
        _tone_wav(wav)
        async with playwright_api.async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=os.environ.get("CHROME") or None, headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                      f"--use-file-for-fake-audio-capture={wav}", "--autoplay-policy=no-user-gesture-required"])
            site.page = await browser.new_page(viewport={"width": 390, "height": 760})
            site.page.on("request", lambda request: site.http_requests.append(request.url))
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
        await site.page.goto(f"{site.url}/?token={TOKEN}")
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


def _list_queries(site):
    """The query of every session list request the page made."""
    return [parse_qs(urlsplit(url).query) for url in site.http_requests if urlsplit(url).path == "/sessions"]


async def test_the_chooser_shows_new_first_then_the_rows_as_received(site):
    """The page has one voice: the session list is asked for without a client lane, no
    row is marked current, and the row a UI client has voice attached to says so."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    texts = await _chooser_rows(page)
    assert await page.is_visible("#chooser") and await page.is_hidden("#toggle")
    assert site.list_requests == ["voice"]  # the server's default lane: the page named none
    (query,) = _list_queries(site)
    assert query["token"] == [TOKEN] and "client" not in query
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
    assert "2 h ago" in preview and "1 line" in preview and "1 lines" not in preview and "voice_unleashed" in preview
    assert "3 days ago" in bare and "0 lines" in bare and "cli" in bare
    assert await page.locator("#sessions [aria-current]").count() == 0
    assert "Current" not in await page.text_content("#chooser")
    voice = await page.eval_on_selector_all("#sessions .session", "els => els.map(e => e.querySelector('.voice')?.textContent ?? null)")
    assert voice == ["Voice on Desktop", None, None]
    assert "Voice on Desktop" in titled and "Voice on Desktop" not in preview and "Voice on Desktop" not in bare
    for text in texts:
        for row in site.rows:
            assert row["id"] not in text, (row["id"], text)


async def test_tapping_a_chooser_row_connects_with_that_session_and_no_client(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await _chooser_rows(page)
    await page.click("#sessions .session:nth-child(2) button")
    await site.fake.wait_connections(1, timeout=10)
    assert site.ws_requests[0]["session"] == ["sid-untitled-222"]
    assert "client" not in site.ws_requests[0]
    assert await page.is_hidden("#chooser") and await page.is_visible("#toggle")
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)


async def test_tapping_new_in_the_chooser_connects_with_session_new(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await _chooser_rows(page)
    await page.click("#new")
    await site.fake.wait_connections(1, timeout=10)
    assert site.ws_requests[0]["session"] == ["new"]
    assert "client" not in site.ws_requests[0]
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)


async def test_a_client_in_the_page_url_is_not_sent_on_from_the_chooser(site):
    """The page has one voice per profile: a leftover client parameter in its URL is
    not read and reaches neither the session list nor the connection."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&client=tablet")
    await _chooser_rows(page)
    assert site.list_requests == ["voice"]
    (query,) = _list_queries(site)
    assert "client" not in query
    await page.click("#new")
    await site.fake.wait_connections(1, timeout=10)
    assert "client" not in site.ws_requests[0]


async def test_a_reconnect_after_the_chooser_does_not_ask_for_the_session_again(site):
    """The choice is sent once. A reconnect stays on the session the lane is on: asking
    for a new session again would open another one each time."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await _chooser_rows(page)
    await page.click("#new")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)
    await site.sessions[0].page.close()
    await wait_for(lambda: len(site.ws_requests) == 2, timeout=10)
    assert site.ws_requests[0]["session"] == ["new"]
    assert "session" not in site.ws_requests[1]
    assert "client" not in site.ws_requests[1]


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
    await page.goto(f"{site.url}/?token={TOKEN}")
    await _chooser_rows(page)
    await page.evaluate(TURNS_AT_LISTENING)
    await page.click("#sessions .session:nth-child(1) button")
    await site.fake.wait_connections(1, timeout=10)
    assert site.ws_requests[0]["session"] == ["sid-titled-111"]
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
# Whether each mic track still captures: a track switched off is silent but not released.
MIC_TRACKS_ON = "window.micStreams.flatMap(s => s.getAudioTracks().map(t => t.enabled))"

# With the mic streams and wake locks: how many audio contexts the page closed so far.
# The page keeps one for the mic and one for the replies.
MIC_WAKE_LOCK_AND_CONTEXTS = MIC_AND_WAKE_LOCK + """
window.contextCloses = 0;
const realClose = AudioContext.prototype.close;
AudioContext.prototype.close = function () { window.contextCloses += 1; return realClose.call(this); };
"""


def _url_session(url):
    """The session the page URL names, or None."""
    return parse_qs(urlsplit(url).query).get("session", [None])[0]


def _page_loads(site):
    """How many times the page itself was fetched: one per load or reload."""
    return sum(1 for url in site.http_requests if urlsplit(url).path == "/")


LISTENING = "document.getElementById('state').textContent === 'Listening'"


async def test_tapping_a_row_puts_the_session_in_the_url_without_a_navigation(site):
    """The session is the page's identity: a join writes session=<id> into the URL as a
    history entry, in place, so the page is not loaded again and the token stays."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await _chooser_rows(page)
    assert _url_session(page.url) is None
    await page.evaluate("window.samePage = true")
    await page.click("#sessions .session:nth-child(2) button")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function(LISTENING, timeout=10000)
    assert _url_session(page.url) == "sid-untitled-222"
    assert parse_qs(urlsplit(page.url).query)["token"] == [TOKEN]
    assert await page.evaluate("window.samePage") is True  # no load happened
    assert _page_loads(site) == 1
    assert len(site.ws_requests) == 1 and site.ws_requests[0]["session"] == ["sid-untitled-222"]
    assert await page.evaluate("history.length") >= 2  # Back has somewhere to go


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_back_returns_to_the_chooser_with_the_mic_off_and_no_session_in_the_url(site):
    """The chooser is the history entry before the join: Back leaves the session, lets
    go of the mic and the wake lock, shows the sessions again and does not reconnect."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await _chooser_rows(page)
    await page.click("#sessions .session:nth-child(2) button")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function(LISTENING, timeout=10000)
    await page.evaluate(STATES_SEEN)
    await page.go_back()
    await page.wait_for_selector("#chooser:not([hidden])", timeout=5000)
    await wait_for(lambda: len(site.ended) == 1, timeout=10)
    await wait_for(lambda: len(site.list_requests) == 2, timeout=5)
    await asyncio.sleep(1.0)  # long enough for a first reconnect try
    assert _url_session(page.url) is None
    assert parse_qs(urlsplit(page.url).query)["token"] == [TOKEN]
    assert _page_loads(site) == 1  # Back moved within the page
    assert await page.evaluate(MIC_TRACK_STATES) == ["ended"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 1}
    assert await page.text_content("#state") == "Sessions"
    assert await page.is_visible("#chooser") and await page.is_hidden("#toggle")
    assert len(site.ws_requests) == 1  # no reconnect
    states = await page.evaluate("window.statesSeen")
    assert "Reconnecting" not in states, states
    await _chooser_rows(page)


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_load_with_a_session_in_the_url_rejoins_it_without_a_tap(site):
    """A reload must reload the session: a page opened with session=<id> connects with
    that id and opens the mic by itself. No chooser, no tap, no unmute button when the
    browser lets playback run."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&session=sid-titled-111")
    await site.fake.wait_connections(1, timeout=10)
    assert site.ws_requests[0]["session"] == ["sid-titled-111"]
    assert "client" not in site.ws_requests[0]
    await page.wait_for_function(LISTENING, timeout=10000)
    assert await page.is_hidden("#chooser") and await page.is_visible("#toggle")
    assert site.list_requests == []
    assert await page.evaluate(MIC_TRACK_STATES) == ["live"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 0}
    assert await page.text_content("#toggle") == "Mute microphone"
    assert await page.is_hidden("#unmute")
    assert _url_session(page.url) == "sid-titled-111"


# Playback stays locked: the context reports suspended until its second resume, as a
# browser that waits for a tap does. The real context runs (the harness allows autoplay),
# so the page's audio path is not touched; only what the page reads is. resumeCalls
# counts the resumes of the reply context, the first one the page makes; the mic
# context gets its own resume, which is not the unmute button's business.
PLAYBACK_LOCKED = MIC_AND_WAKE_LOCK + """
window.resumeCalls = 0;
window.lockedContexts = [];
const RealContext = window.AudioContext;
window.AudioContext = class extends RealContext {
  constructor(...args) { super(...args); window.lockedContexts.push(this); }
};
const realResume = RealContext.prototype.resume;
RealContext.prototype.resume = function () {
  if (this === window.lockedContexts[0]) window.resumeCalls += 1;
  this.resumes = (this.resumes || 0) + 1;
  return realResume.call(this);
};
Object.defineProperty(AudioContext.prototype, 'state', { configurable: true,
  get() { return (this.resumes || 0) >= 2 ? 'running' : 'suspended'; } });
"""


@pytest.mark.parametrize("site", [PLAYBACK_LOCKED], indirect=True)
async def test_a_rejoin_with_playback_locked_shows_one_unmute_button_that_resumes_it(site):
    """Without a tap the browser may keep the reply context suspended. The page joins
    all the same, with the mic open, and shows one Tap to unmute button; the tap resumes
    the context and the button goes."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}&session=sid-titled-111")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function(LISTENING, timeout=10000)
    await page.wait_for_selector("#unmute:not([hidden])", timeout=5000)
    assert await page.text_content("#unmute") == "Tap to unmute"
    assert await page.locator("button:visible", has_text="unmute").count() == 1
    assert await page.evaluate("window.resumeCalls") == 1
    assert await page.evaluate(MIC_TRACK_STATES) == ["live"]
    assert await page.text_content("#state") == "Listening"
    await page.click("#unmute")
    await page.wait_for_selector("#unmute", state="hidden", timeout=5000)
    assert await page.evaluate("window.resumeCalls") == 2
    assert await page.text_content("#state") == "Listening"
    assert len(site.ws_requests) == 1


# The mic streams and the audio contexts the page made, kept where a test can read them.
STREAMS_AND_CONTEXTS = """
window.__streams = [];
const realGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
navigator.mediaDevices.getUserMedia = async (constraints) => {
  const stream = await realGetUserMedia(constraints);
  window.__streams.push(stream);
  return stream;
};
window.__contexts = [];
const RealAudioContext = window.AudioContext;
window.AudioContext = class extends RealAudioContext {
  constructor(...args) { super(...args); window.__contexts.push(this); }
};
"""

# With the streams and contexts: the mic worklet does not load, so a start fails after
# the mic and its context are open.
WORKLET_FAILS = STREAMS_AND_CONTEXTS + """
AudioWorklet.prototype.addModule = function () { return Promise.reject(new Error('no worklet')); };
"""

# With the streams and contexts: the contexts that were resumed at least once.
RESUMES_COUNTED = STREAMS_AND_CONTEXTS + """
window.__resumed = [];
const realResume = AudioContext.prototype.resume;
AudioContext.prototype.resume = function () {
  if (!window.__resumed.includes(this)) window.__resumed.push(this);
  return realResume.call(this);
};
"""

STREAM_TRACK_STATES = "window.__streams.flatMap(s => s.getTracks().map(t => t.readyState))"


@pytest.mark.parametrize("site", [WORKLET_FAILS], indirect=True)
async def test_a_failed_start_lets_go_of_the_mic_and_closes_both_contexts(site):
    """A start that fails after the mic opened must not leave the mic on: the page shows
    No microphone, stops the mic tracks and closes the capture context as well as the
    reply context."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await page.click("#new")
    await page.wait_for_function("document.getElementById('state').textContent === 'No microphone'", timeout=10000)
    assert await page.evaluate("window.__streams.length") >= 1
    assert await page.evaluate("window.__contexts.length") >= 2  # the reply context and the mic context
    await page.wait_for_function(f"{STREAM_TRACK_STATES}.every(s => s === 'ended')", timeout=2000)
    await page.wait_for_function("window.__contexts.every(c => c.state === 'closed')", timeout=2000)
    assert await page.is_enabled("#toggle")


@pytest.mark.parametrize("site", [RESUMES_COUNTED], indirect=True)
async def test_a_start_resumes_the_mic_context_as_well_as_the_reply_context(site):
    """A context made outside a tap may stay suspended, the mic one included: a start
    resumes both of them."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await page.click("#new")
    await page.wait_for_function(LISTENING, timeout=10000)
    assert await page.evaluate("window.__contexts.length") == 2
    assert await page.evaluate("new Set(window.__resumed).size") == 2


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_history_frame_puts_its_session_in_the_url_so_a_reload_rejoins_it(site):
    """After New the page learns the session's id from the history frame and writes it
    into the URL in place; a reload then rejoins that session, and Back from it still
    returns to the chooser."""
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await _chooser_rows(page)
    await page.click("#new")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function(LISTENING, timeout=10000)
    assert site.ws_requests[0]["session"] == ["new"]
    assert _url_session(page.url) is None  # the id is not known yet
    await site.sessions[0].send_page({"type": "history", "session_id": "fresh", "lines": []})
    await page.wait_for_function("new URLSearchParams(location.search).get('session') === 'fresh'", timeout=5000)
    assert _page_loads(site) == 1
    assert len(site.ws_requests) == 1
    await page.reload()
    await wait_for(lambda: len(site.ws_requests) == 2, timeout=10)
    assert site.ws_requests[1]["session"] == ["fresh"]
    await page.wait_for_function(LISTENING, timeout=10000)
    assert _page_loads(site) == 2
    assert await page.is_hidden("#chooser")
    assert _url_session(page.url) == "fresh"
    await page.go_back()
    await page.wait_for_selector("#chooser:not([hidden])", timeout=5000)
    assert _url_session(page.url) is None
    await asyncio.sleep(1.0)
    assert len(site.ws_requests) == 2  # no reconnect
    assert await page.evaluate(f"{MIC_TRACK_STATES}.every(s => s === 'ended')")


async def test_the_chooser_after_a_failed_join_is_at_the_bare_url_and_a_reload_shows_it_again(site):
    """When the page itself leaves to the chooser, the URL loses its session: a reload
    shows the chooser, not another try at the session that failed."""
    page = site.page
    site.join_failed_message = "could not join session sid-titled-111: no such session"
    await page.goto(f"{site.url}/?token={TOKEN}&session=sid-titled-111")
    await page.wait_for_selector("#chooser:not([hidden])", timeout=5000)
    await wait_for(lambda: len(site.ws_requests) == 1, timeout=10)
    assert _url_session(page.url) is None
    assert parse_qs(urlsplit(page.url).query)["token"] == [TOKEN]
    await page.reload()
    await page.wait_for_selector("#chooser:not([hidden])", timeout=5000)
    await _chooser_rows(page)
    await asyncio.sleep(0.5)
    assert len(site.ws_requests) == 1  # the reload did not try the session again
    assert len(site.list_requests) == 2


async def _listening_from_the_chooser(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await _chooser_rows(page)
    await page.click("#new")
    await site.fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=10000)
    assert site.list_requests == ["voice"]
    assert await page.evaluate(MIC_TRACK_STATES) == ["live"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 0}
    await page.evaluate(STATES_SEEN)


NOTICE_SECONDS = 1.5  # the spoken notice the server sends before its session_end frame


async def _notice_to_the_page(session, request_id, seconds=NOTICE_SECONDS):
    """The audio of a spoken notice, as the plugin forwards it: audio_start, the PCM in
    0.2 s chunks at 24 kHz, audio_end. The page queues it all for playback."""
    await session.send_page({"type": "audio_start", "request_id": request_id, "sample_rate": 24000})
    tone = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 300 * i / 24000)))
                    for i in range(int(seconds * 24000)))
    for start in range(0, len(tone), 9600):
        await session.page.send(tone[start:start + 9600])
    await session.send_page({"type": "audio_end", "request_id": request_id, "reason": "complete"})


@pytest.mark.parametrize("site", [MIC_WAKE_LOCK_AND_CONTEXTS], indirect=True)
async def test_a_session_end_frame_silences_the_mic_at_once_and_shows_the_chooser_once_the_notice_played(site):
    """The user ended the session by voice. The server speaks its notice, sends the
    session_end frame once the notice's audio has all gone out, then closes. The page
    silences the mic the moment the frame comes but does not release it yet (a release
    while the notice plays garbles its end on a phone), lets the queued notice play to
    its end, and only then releases the mic, closes its socket, lets go of the wake
    lock and offers the sessions again, freshly listed. The server's close does not
    make it reconnect."""
    page = site.page
    await _listening_from_the_chooser(site)
    session = site.sessions[0]
    await _notice_to_the_page(session, "notice-1")
    await page.wait_for_function("document.getElementById('state').textContent === 'Speaking'", timeout=5000)
    sent_at = time.monotonic()
    await session.send_page({"type": "session_end"})
    await session.page.close()  # as the server does right after the frame
    await page.wait_for_function(f"{MIC_TRACKS_ON}.every(on => !on)", timeout=2000)
    mic_off_at = time.monotonic()
    assert mic_off_at - sent_at < 1.0, mic_off_at - sent_at
    assert await page.evaluate(MIC_TRACK_STATES) == ["live"]  # silent, not released: the notice plays
    assert await page.is_hidden("#chooser")
    assert await page.text_content("#state") == "Session ended"
    assert await page.evaluate("window.contextCloses") == 0  # nothing of the audio changes yet
    await page.wait_for_selector("#chooser:not([hidden])", timeout=8000)
    chooser_at = time.monotonic()
    drained = chooser_at - sent_at
    assert NOTICE_SECONDS - 0.5 <= drained < 4.0, drained  # the notice played out; the cap did not hit
    assert await page.evaluate("window.contextCloses") == 2  # now the reply context too
    await wait_for(lambda: len(site.ended) == 1, timeout=10)
    await wait_for(lambda: len(site.list_requests) == 2, timeout=5)
    await asyncio.sleep(1.0)  # long enough for a first reconnect try
    assert await page.evaluate(MIC_TRACK_STATES) == ["ended"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 1}
    assert await page.is_visible("#chooser") and await page.is_hidden("#toggle")
    assert await page.text_content("#state") == "Sessions"
    assert site.list_requests == ["voice", "voice"]
    assert len(site.ws_requests) == 1  # no reconnect
    states = await page.evaluate("window.statesSeen")
    assert "Reconnecting" not in states and "Voice moved to another device" not in states, states
    assert "Listening" not in states[states.index("Session ended"):], states  # nothing undid the end
    await _chooser_rows(page)


END_DRAIN_CAP_SECONDS = 5  # the page gives the notice at most this long to play out


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_history_move_to_a_session_while_the_end_notice_plays_joins_it_and_stays(site):
    """The session ended by voice and its notice is still playing when Back or Forward
    moves the URL to another session. The page joins that session, and the end of the
    first session must not come back later to tear the new one down: well past the time
    the notice would have been given, the page is still Listening on the new session,
    with its mic on, the chooser hidden and the session in the URL."""
    page = site.page
    await _listening_from_the_chooser(site)
    session = site.sessions[0]
    await _notice_to_the_page(session, "notice-1", seconds=END_DRAIN_CAP_SECONDS + 1)
    await page.wait_for_function("document.getElementById('state').textContent === 'Speaking'", timeout=5000)
    sent_at = time.monotonic()
    await session.send_page({"type": "session_end"})
    await session.page.close()  # as the server does right after the frame
    await page.wait_for_function(f"{MIC_TRACKS_ON}.every(on => !on)", timeout=2000)
    assert await page.text_content("#state") == "Session ended"
    target = site.rows[0]["id"]
    await page.evaluate(
        """([id, token]) => {
          history.pushState({ session: id }, "", `?token=${encodeURIComponent(token)}&session=${id}`);
          dispatchEvent(new PopStateEvent("popstate", { state: { session: id } }));
        }""",
        [target, TOKEN])
    await wait_for(lambda: len(site.ws_requests) == 2, timeout=10)
    assert site.ws_requests[1]["session"] == [target]
    await page.wait_for_function(LISTENING, timeout=10000)
    await asyncio.sleep(max(0.0, sent_at + END_DRAIN_CAP_SECONDS + 1.5 - time.monotonic()))
    assert await page.text_content("#state") == "Listening"
    assert await page.is_hidden("#chooser") and await page.is_visible("#toggle")
    assert _url_session(page.url) == target
    assert await page.evaluate(MIC_TRACK_STATES) == ["ended", "live"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 2, "released": 1}
    assert len(site.ws_requests) == 2 and len(site.sessions) == 2
    states = await page.evaluate("window.statesSeen")
    assert "Sessions" not in states and "Reconnecting" not in states, states


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_session_new_frame_keeps_the_socket_and_starts_the_log_over(site):
    """The user asked for a new session by voice. The server moves the connection onto
    a fresh session itself and sends session_new, then that session's (empty) history.
    The page keeps its socket, the mic and the wake lock, shows the empty log and
    Listening, and goes on with the next utterance on the same connection."""
    page = site.page
    await _listening_from_the_chooser(site)
    session = site.sessions[0]
    await session.send_page({"type": "utterance", "text": "end of the old session"})
    await page.wait_for_function("document.getElementById('state').textContent === 'Thinking'", timeout=5000)
    assert await _log_turns(page) == [["turn user", "end of the old session"]]
    await session.send_page({"type": "session_new", "session_id": "fresh"})
    await session.send_page({"type": "history", "session_id": "fresh", "lines": []})
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'", timeout=5000)
    await page.wait_for_function("document.querySelectorAll('#log .turn').length === 0", timeout=5000)
    await asyncio.sleep(1.0)  # long enough for a reconnect, had the page tried one
    assert len(site.ws_requests) == 1 and len(site.sessions) == 1 and site.ended == []
    assert len(site.fake.connections) == 1
    assert await page.text_content("#state") == "Listening"
    assert await _log_turns(page) == []
    assert await page.text_content("#transcript") == ""
    assert await page.is_hidden("#elapsed")
    assert await page.evaluate(MIC_TRACK_STATES) == ["live"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 0}
    assert await page.text_content("#toggle") == "Mute microphone"
    assert await page.is_hidden("#chooser")
    assert await page.evaluate("!document.getElementById('log').hidden")  # empty, so not visible to Playwright
    await session.send_page({"type": "utterance", "text": "first words of the new one"})
    await page.wait_for_function("document.querySelectorAll('#log .turn').length === 1", timeout=5000)
    assert await _log_turns(page) == [["turn user", "first words of the new one"]]
    states = await page.evaluate("window.statesSeen")
    assert "Voice moved to another device" not in states and "Reconnecting" not in states, states
    assert "Connecting" not in states and "Sessions" not in states, states
    assert site.list_requests == ["voice"]


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_join_failed_frame_shows_the_chooser_with_the_message(site):
    """The session the page asked for could not be joined: the server says so and closes
    with its own code. The page, which joined the session in its URL by itself, shows
    the chooser with the message and does not reconnect."""
    page = site.page
    site.join_failed_message = "could not join session sid-titled-111: no such session"
    await page.goto(f"{site.url}/?token={TOKEN}&session=sid-titled-111")
    await page.evaluate(STATES_SEEN)
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
    assert "Reconnecting" not in states and "Voice moved to another device" not in states, states
    await _chooser_rows(page)


MOVED = "document.getElementById('state').textContent === 'Voice moved to another device'"


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_replaced_page_shows_voice_moved_and_its_sessions_button_returns_to_the_chooser(site):
    """A second page connects for the same profile and the server replaces the first: a
    replaced frame, then the replaced close. The first page lets go of the mic and the wake
    lock, shows that the voice moved, does not reconnect or offer to take the voice back,
    and its one button returns to the chooser."""
    page = site.page
    await _listening_from_the_chooser(site)
    import websockets
    async with websockets.connect(f"{site.url.replace('http', 'ws')}/ws?token={TOKEN}") as other:
        await page.wait_for_function(MOVED, timeout=5000)
        await site.fake.wait_connections(2, timeout=5)  # the page's own session, then the other page's
        await wait_for(lambda: len(site.ended) == 1, timeout=10)
        await asyncio.sleep(2.0)  # long enough for a first reconnect try
        assert len(site.ws_requests) == 2  # the page's connect and the other page's: no reconnect
        assert len(site.fake.connections) == 2
        assert await page.evaluate(MIC_TRACK_STATES) == ["ended"]
        assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 1}
        assert await page.is_visible("#toggle") and await page.is_hidden("#chooser")
        assert await page.text_content("#toggle") == "Sessions"
        assert "Use here" not in await page.text_content("body")
        detail = await page.text_content("#detail")
        assert "another device" in detail and "Sessions" in detail
        await page.click("#toggle")
        await page.wait_for_selector("#chooser:not([hidden])", timeout=5000)
        await wait_for(lambda: len(site.list_requests) == 2, timeout=5)
        assert await page.text_content("#state") == "Sessions"
        assert await page.is_hidden("#toggle")
        await _chooser_rows(page)
        await asyncio.sleep(0.5)
        assert len(site.ws_requests) == 2 and len(site.fake.connections) == 2
        assert await page.evaluate(MIC_TRACK_STATES) == ["ended"]
        assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 1}
        assert other.state.name == "OPEN"  # the other page keeps the voice
        states = await page.evaluate("window.statesSeen")
        assert "Reconnecting" not in states and "Listening" not in states, states


@pytest.mark.parametrize("site", [MIC_AND_WAKE_LOCK], indirect=True)
async def test_a_replaced_frame_alone_shows_voice_moved_and_closes_the_socket(site):
    """The replaced frame on its own, before the server's close: the page shuts its socket,
    the mic and the wake lock, and shows that the voice moved."""
    page = site.page
    await _listening_from_the_chooser(site)
    await site.sessions[0].send_page({"type": "replaced"})
    await page.wait_for_function(MOVED, timeout=5000)
    await wait_for(lambda: len(site.ended) == 1, timeout=10)
    await asyncio.sleep(1.5)  # long enough for a first reconnect try
    assert len(site.ws_requests) == 1
    assert await page.evaluate(MIC_TRACK_STATES) == ["ended"]
    assert await page.evaluate("window.wakeLocks") == {"requested": 1, "released": 1}
    assert await page.text_content("#toggle") == "Sessions"
    states = await page.evaluate("window.statesSeen")
    assert "Reconnecting" not in states, states


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


async def test_playback_frames_of_a_reply_with_a_gap_count_only_the_samples_played(stack):
    """A reply whose chunks arrive with a gap between them is cut part way through the
    second chunk: the played count is the first chunk plus the played part of the second,
    not the whole time since the first chunk started (that would count the silent gap)."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    rid = await _reply_request(conn, page, sessions, utterances, "hello")
    await conn.send(b"\x10\x00" * 4800)  # 0.2 s at 24 kHz
    start, _ = await _start_frame(conn)
    await asyncio.sleep(1.0)  # the first chunk played out, the page is quiet
    await conn.send(b"\x10\x00" * 48000)  # 2 s at 24 kHz
    await asyncio.sleep(0.3)
    await conn.send({"type": "barge", "request_id": rid, "onset_mic_sample": 0, "mic_sample": 0})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=10)
    start, stop = playback[:2]
    assert (start["event"], stop["event"]) == ("start", "stop")
    assert _is_count(start["mic_sample"]) and _is_count(stop["mic_sample"])
    assert 4800 < stop["reply_sample"] < 24000, stop


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


OUTPUT_DELAY_SECONDS = 0.6
# A device whose speaker is this far behind the audio context's clock, as a phone is.
SLOW_SPEAKER = MIC_WAKE_LOCK_AND_CONTEXTS + f"""
AudioContext.prototype.getOutputTimestamp = () => ({{ contextTime: 0, performanceTime: 0 }});
Object.defineProperty(AudioContext.prototype, 'outputLatency', {{ get: () => {OUTPUT_DELAY_SECONDS}, configurable: true }});
"""


@pytest.mark.parametrize("site", [SLOW_SPEAKER], indirect=True)
async def test_after_a_session_end_frame_the_page_stays_until_the_notice_has_left_a_slow_speaker(site):
    """The audio context's clock passes the end of the notice before the sound is out
    of the speaker. The page keeps the context until then, so the notice is not cut."""
    page = site.page
    await _listening_from_the_chooser(site)
    session = site.sessions[0]
    began_at = time.monotonic()
    await _notice_to_the_page(session, "notice-1")
    await session.send_page({"type": "session_end"})
    await session.page.close()
    await page.wait_for_selector("#chooser:not([hidden])", timeout=8000)
    stayed = time.monotonic() - began_at
    assert NOTICE_SECONDS + OUTPUT_DELAY_SECONDS <= stayed < 4.5, stayed
    assert await page.evaluate("window.contextCloses") == 2


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


COUNT_GET_SETTINGS = """
const getSettings = MediaStreamTrack.prototype.getSettings;
window.__getSettingsCalls = 0;
MediaStreamTrack.prototype.getSettings = function () {
  window.__getSettingsCalls += 1;
  return getSettings.call(this);
};
"""


@pytest.mark.parametrize("site", [COUNT_GET_SETTINGS], indirect=True)
async def test_the_capture_latency_is_read_once_per_mic_start(site):
    """The mic track's settings are read when the mic starts, not on every 20 ms frame."""
    await site.page.goto(f"{site.url}/?token={TOKEN}")
    await site.page.click("#new")
    await site.page.wait_for_function(LISTENING, timeout=10000)
    await site.page.wait_for_timeout(2000)
    calls = await site.page.evaluate("window.__getSettingsCalls")
    assert calls <= 1, calls


def _capture_latency_only(seconds):
    """Stub the output clock away and give the mic track a fixed capture latency, so the
    capture latency is the only latency the page sees."""
    return f"""
AudioContext.prototype.getOutputTimestamp = () => ({{ contextTime: 0, performanceTime: 0 }});
Object.defineProperty(AudioContext.prototype, 'outputLatency', {{ get: () => undefined, configurable: true }});
MediaStreamTrack.prototype.getSettings = () => ({{ latency: {seconds} }});
"""


async def _start_offset_with_capture_latency(tmp_path, seconds):
    """Start a reply on a page whose mic track reports ``seconds`` of capture latency, and
    return the start edge's mic_sample minus the mic samples sent when the frame came."""
    async with _site(tmp_path, _capture_latency_only(seconds)) as site:
        await site.page.goto(f"{site.url}/?token={TOKEN}")
        await site.page.click("#new")
        (conn,) = await site.fake.wait_connections(1, timeout=10)
        rid = await _reply_request(conn, site.page, site.sessions, site.utterances, "hello")
        for _ in range(5):
            await conn.send(b"\x10\x00" * 2400)  # 5 x 0.1 s at 24 kHz
        start, sent_at_start = await _start_frame(conn)
        assert start["event"] == "start"
        assert _is_count(start["mic_sample"])
        return start["mic_sample"] - sent_at_start


async def test_a_larger_capture_latency_moves_the_start_edge_later_on_the_mic_stream(tmp_path):
    """A mic frame reaches the page its capture latency after the sound reached the mic,
    so a speaker time maps onto a mic sample that many samples later: 0.3 s more capture
    latency moves the start edge 4800 mic samples up."""
    offset_0 = await _start_offset_with_capture_latency(tmp_path, 0)
    offset_300 = await _start_offset_with_capture_latency(tmp_path, 0.3)
    assert abs((offset_300 - offset_0) - 4800) <= 2400, (offset_0, offset_300)


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


async def test_a_final_reply_that_extends_the_streamed_text_absorbs_the_late_deltas(stack):
    """The gateway's final send can arrive before the last deltas. A final that
    extends the streamed text replaces it in place; the deltas it already holds
    add nothing."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    await conn.send({"type": "turn_end", "text": "weather please", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.on_reply_delta(reply_id, "t1", 1, "Let me check. It is")
    await session.send_page({"type": "reply", "text": "Let me check. It is sunny."})
    await session.on_reply_delta(reply_id, "t1", 1, " sunny.")
    await asyncio.sleep(0.3)
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert texts == ["Let me check. It is sunny."]


async def test_a_delta_that_is_not_part_of_the_final_reply_starts_its_own_line(stack):
    """A final with nothing streamed is its own line; a delta that does not continue
    it starts a new line."""
    fake, page, sessions, utterances = stack
    (conn,) = await fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    await conn.send({"type": "turn_end", "text": "weather please", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.send_page({"type": "reply", "text": "A."})
    await session.on_reply_delta(reply_id, "t1", 1, " B")
    await asyncio.sleep(0.3)
    texts = await page.eval_on_selector_all("#log .turn.hermes",
                                            "els => els.map(e => e.textContent.trim())")
    assert texts == ["A.", "B"]


async def test_a_final_reply_that_beats_every_delta_absorbs_them(stack):
    """A one-delta answer can reach the page as the final before its delta. The
    final gets the line; the late delta that repeats it adds nothing."""
    fake, page, sessions, utterances = stack
    conn = await _listening(fake, page)
    await conn.send({"type": "turn_end", "text": "do it", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.send_page({"type": "reply", "text": "Done."})
    await session.on_reply_delta(reply_id, "t1", 1, "Done.")
    await asyncio.sleep(0.3)
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert texts == ["Done."]


async def test_a_late_first_delta_with_a_paragraph_break_is_absorbed_too(stack):
    """Hermes opens the first text after a tool call with a paragraph break, which the
    final does not carry. The late delta is still the answer the final already shows."""
    fake, page, sessions, utterances = stack
    conn = await _listening(fake, page)
    await conn.send({"type": "turn_end", "text": "do it", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.send_page({"type": "reply", "text": "Done."})
    await session.on_reply_delta(reply_id, "t1", 2, "\n\nDone.")
    await asyncio.sleep(0.3)
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert texts == ["Done."]


@pytest.mark.parametrize("late", ["Done.\n", "Done. "])
async def test_a_late_delta_that_ends_in_whitespace_is_absorbed_too(stack, late):
    """Hermes strips the final; the delta keeps the whitespace the model ended with."""
    fake, page, sessions, utterances = stack
    conn = await _listening(fake, page)
    await conn.send({"type": "turn_end", "text": "do it", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.send_page({"type": "reply", "text": "Done."})
    await session.on_reply_delta(reply_id, "t1", 1, late)
    await asyncio.sleep(0.3)
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert texts == ["Done."]


async def test_a_final_that_does_not_extend_the_streamed_text_absorbs_its_late_deltas(stack):
    """With interim assistant messages off the final is the last message only, while
    the line still holds the text from before the tool call. The final gets its own
    line, and the late delta that repeats it adds nothing."""
    fake, page, sessions, utterances = stack
    conn = await _listening(fake, page)
    await conn.send({"type": "turn_end", "text": "do it", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    session, reply_id = sessions[0], utterances[0][1]
    await session.on_reply_delta(reply_id, "t1", 1, "Let me check.")
    await session.send_page({"type": "reply", "text": "Done."})
    await session.on_reply_delta(reply_id, "t1", 2, "\n\nDone.")
    await asyncio.sleep(0.3)
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert texts == ["Let me check.", "Done."]


async def _listening(fake, page):
    (conn,) = await fake.wait_connections(1, timeout=10)
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=10000)
    return conn


async def _thinking_after_an_utterance(stack):
    fake, page, sessions, utterances = stack
    conn = await _listening(fake, page)
    await conn.send({"type": "turn_end", "text": "hello", "turn_id": 0})
    await wait_for(lambda: utterances, timeout=5)
    await page.wait_for_function("document.getElementById('state').textContent === 'Thinking'",
                                 timeout=5000)
    return conn, sessions[0], utterances[0][1]


async def test_a_reply_frame_with_nothing_streamed_ends_the_turn_on_the_page(stack):
    """A message the gateway sends without running a turn, such as a busy notice, brings
    no turn_done: the reply frame alone is the whole answer and ends the turn."""
    _, page, _, _ = stack
    _, session, _ = await _thinking_after_an_utterance(stack)
    await session.send_page({"type": "reply", "text": "The gateway is busy, try again later."})
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=2000)
    assert await page.is_hidden("#elapsed")
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert "The gateway is busy, try again later." in texts


async def test_a_speech_failed_frame_adds_a_note_under_the_answer(stack):
    """The server got no audio for the reply: the page says why nothing was heard."""
    _, page, _, _ = stack
    _, session, reply_id = await _thinking_after_an_utterance(stack)
    await session.send_page({"type": "reply_delta", "text": "Flour, water, yeast and salt."})
    await session.send_page({"type": "speech_failed", "request_id": "r1"})
    await page.wait_for_selector("#log .turn.note")
    assert "not spoken" in await page.text_content("#log .turn.note")
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert texts == ["Flour, water, yeast and salt."]


async def test_a_reply_frame_marked_as_sent_during_the_turn_does_not_end_it(stack):
    """The gateway sends a status line, a provider fallback for example, before the
    answer's first text. The turn still runs: the page stays busy, the tool lines stay
    open, and the turn ends once, at turn_done."""
    _, page, _, _ = stack
    _, session, _ = await _thinking_after_an_utterance(stack)
    await session.send_page({"type": "tool_start", "id": "c1", "name": "terminal", "summary": "ls"})
    await session.send_page({"type": "reply", "text": "Switching to the fallback provider.",
                             "turn_running": True})
    await asyncio.sleep(0.5)
    assert await page.text_content("#state") != "Listening"
    assert await page.evaluate("document.querySelectorAll('#tools .group').length") == 0
    assert await page.evaluate("[...document.querySelectorAll('#tools .tool')].every(li => !li.hidden)")
    texts = await page.eval_on_selector_all("#log .turn.hermes", "els => els.map(e => e.textContent)")
    assert "Switching to the fallback provider." in texts
    await session.send_page({"type": "tool_end", "id": "c1", "status": "ok", "duration_ms": 10})
    await session.send_page({"type": "turn_done"})
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=2000)
    assert await page.evaluate("document.querySelectorAll('#tools .group').length") == 1


async def test_a_reply_frame_after_streamed_text_leaves_the_turn_running(stack):
    """The final send of a streamed answer comes before turn_done: the turn is still
    running on the page until that frame."""
    _, page, _, _ = stack
    conn, session, reply_id = await _thinking_after_an_utterance(stack)
    await session.on_reply_delta(reply_id, "t1", 1, "Let me check. ")
    await session.send_page({"type": "reply", "text": "Let me check."})
    await asyncio.sleep(0.5)
    assert await page.text_content("#state") in ("Thinking", "Working")
    await session.on_turn_done(reply_id, "t1", False)
    frames = await wait_for(lambda: conn.json_frames("tts_stream") if any(
        f.get("done") for f in conn.json_frames("tts_stream")) else None, timeout=5)
    rid = frames[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x10\x00" * 2400)  # 0.1 s
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=5000)


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


async def _turn_ended_with_a_tool_running(session, page):
    await session.send_page({"type": "utterance", "text": "run something"})
    await session.send_page({"type": "tool_start", "id": "c1", "name": "terminal", "summary": "make"})
    await page.wait_for_selector("#tools .tool.running")
    await session.send_page({"type": "turn_done"})
    await page.wait_for_selector("#tools .group")
    assert await page.locator("#tools .tool.running").count() == 0
    assert await page.locator("#tools .tool.failed").count() == 1
    assert await page.text_content("#tools .tool .status") == "stopped"


async def test_a_tool_line_still_running_when_the_turn_ends_reads_stopped(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    await _turn_ended_with_a_tool_running(sessions[0], page)
    # Nothing more comes for the call: the line stays stopped.
    await asyncio.sleep(0.2)
    assert await page.text_content("#tools .tool .status") == "stopped"
    assert await page.locator("#tools .tool.failed").count() == 1
    assert await page.locator("#tools .group").count() == 1


async def test_a_tool_end_after_the_turn_ended_turns_the_stopped_line_done(stack):
    # A tool that waits for a spoken approval: the turn ends while the tool waits, the
    # approved tool then runs and ends under the same call id.
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    await _turn_ended_with_a_tool_running(session, page)
    await session.send_page({"type": "tool_end", "id": "c1", "status": "ok", "duration_ms": 1234})
    await page.wait_for_selector("#tools .tool.done", state="attached")  # folded, so hidden
    assert await page.text_content("#tools .tool .status") == "1.2 s"
    assert await page.locator("#tools .tool.failed").count() == 0
    assert await page.locator("#tools .tool.running").count() == 0
    assert await page.locator("#tools .group").count() == 1


async def test_a_failed_tool_end_after_the_turn_ended_turns_the_stopped_line_failed(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    session = sessions[0]
    await _turn_ended_with_a_tool_running(session, page)
    await session.send_page({"type": "tool_end", "id": "c1", "status": "error", "duration_ms": 1234})
    await page.wait_for_function(
        "document.querySelector('#tools .tool .status').textContent === 'failed 1.2 s'")
    assert await page.locator("#tools .tool.failed").count() == 1
    assert await page.locator("#tools .tool.running").count() == 0
    assert await page.locator("#tools .group").count() == 1


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


async def test_a_full_header_stays_within_a_short_wide_window(stack):
    # Two columns at the narrowest width that gets them, on a window too short for the
    # whole header: the reasoning text and the tool list shrink and scroll inside
    # themselves, the header itself does not scroll and the page does not grow.
    fake, page, sessions, utterances = stack
    await page.set_viewport_size({"width": 900, "height": 560})
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "do a lot"})
    await session.send_page({"type": "reasoning_delta", "text": "A long thought. " * 40})
    for i in range(12):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 12")
    await page.click("#reason-fold")
    await asyncio.sleep(0.2)
    box = await page.evaluate("""() => {
        const header = document.querySelector('header');
        const h = header.getBoundingClientRect();
        const b = document.getElementById('toggle').getBoundingClientRect();
        const tools = document.getElementById('tools');
        const full = document.getElementById('reason-full');
        const li = [...document.querySelectorAll('#tools .tool')].pop().getBoundingClientRect();
        return {
            pageScroll: document.documentElement.scrollHeight, window: window.innerHeight,
            headerBottom: h.bottom,
            button: b.top >= 0 && b.bottom <= window.innerHeight,
            toolsScroll: tools.scrollHeight, toolsClient: tools.clientHeight,
            reasonHeight: full.getBoundingClientRect().height,
            reasonMin: parseFloat(getComputedStyle(full).minHeight),
            headerScrollTop: header.scrollTop,
            newest: li.height > 0 && li.top >= h.top - 1 && li.bottom <= h.bottom + 1,
        };
    }""")
    assert box["pageScroll"] <= box["window"], box
    assert box["headerBottom"] <= box["window"], box
    assert box["button"], box
    assert box["toolsScroll"] > box["toolsClient"], box
    assert box["reasonHeight"] >= box["reasonMin"] > 0, box
    assert box["headerScrollTop"] == 0, box
    assert box["newest"], box


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


async def test_the_reconnecting_screen_keeps_the_error_message(stack):
    # The session sends an error frame and closes the page's socket right after it, so
    # the Reconnecting screen must carry the message or nobody ever reads it.
    fake, page, sessions, utterances = stack
    conn = await _listening(fake, page)
    await conn.ws.close()
    await page.wait_for_function("""() => {
        const detail = document.getElementById('detail').textContent;
        return document.getElementById('state').textContent === 'Reconnecting'
            && detail.includes('VoiceManager closed the connection') && detail.includes('Trying again');
    }""", timeout=5000)
    await fake.wait_connections(2, timeout=10)
    await page.wait_for_function("""() => document.getElementById('state').textContent === 'Listening'
        && document.getElementById('detail').textContent === 'Say something.'""", timeout=10000)


async def test_a_stop_forgets_the_error_message(site):
    # The reason the last session gave for its close belongs to that session. After Back
    # to the chooser, a new session whose first socket fails before any frame must not
    # show it on its Reconnecting screen.
    fake, page = site.fake, site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await page.click("#new")
    conn = await _listening(fake, page)
    await conn.ws.close()
    await page.wait_for_function("""() => document.getElementById('state').textContent === 'Reconnecting'
        && document.getElementById('detail').textContent.includes('VoiceManager closed the connection')""",
                                 timeout=5000)
    await page.go_back()
    await page.wait_for_selector("#chooser:not([hidden])", timeout=5000)
    await _chooser_rows(page)
    await site.server.stop()  # the next connect fails before any frame arrives
    await page.click("#new")
    await page.wait_for_function("document.getElementById('state').textContent === 'Reconnecting'", timeout=5000)
    detail = await page.text_content("#detail")
    assert "Trying again" in detail, detail
    assert "VoiceManager closed the connection" not in detail, detail


async def test_a_replaced_page_ends_its_turn_on_the_moved_screen(stack):
    fake, page, sessions, utterances = stack
    await _listening(fake, page)
    await _turn_in_flight(sessions[0], page)
    import websockets
    port = page.url.split(":")[2].split("/")[0]
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={TOKEN}"):
        await page.wait_for_function(MOVED, timeout=5000)
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
    await _scroll_the_header_by_hand(page, to_end=False)
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


async def _fling_the_tool_list(page, stops):
    """Scroll the tool list like a finger fling: a touch, then one scroll step every 150 ms.

    Each stop is a share of the list's scroll range (0 is the top, 1 the end). Ten stops
    spread the scroll over 1.35 s, longer than the second a touch alone counts for."""
    return await page.evaluate("""async (stops) => {
        const list = document.getElementById('tools');
        const range = list.scrollHeight - list.clientHeight;
        list.dispatchEvent(new TouchEvent('touchstart', { bubbles: true }));
        for (const stop of stops) {
            list.scrollTop = stop === 1 ? list.scrollHeight : Math.round(stop * range);
            await new Promise(r => setTimeout(r, 150));
        }
        return range;
    }""", stops)


async def _a_busy_turn_fills_the_tool_list(fake, page, sessions, count=14):
    """A phone-sized page with a running turn of enough tool lines for the list to scroll.

    The list is capped at a share of the window height, so a shorter window shrinks it: the
    page then moves a list that follows its end and leaves one the user scrolled away."""
    await page.set_viewport_size({"width": 390, "height": 664})
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "utterance", "text": "many steps"})
    for i in range(count):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await page.wait_for_function(f"document.querySelectorAll('#tools .tool').length === {count}")
    await asyncio.sleep(0.2)
    return session


async def _tool_list_box(page):
    return await page.evaluate("""() => { const l = document.getElementById('tools');
        return { atEnd: l.scrollTop + l.clientHeight >= l.scrollHeight - 4, top: l.scrollTop,
                 height: l.clientHeight, scroll: l.scrollHeight }; }""")


async def test_a_fling_that_ends_at_the_end_of_the_tool_list_pins_it_again(stack):
    fake, page, sessions, utterances = stack
    session = await _a_busy_turn_fills_the_tool_list(fake, page, sessions)
    # Up to the top inside the first second, then back to the end after it.
    range_ = await _fling_the_tool_list(page, [0.6, 0.3, 0, 0.1, 0, 0.1, 0.2, 0.5, 0.8, 1])
    assert range_ > 0
    await asyncio.sleep(0.5)
    before = await _tool_list_box(page)
    await page.set_viewport_size({"width": 390, "height": 600})
    await asyncio.sleep(0.2)
    after = await _tool_list_box(page)
    assert after["height"] < before["height"], (before, after)
    assert after["atEnd"], (before, after)
    await session.send_page({"type": "tool_start", "id": "n1", "name": "read_file", "summary": "next"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 15")
    await asyncio.sleep(0.2)
    assert (await _tool_list_box(page))["atEnd"]


async def test_a_fling_that_ends_at_the_top_of_the_tool_list_leaves_it_there(stack):
    fake, page, sessions, utterances = stack
    await _a_busy_turn_fills_the_tool_list(fake, page, sessions)
    # At the end when the first second is over, then up to the top after it.
    range_ = await _fling_the_tool_list(page, [0.5, 0, 0.5, 1, 0.5, 0.8, 1, 0.5, 0.2, 0])
    assert range_ > 0
    await asyncio.sleep(0.5)
    before = await _tool_list_box(page)
    await page.set_viewport_size({"width": 390, "height": 600})
    await asyncio.sleep(0.2)
    after = await _tool_list_box(page)
    assert after["height"] < before["height"], (before, after)
    assert after["top"] == 0, (before, after)


async def _header_box(page):
    """Where the header stands and whether its newest tool line is within its visible box."""
    return await page.evaluate("""() => {
        const header = document.querySelector('header');
        const h = header.getBoundingClientRect();
        const last = [...document.querySelectorAll('#tools .tool')].pop();
        const li = last ? last.getBoundingClientRect() : null;
        return { top: header.scrollTop, scrolls: header.scrollHeight > header.clientHeight,
                 window: window.scrollY,
                 newest: !!li && li.height > 0 && li.top >= h.top - 1 && li.bottom <= h.bottom + 1 };
    }""")


async def _a_sideways_phone_with_a_wrapped_transcript(fake, page, sessions):
    """A phone held sideways: the header's rest does not fit its cap, so the header scrolls."""
    await page.set_viewport_size({"width": 740, "height": 360})
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "transcript", "text": "I keep talking about this and that " * 6})
    await page.wait_for_function("document.getElementById('transcript').textContent.length > 100")
    return session


async def test_a_header_that_scrolls_itself_follows_the_newest_tool_line(stack):
    fake, page, sessions, utterances = stack
    session = await _a_sideways_phone_with_a_wrapped_transcript(fake, page, sessions)
    await session.send_page({"type": "utterance", "text": "sideways"})
    for i in range(6):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
        await page.wait_for_function(f"document.querySelectorAll('#tools .tool').length === {i + 1}")
        await asyncio.sleep(0.2)
        box = await _header_box(page)
        assert box["newest"], (i, box)
        assert box["window"] == 0, (i, box)
    assert box["scrolls"] and box["top"] > 0, box
    for height in (300, 360):
        await page.set_viewport_size({"width": 740, "height": height})
        await asyncio.sleep(0.2)
        box = await _header_box(page)
        assert box["newest"], (height, box)
        assert box["window"] == 0, (height, box)
    await session.send_page({"type": "transcript", "text": "x" * 200})
    await page.wait_for_function("document.getElementById('transcript').textContent.length === 200")
    await asyncio.sleep(0.2)
    box = await _header_box(page)
    assert box["newest"], box
    assert box["window"] == 0, box


async def _scroll_the_header_by_hand(page, to_end):
    """A wheel gesture on the header, then its scroll: to the top, or to its end."""
    await page.evaluate("""(toEnd) => {
        const header = document.querySelector('header');
        header.dispatchEvent(new WheelEvent('wheel', { bubbles: true }));
        header.scrollTop = toEnd ? header.scrollHeight : 0;
    }""", to_end)


async def test_a_header_scrolled_up_by_hand_stays_until_scrolled_back_to_its_end(stack):
    fake, page, sessions, utterances = stack
    session = await _a_sideways_phone_with_a_wrapped_transcript(fake, page, sessions)
    await session.send_page({"type": "utterance", "text": "sideways"})
    for i in range(4):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 4")
    await asyncio.sleep(0.2)
    box = await _header_box(page)
    assert box["scrolls"] and box["top"] > 0 and box["newest"], box
    await _scroll_the_header_by_hand(page, to_end=False)
    await asyncio.sleep(0.2)
    for i in range(10):
        await session.send_page({"type": "reasoning_delta", "text": f"Thought {i}. "})
    await page.wait_for_selector("#reasoning:not([hidden])")
    await session.send_page({"type": "tool_start", "id": "c4", "name": "terminal", "summary": "step 4"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 5")
    await session.send_page({"type": "transcript", "text": "y" * 200})
    await page.wait_for_function("document.getElementById('transcript').textContent.length === 200")
    await asyncio.sleep(0.3)
    box = await _header_box(page)
    assert box["top"] == 0, box
    assert box["window"] == 0, box
    await _scroll_the_header_by_hand(page, to_end=True)
    await asyncio.sleep(0.2)
    await session.send_page({"type": "tool_start", "id": "c5", "name": "terminal", "summary": "step 5"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 6")
    await asyncio.sleep(0.2)
    box = await _header_box(page)
    assert box["newest"], box
    assert box["window"] == 0, box


async def test_a_portrait_header_does_not_scroll_itself_while_the_tool_list_follows_its_end(stack):
    fake, page, sessions, utterances = stack
    await page.set_viewport_size({"width": 390, "height": 760})
    await _listening(fake, page)
    session = sessions[0]
    await session.send_page({"type": "transcript", "text": "I keep talking about this and that " * 3})
    await session.send_page({"type": "utterance", "text": "portrait"})
    for i in range(12):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
        if i % 4 == 3:
            await session.send_page({"type": "reasoning_delta", "text": "A long thought. " * 10})
            await page.wait_for_function(f"document.querySelectorAll('#tools .tool').length === {i + 1}")
            await asyncio.sleep(0.2)
            assert await page.evaluate("document.querySelector('header').scrollTop") == 0, i
            assert await _newest_tool_in_view(page), i
    await session.send_page({"type": "transcript", "text": "z" * 120})
    await page.wait_for_function("document.getElementById('transcript').textContent.length === 120")
    await asyncio.sleep(0.2)
    box = await _header_box(page)
    assert box["top"] == 0 and box["window"] == 0, box
    assert await _newest_tool_in_view(page)
    assert await page.evaluate("""() => { const t = document.getElementById('tools');
        return t.scrollHeight > t.clientHeight && t.scrollTop + t.clientHeight >= t.scrollHeight - 4; }""")


async def test_a_header_with_no_tool_line_stays_at_its_top(stack):
    fake, page, sessions, utterances = stack
    await page.set_viewport_size({"width": 740, "height": 360})
    await _listening(fake, page)
    session = sessions[0]
    await asyncio.sleep(0.2)
    box = await _header_box(page)
    assert box["scrolls"], box
    assert box["top"] == 0, ("join", box)
    await session.send_page({"type": "transcript", "text": "I keep talking about this and that " * 6})
    await page.wait_for_function("document.getElementById('transcript').textContent.length > 200")
    await asyncio.sleep(0.2)
    box = await _header_box(page)
    assert box["scrolls"], box
    assert box["top"] == 0, ("transcript", box)
    await session.send_page({"type": "utterance", "text": "sideways"})
    for i in range(10):
        await session.send_page({"type": "reasoning_delta", "text": f"Thought {i}. "})
    await page.wait_for_function("document.getElementById('reason-full').textContent.includes('Thought 9')")
    await asyncio.sleep(0.2)
    box = await _header_box(page)
    assert box["scrolls"], box
    assert box["top"] == 0, ("reasoning", box)
    assert box["window"] == 0, box


async def test_a_header_whose_tool_list_is_hidden_stays_where_it_is(stack):
    fake, page, sessions, utterances = stack
    session = await _a_sideways_phone_with_a_wrapped_transcript(fake, page, sessions)
    await session.send_page({"type": "utterance", "text": "sideways"})
    for i in range(6):
        await session.send_page({"type": "tool_start", "id": f"c{i}", "name": "terminal", "summary": f"step {i}"})
    await page.wait_for_function("document.querySelectorAll('#tools .tool').length === 6")
    await asyncio.sleep(0.2)
    box = await _header_box(page)
    assert box["scrolls"] and box["top"] > 0 and box["newest"], box
    await page.click("#fold")
    await page.wait_for_selector("#tools[hidden]", state="attached")
    await asyncio.sleep(0.2)
    # The fold itself may move the header: the list is gone, so measure after it. A
    # longer transcript then makes the header taller, which must not move it again.
    folded = await page.evaluate("""() => { const h = document.querySelector('header');
        return { top: h.scrollTop, height: h.scrollHeight }; }""")
    await session.send_page({"type": "transcript", "text": "I keep talking about this and that " * 12})
    await page.wait_for_function("document.getElementById('transcript').textContent.length > 400")
    await asyncio.sleep(0.2)
    grown = await page.evaluate("document.querySelector('header').scrollHeight")
    assert grown > folded["height"], (folded, grown)
    box = await _header_box(page)
    assert box["top"] == folded["top"], (folded, box)
    assert box["window"] == 0, box


# --- the look: the Hermes dashboard palette, title font and button style ------------------

CREAM, TEAL = "rgb(255, 230, 203)", "rgb(4, 28, 28)"


async def _style(page, selector, *props):
    return await page.eval_on_selector(
        selector, "(el, props) => { const s = getComputedStyle(el); return props.map(p => s[p]); }", list(props))


async def test_the_page_uses_the_cream_on_teal_palette_and_a_system_sans_body(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    background, color, family = await _style(page, "body", "backgroundColor", "color", "fontFamily")
    assert (background, color) == (TEAL, CREAM)
    assert family.startswith("system-ui")
    assert await page.get_attribute('meta[name="theme-color"]', "content") == "#041c1c"
    (muted,) = await _style(page, "#detail", "color")
    assert muted == "rgba(255, 230, 203, 0.8)"


async def test_the_title_color_follows_who_is_speaking(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    for who, expected in (("hermes", CREAM), ("you", "rgb(52, 211, 153)"), ("alert", "rgb(251, 44, 54)")):
        await page.evaluate("who => { document.body.dataset.who = who; }", who)
        await page.wait_for_function(
            "c => getComputedStyle(document.getElementById('state')).color === c", arg=expected, timeout=2000)


async def test_the_title_font_is_eb_garamond_italic_from_inside_the_page(site):
    page = site.page
    await page.goto(f"{site.url}/?token={TOKEN}")
    await page.evaluate("document.fonts.load('italic 400 44px \"EB Garamond\"')")
    assert await page.evaluate("document.fonts.check('italic 400 44px \"EB Garamond\"')")
    # check() is also true for a family the page never declared, so look for the face itself.
    assert await page.evaluate(
        "[...document.fonts].some(f => f.family.replace(/\"/g, '') === 'EB Garamond' && f.status === 'loaded')")
    family, style, weight = await _style(page, "#state", "fontFamily", "fontStyle", "fontWeight")
    assert family.startswith('"EB Garamond"') and style == "italic" and weight == "400"
    html = await page.content()
    assert not re.search(r"""(?:src|href)\s*=\s*["']?https?://""", html)
    assert all(urlsplit(url).netloc == urlsplit(site.url).netloc for url in site.http_requests
               if urlsplit(url).scheme in ("http", "https")), site.http_requests
