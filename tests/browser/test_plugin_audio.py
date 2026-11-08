"""The Desktop plugin's voice audio, run in headless Chrome.

createVoiceAudio sends the microphone as 16 kHz mono 16-bit PCM in 20 ms
binary frames, as the voice page does, plays reply audio and reports its
playback edges as JSON frames, and stops all of a reply's audio at once on a
clear frame. It opens no socket: the harness page records what it sends.

Chrome's fake microphone plays a 440 Hz tone WAV. The harness loads
desktop/plugin.js as the app does, from blob: modules, so one test can run it
under the Webapp's Content-Security-Policy.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import math
import os
import re
import struct
import threading
import wave
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "browser" / "audio_harness.html"
TONE_HZ = 440
MIC_RATE = 16000
FRAME_BYTES = 640  # 20 ms of 16 kHz mono 16-bit PCM


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


def _webapp_policy() -> str:
    """The script and worker directives of the Webapp's page policy
    (hermes_cli/webapp_page_policy.py), with the hash of the harness's inline
    script in place of the app build's."""
    inline = re.search(r"<script>(.*?)</script>", HARNESS.read_text(encoding="utf-8"), re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(inline.encode("utf-8")).digest()).decode("ascii")
    return "; ".join((
        "default-src 'none'",
        f"script-src 'self' blob: 'wasm-unsafe-eval' 'sha256-{digest}'",
        "script-src-attr 'none'",
        "connect-src * ws: wss: data: blob:",
        "media-src * data: blob:",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'none'",
    ))


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the repository; the harness under the Webapp's policy when asked with ?csp."""

    def end_headers(self):
        if self.path.startswith("/tests/browser/audio_harness.html?csp"):
            self.send_header("Content-Security-Policy", _webapp_policy())
        super().end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(_Handler, directory=str(ROOT))
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


async def _open(site, tone_wav, query=""):
    pw = await playwright_api.async_playwright().start()
    browser = await pw.chromium.launch(
        executable_path=os.environ.get("CHROME") or None, headless=True,
        args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
              f"--use-file-for-fake-audio-capture={tone_wav}", "--autoplay-policy=no-user-gesture-required"])
    page = await browser.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    await page.goto(f"{site}/tests/browser/audio_harness.html{query}")
    await page.wait_for_function("() => window.harnessReady || window.harnessError", timeout=10000)
    harness_error = await page.evaluate("window.harnessError || ''")
    assert not harness_error, harness_error
    assert not errors, errors
    await page.evaluate("window.makeAudio()")
    return pw, browser, page, errors


@pytest.fixture
async def page(site, tone_wav):
    pw, browser, page, errors = await _open(site, tone_wav)
    try:
        yield page
    finally:
        await browser.close()
        await pw.stop()
    assert not errors, errors


async def _start(page):
    await page.evaluate("async () => { await window.openMic(); await window.audio.start(window.stream) }")


async def _mic_pcm(page, seconds=2):
    """Capture ``seconds`` of mic audio after one second of device warm-up."""
    total = (seconds + 1) * MIC_RATE * 2
    await page.wait_for_function(f"() => window.binBytes() >= {total}", timeout=15000)
    return base64.b64decode(await page.evaluate(f"window.pcmBase64({MIC_RATE * 2}, {total})"))


async def _playback(page):
    return await page.evaluate("window.jsons.filter(f => f.type === 'playback')")


async def _wait_playback(page, count, timeout=10000):
    await page.wait_for_function(
        f"() => window.jsons.filter(f => f.type === 'playback').length >= {count}", timeout=timeout)
    return await _playback(page)


def _mic_sample(frame):
    """The frame's mic_sample, checked to be a whole number of mic samples."""
    value = frame["mic_sample"]
    assert isinstance(value, int) and not isinstance(value, bool), frame
    assert value >= 0, frame
    return value


def _without_mic_sample(frames):
    """The playback frames without mic_sample, after checking each has one."""
    for frame in frames:
        _mic_sample(frame)
    return [{k: v for k, v in f.items() if k != "mic_sample"} for f in frames]


async def _playback_with_sent(page):
    """Each playback frame with the mic samples the harness had sent when it went out."""
    return await page.evaluate(
        "window.jsons.map((f, i) => [f, window.sentAt[i]]).filter(([f]) => f.type === 'playback')")


