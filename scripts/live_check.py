#!/usr/bin/env python3
"""Live check of a running voice page: how soon a spoken question gets a spoken answer.

Two commands:

  live_check.py make-wav --vm URL --vm-token TOKEN --text "..." --out question.wav
      Speak TEXT with VoiceManager and save it as 16 kHz mono WAV.

  live_check.py run --page ws://HOST:PORT --token TOKEN --wav question.wav [--runs 5]
      Speak the WAV into the page's WebSocket in real time, as the page would, and
      time each run from the plugin's utterance frame (sent when the turn ended) to
      the first reply audio. Each run uses a new client id, so a new conversation.
      Prints each run and the median; exits 1 when the median is over --max-ms.

It needs websockets and httpx (tests/browser/requirements.txt has both).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
import time
import wave
from array import array
from pathlib import Path

import websockets

FRAME = 320  # 20 ms at 16 kHz
RATE = 16000


def _vm_client():
    path = Path(__file__).resolve().parent.parent / "vm_client.py"
    spec = importlib.util.spec_from_file_location("vm_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resample(pcm: bytes, src: int, dst: int) -> bytes:
    data = array("h", pcm)
    if src == dst or not data:
        return pcm
    out = array("h")
    step = src / dst
    pos = 0.0
    while pos < len(data) - 1:
        i = int(pos)
        frac = pos - i
        out.append(int(data[i] * (1 - frac) + data[i + 1] * frac))
        pos += step
    return out.tobytes()


async def make_wav(args) -> int:
    from urllib.parse import urlsplit

    vm = _vm_client()
    parts = urlsplit(args.vm)
    secure = parts.scheme in ("https", "wss")
    async with vm.VoiceManagerClient(host=parts.hostname, port=parts.port or 8255, token=args.vm_token,
                                     use_ssl=secure) as client:
        pcm, rate = await client.synthesize_tts(args.text)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(_resample(pcm, rate, RATE))
    print(f"wrote {args.out}")
    return 0


def _read_wav(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("the WAV must be mono 16-bit")
        return _resample(w.readframes(w.getnframes()), w.getframerate(), RATE)


async def one_run(page_url: str, token: str, question: bytes, client: str, timeout: float) -> tuple[str, float]:
    url = f"{page_url.rstrip('/')}/ws?token={token}&client={client}"
    async with websockets.connect(url, max_size=2**22) as ws:
        ready = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if ready.get("type") != "ready":
            raise RuntimeError(f"no ready frame: {ready}")
        done = asyncio.Event()
        result: dict = {}
        audio = b"\x00\x00" * RATE * 3 + question  # 3 s of room first: the turn engine calibrates

        async def speak():
            start = time.monotonic()
            sent = 0
            while not done.is_set():
                chunk = audio[sent * 2: (sent + FRAME) * 2] or b"\x00\x00" * FRAME
                await ws.send(chunk.ljust(FRAME * 2, b"\x00"))
                sent += FRAME
                delay = start + sent / RATE - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

        async def listen():
            utterance_at = None
            request_id, samples = None, 0
            async for message in ws:
                now = time.monotonic()
                if isinstance(message, bytes):
                    if utterance_at is not None and "first_audio_ms" not in result:
                        result["first_audio_ms"] = (now - utterance_at) * 1000
                        await ws.send(json.dumps({"type": "playback", "event": "start", "request_id": request_id}))
                    samples += len(message) // 2
                    continue
                frame = json.loads(message)
                kind = frame.get("type")
                if kind == "utterance" and utterance_at is None:
                    utterance_at = now
                    result["heard"] = frame.get("text", "")
                elif kind == "audio_start" and utterance_at is not None and request_id is None:
                    request_id = frame.get("request_id")
                elif kind == "audio_end" and frame.get("request_id") == request_id and request_id:
                    await ws.send(json.dumps({"type": "playback", "event": "stop", "request_id": request_id,
                                              "reply_sample": samples}))
                    done.set()
                    return
                elif kind == "reply":
                    result["reply"] = frame.get("text", "")

        speaker = asyncio.create_task(speak())
        try:
            await asyncio.wait_for(listen(), timeout)
        finally:
            done.set()
            speaker.cancel()
        if "first_audio_ms" not in result:
            raise RuntimeError("no reply audio")
        return result.get("heard", ""), result["first_audio_ms"]


async def run(args) -> int:
    question = _read_wav(args.wav)
    times = []
    for n in range(args.runs):
        heard, ms = await one_run(args.page, args.token, question, f"live-check-{int(time.time())}-{n}",
                                  args.timeout)
        times.append(ms)
        print(f"run {n + 1}: heard {heard!r}, reply audio after {ms:.0f} ms")
    median = statistics.median(times)
    print(f"median {median:.0f} ms over {len(times)} runs (limit {args.max_ms:.0f} ms)")
    return 0 if median <= args.max_ms else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    mk = sub.add_parser("make-wav")
    mk.add_argument("--vm", required=True)
    mk.add_argument("--vm-token", required=True)
    mk.add_argument("--text", required=True)
    mk.add_argument("--out", required=True)
    rn = sub.add_parser("run")
    rn.add_argument("--page", required=True, help="ws://host:port of the voice page")
    rn.add_argument("--token", required=True)
    rn.add_argument("--wav", required=True)
    rn.add_argument("--runs", type=int, default=5)
    rn.add_argument("--max-ms", type=float, default=2000)
    rn.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    return asyncio.run(make_wav(args) if args.command == "make-wav" else run(args))


if __name__ == "__main__":
    sys.exit(main())
