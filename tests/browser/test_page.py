"""The voice page in headless Chrome, against the plugin's server and a fake VoiceManager.

Chrome's fake microphone plays a 440 Hz tone WAV. The test checks that the
plugin gets it as 16 kHz mono PCM, that a reply's audio plays and its
playback edges come back, and that the page shows what was said.
"""

from __future__ import annotations

import asyncio
import math
import os
import struct
import sys
import wave

import pytest

from fake_vm import FakeVoiceManager, wait_for

playwright_api = pytest.importorskip("playwright.async_api")

TOKEN = "page-token"
TONE_HZ = 440


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


@pytest.fixture
async def stack(tmp_path):
    plugin = sys.modules["voicemanager_live"]
    from voicemanager_live.server import VoiceServer
    from voicemanager_live.session import VoiceSession, client_from_url

    async with FakeVoiceManager() as fake:
        sessions, utterances = [], []

        async def on_client(connection, client_id):
            async def on_utterance(text, message_id):
                utterances.append((text, message_id))

            session = VoiceSession(connection, client_from_url(fake.url), client_id, on_utterance=on_utterance)
            sessions.append(session)
            await session.run()

        server = VoiceServer("127.0.0.1", 0, TOKEN, on_client)
        await server.start()
        wav = tmp_path / "tone.wav"
        _tone_wav(wav)
        async with playwright_api.async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=os.environ.get("CHROME") or None, headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                      f"--use-file-for-fake-audio-capture={wav}", "--autoplay-policy=no-user-gesture-required"])
            page = await browser.new_page(viewport={"width": 390, "height": 760})
            await page.goto(f"http://127.0.0.1:{server.bound_port}/?token={TOKEN}&client=phone")
            await page.click("#toggle")
            try:
                yield fake, page, sessions, utterances
            finally:
                await browser.close()
                await server.stop()
        assert plugin  # loaded by conftest


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
    await session.on_reply_delta(reply_id, "t1", 1, "Hi.")
    rid = (await wait_for(lambda: conn.json_frames("tts_stream")))[0]["request_id"]
    await conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await conn.send(b"\x10\x00" * 4800)  # 0.2 s
    await wait_for(lambda: conn.json_frames("playback"), timeout=10)  # it started playing
    await asyncio.sleep(1.0)  # ... and finished long before the reply's text ends
    await session.on_turn_done(reply_id, "t1", False)
    await conn.send({"type": "audio_end", "request_id": rid, "reason": "complete"})
    playback = await wait_for(lambda: conn.json_frames("playback") if len(conn.json_frames("playback")) >= 2
                              else None, timeout=5)
    assert [f["event"] for f in playback] == ["start", "stop"]
    assert playback[1]["reply_sample"] == 4800
    await page.wait_for_function("document.getElementById('state').textContent === 'Listening'",
                                 timeout=5000)