def _check_mic_samples(frames_with_sent, played_samples, rate=24000):
    """The start frame's mic_sample is near the mic samples sent when it went
    out, and the stop's is after it by the length of the audio heard."""
    (start, sent_at_start), (stop, _) = frames_with_sent
    assert start["event"] == "start" and stop["event"] == "stop"
    start_sample, stop_sample = _mic_sample(start), _mic_sample(stop)
    assert abs(start_sample - sent_at_start) <= 8000, (start_sample, sent_at_start)
    heard = played_samples * MIC_RATE / rate
    assert stop_sample >= start_sample
    assert abs((stop_sample - start_sample) - heard) <= 8000, (start_sample, stop_sample, heard)


async def _send_reply(page, request_id, samples, chunk=4800, rate=24000, end=True):
    """audio_start, the reply's PCM in chunks, and audio_end, as the server sends them."""
    await page.evaluate(
        """([id, samples, chunk, rate, end]) => {
          const a = window.audio
          a.onJson({ type: 'audio_start', request_id: id, sample_rate: rate })
          const all = new Int16Array(window.tone(samples, rate, 300))
          for (let at = 0; at < samples; at += chunk) a.onBinary(all.slice(at, at + chunk).buffer)
          if (end) a.onJson({ type: 'audio_end', request_id: id, reason: 'complete' })
        }""",
        [request_id, samples, chunk, rate, end])


async def test_plugin_exports_the_audio_factory(page):
    assert await page.evaluate("typeof window.plugin.createVoiceAudio") == "function"
    assert await page.evaluate("typeof window.plugin.default.register") == "function"
    assert await page.evaluate("typeof window.plugin.createSayCutter") == "function"
    names = await page.evaluate("Object.keys(window.audio).sort()")
    assert names == ["onBinary", "onJson", "start", "stop"]


async def test_mic_goes_out_as_16k_mono_pcm_in_20ms_frames(page):
    await _start(page)
    pcm = await _mic_pcm(page)
    hz = _dominant_hz(pcm, MIC_RATE)
    assert abs(hz - TONE_HZ) < 25, f"tone read at 16 kHz is {hz:.0f} Hz"
    sizes = await page.evaluate("window.bins.map(b => b.byteLength)")
    assert set(sizes) == {FRAME_BYTES}
    assert await page.evaluate("window.bins.every(b => b instanceof ArrayBuffer)")
    assert await page.evaluate("window.jsons.length") == 0  # capture sends no JSON


async def test_a_reply_plays_and_reports_its_playback_edges(page):
    await _start(page)
    await _send_reply(page, "r1", 12000)  # 0.5 s at 24 kHz
    playback = await _wait_playback(page, 2)
    assert _without_mic_sample(playback) == [
        {"type": "playback", "event": "start", "request_id": "r1"},
        {"type": "playback", "event": "stop", "request_id": "r1", "reply_sample": 12000},
    ]
    assert await page.evaluate("window.sources.length") == 3
    assert await page.evaluate("window.sources.every(s => s.endedAt !== null)")
    assert await page.evaluate("window.speaking") == [True, False]


async def test_playback_frames_place_the_reply_on_the_mic_stream_with_mic_sample(page):
    """Both edges carry mic_sample: the start near the mic samples sent when
    the reply was first heard, the stop later by the length of the reply."""
    await _start(page)
    await _send_reply(page, "r1", 12000)  # 0.5 s at 24 kHz
    await _wait_playback(page, 2)
    _check_mic_samples(await _playback_with_sent(page), 12000)


async def test_a_cleared_reply_reports_mic_sample_for_the_part_heard(page):
    await _start(page)
    await _send_reply(page, "r1", 48000, end=False)  # 2 s
    await _wait_playback(page, 1)
    await page.wait_for_timeout(300)
    await page.evaluate("window.audio.onJson({ type: 'clear', request_id: 'r1' })")
    frames = await _playback_with_sent(page)
    assert len(frames) == 2
    stop = frames[1][0]
    assert 0 < stop["reply_sample"] < 48000
    _check_mic_samples(frames, stop["reply_sample"])


async def test_mic_sample_holds_without_the_latency_apis(page):
    """With no output timestamp, no output latency and no track latency the
    edges are mapped through the context clock alone."""
    await page.evaluate("""() => {
      AudioContext.prototype.getOutputTimestamp = () => ({ contextTime: 0, performanceTime: 0 })
      Object.defineProperty(AudioContext.prototype, 'outputLatency', { get: () => undefined, configurable: true })
      MediaStreamTrack.prototype.getSettings = () => ({})
    }""")
    await _start(page)
    await _send_reply(page, "r1", 12000)
    await _wait_playback(page, 2)
    _check_mic_samples(await _playback_with_sent(page), 12000)


async def test_a_reply_finishes_when_audio_end_comes_after_the_audio(page):
    await _start(page)
    await _send_reply(page, "r1", 4800, end=False)  # 0.2 s
    await _wait_playback(page, 1)
    await page.wait_for_function("() => window.sources.every(s => s.endedAt !== null)", timeout=5000)
    assert len(await _playback(page)) == 1  # played out, but the reply is not over yet
    await page.evaluate("window.audio.onJson({ type: 'audio_end', request_id: 'r1', reason: 'complete' })")
    playback = await _wait_playback(page, 2, timeout=2000)
    assert _without_mic_sample(playback)[1] == {
        "type": "playback", "event": "stop", "request_id": "r1", "reply_sample": 4800}


async def test_clear_stops_all_scheduled_playback_at_once(page):
    await _start(page)
    await _send_reply(page, "r1", 48000, end=False)  # 2 s in ten chunks
    await _wait_playback(page, 1)
    await page.wait_for_timeout(300)
    result = await page.evaluate(
        """() => new Promise(resolve => {
          const t0 = performance.now()
          window.audio.onJson({ type: 'clear', request_id: 'r1' })
          const sent = window.jsons.filter(f => f.type === 'playback').length
          const wait = () => {
            if (window.sources.every(s => s.endedAt !== null)) {
              resolve({ ms: Math.max(...window.sources.map(s => s.endedAt)) - t0, sent })
            } else if (performance.now() - t0 > 2000) {
              resolve({ ms: null, sent })
            } else {
              setTimeout(wait, 2)
            }
          }
          wait()
        })""")
    assert result["ms"] is not None, "scheduled sources still playing 2 s after clear"
    assert result["ms"] < 100, f"playback stopped {result['ms']:.0f} ms after clear"
    assert result["sent"] == 2  # the stop frame goes out with the clear
    stop = (await _playback(page))[1]
    assert stop["event"] == "stop" and stop["request_id"] == "r1"
    assert 0 < stop["reply_sample"] < 48000
    # More audio of the cleared reply is dropped.
    await page.evaluate("window.audio.onBinary(window.tone(4800, 24000, 300))")
    assert await page.evaluate("window.sources.length") == 10


async def test_clear_for_another_reply_leaves_playback_alone(page):
    await _start(page)
    await _send_reply(page, "r1", 24000)  # 1 s
    await _wait_playback(page, 1)
    await page.evaluate("window.audio.onJson({ type: 'clear', request_id: 'other' })")
    playback = await _wait_playback(page, 2, timeout=5000)
    assert playback[1]["reply_sample"] == 24000  # it played to the end


async def test_clear_without_a_request_id_stops_the_running_reply(page):
    await _start(page)
    await _send_reply(page, "r1", 48000, end=False)
    await _wait_playback(page, 1)
    await page.evaluate("window.audio.onJson({ type: 'clear', request_id: null })")
    playback = await _playback(page)
    assert len(playback) == 2 and playback[1]["reply_sample"] < 48000
    await page.wait_for_function("() => window.sources.every(s => s.endedAt !== null)", timeout=1000)


async def test_a_reply_that_starts_while_one_plays_is_queued_behind_it(page):
    """One turn can bring several replies, and speech comes faster than it
    plays: the second reply's audio_start arrives while the first is heard."""
    await _start(page)
    await _send_reply(page, "r1", 24000)  # 1 s
    await _wait_playback(page, 1)
    await _send_reply(page, "r2", 12000)  # 0.5 s
    playback = await _wait_playback(page, 4, timeout=5000)
    assert _without_mic_sample(playback) == [
        {"type": "playback", "event": "start", "request_id": "r1"},
        {"type": "playback", "event": "stop", "request_id": "r1", "reply_sample": 24000},
        {"type": "playback", "event": "start", "request_id": "r2"},
        {"type": "playback", "event": "stop", "request_id": "r2", "reply_sample": 12000},
    ]
    times = await page.evaluate(
        "window.jsons.map((f, i) => [f, window.jsonTimes[i]]).filter(([f]) => f.type === 'playback').map(([, t]) => t)")
    start1, stop1, start2, stop2 = times
    assert stop1 - start1 > 900, f"the first reply was heard for {stop1 - start1:.0f} ms of 1000"
    assert start2 >= stop1
    assert stop2 - start2 > 400, f"the second reply was heard for {stop2 - start2:.0f} ms of 500"
    assert await page.evaluate("window.sources.every(s => s.endedAt !== null)")
    assert await page.evaluate("window.speaking") == [True, False]


@pytest.mark.parametrize("cleared", ["r1", "r2", None])
async def test_clear_stops_the_playing_reply_and_the_ones_queued_behind_it(page, cleared):
    await _start(page)
    await _send_reply(page, "r1", 24000)  # 1 s
    await _wait_playback(page, 1)
    await _send_reply(page, "r2", 24000, end=False)
    await page.wait_for_timeout(200)
    result = await page.evaluate(
        """(id) => new Promise(resolve => {
          const t0 = performance.now()
          window.audio.onJson({ type: 'clear', request_id: id })
          const wait = () => {
            if (window.sources.every(s => s.endedAt !== null)) {
              resolve(Math.max(...window.sources.map(s => s.endedAt)) - t0)
            } else if (performance.now() - t0 > 2000) {
              resolve(null)
            } else {
              setTimeout(wait, 2)
            }
          }
          wait()
        })""",
        cleared)
    assert result is not None, "scheduled sources still playing 2 s after clear"
    assert result < 100, f"playback stopped {result:.0f} ms after clear"
    playback = await _playback(page)
    assert [(f["event"], f["request_id"]) for f in playback] == [("start", "r1"), ("stop", "r1")]
    assert 0 < playback[1]["reply_sample"] < 24000
    assert await page.evaluate("window.speaking") == [True, False]
    # More audio of the cleared reply is dropped, and nothing queued plays later.
    await page.evaluate("window.audio.onBinary(window.tone(4800, 24000, 300))")
    assert await page.evaluate("window.sources.length") == 10
    await page.wait_for_timeout(1200)
    assert len(await _playback(page)) == 2


async def test_stop_releases_the_mic_and_the_audio_contexts(page):
    await _start(page)
    await page.wait_for_function(f"() => window.binBytes() >= {10 * FRAME_BYTES}", timeout=10000)
    await _send_reply(page, "r1", 48000, end=False)
    await _wait_playback(page, 1)
    await page.evaluate("window.audio.stop()")
    states = await page.evaluate("window.stream.getTracks().map(t => t.readyState)")
    assert states and set(states) == {"ended"}
    await page.wait_for_function("() => window.contexts.every(c => c.state === 'closed')", timeout=2000)
    assert await page.evaluate("window.contexts.length") >= 2  # capture and playback
    playback = await _playback(page)
    assert [f["event"] for f in playback] == ["start", "stop"]  # the cut-off reply is reported
    sent = await page.evaluate("window.bins.length")
    await page.wait_for_timeout(300)
    assert await page.evaluate("window.bins.length") == sent  # no frames after stop
    # Frames that come after stop play nothing.
    await page.evaluate("""() => {
      window.audio.onJson({ type: 'audio_start', request_id: 'r2', sample_rate: 24000 })
      window.audio.onBinary(window.tone(4800, 24000, 300))
    }""")
    assert await page.evaluate("window.sources.length") == 10


async def test_stop_during_start_leaves_nothing_running(page):
    await page.evaluate("""async () => {
      await window.openMic()
      const starting = window.audio.start(window.stream)
      await window.audio.stop()
      await starting
    }""")
    await page.wait_for_timeout(300)
    assert await page.evaluate("window.bins.length") == 0
    assert await page.evaluate("window.contexts.every(c => c.state === 'closed')")
    assert set(await page.evaluate("window.stream.getTracks().map(t => t.readyState)")) == {"ended"}


async def test_capture_worklet_loads_under_the_webapp_policy(site, tone_wav):
    """The worklet module comes from a blob: URL; the Webapp's script-src allows blob:."""
    pw, browser, page, errors = await _open(site, tone_wav, "?csp")
    try:
        # The policy is in force: its script-src has no data:.
        refused = await page.evaluate(
            "import('data:text/javascript,export default 1').then(() => false, () => true)")
        assert refused, "the harness is not under the policy"
        await _start(page)
        pcm = await _mic_pcm(page, seconds=1)
        assert abs(_dominant_hz(pcm, MIC_RATE) - TONE_HZ) < 25
        violations = await page.evaluate("window.violations")
        assert [v for v in violations if not v.endswith(" data")] == []  # only the probe was refused
    finally:
        await browser.close()
        await pw.stop()
    assert not errors, errors
