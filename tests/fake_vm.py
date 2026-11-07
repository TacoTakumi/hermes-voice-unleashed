"""A fake VoiceManager server for the plugin tests.

It speaks the parts of the /ws/stream protocol the plugin uses: the ready
handshake, the turn_engine opt-in (accepted or refused), mic audio,
tts_stream, tts_cancel and playback frames. A test reads what each
connection received and pushes server frames with ``send``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from websockets.asyncio.server import ServerConnection, serve


@dataclass
class FakeConnection:
    ws: ServerConnection
    frames: list = field(default_factory=list)  # every frame received, in order (bytes or dict)
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def audio(self) -> bytes:
        return b"".join(f for f in self.frames if isinstance(f, bytes))

    def json_frames(self, kind: Optional[str] = None) -> list[dict]:
        return [f for f in self.frames if isinstance(f, dict) and (kind is None or f.get("type") == kind)]

    async def send(self, payload: Any) -> None:
        await self.ws.send(payload if isinstance(payload, bytes) else json.dumps(payload))


class FakeVoiceManager:
    """``turn_engine``: None accepts the opt-in, an error_type string refuses it."""

    def __init__(self, turn_engine_refusal: Optional[str] = None, stt_engine: str = "audiocpp",
                 answer_turn_start: bool = True) -> None:
        self.turn_engine_refusal = turn_engine_refusal
        self.answer_turn_start = answer_turn_start
        self.stt_engine = stt_engine
        self.connections: list[FakeConnection] = []
        self.new_connection = asyncio.Event()
        self._server = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    async def __aenter__(self) -> "FakeVoiceManager":
        self._server = await serve(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def wait_connections(self, count: int, timeout: float = 3.0) -> list[FakeConnection]:
        async with asyncio.timeout(timeout):
            while len(self.connections) < count:
                self.new_connection.clear()
                await self.new_connection.wait()
        return self.connections

    async def _handle(self, ws: ServerConnection) -> None:
        conn = FakeConnection(ws)
        self.connections.append(conn)
        self.new_connection.set()
        await ws.send(json.dumps({
            "type": "ready", "session_id": f"s{len(self.connections)}",
            "audio_format": {"sample_rate": 16000, "channels": 1, "sample_width": 2},
            "stt_engine": self.stt_engine, "tts_engine": "voxtream", "available_voices": ["default"],
            "default_voice": "default"}))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    conn.frames.append(message)
                    continue
                frame = json.loads(message)
                conn.frames.append(frame)
                if frame.get("type") == "turn_engine":
                    if self.turn_engine_refusal is None:
                        await conn.send({"type": "turn_engine_ready", "engine": frame["engine"]})
                    else:
                        await conn.send({"type": "error", "error_type": self.turn_engine_refusal,
                                         "message": "refused"})
                elif frame.get("type") == "turn_start" and self.answer_turn_start:
                    await conn.send({"type": "turn_started", "turn_id": len(conn.json_frames("turn_start"))})
        except Exception:
            pass
        finally:
            conn.closed.set()


async def wait_for(predicate, timeout: float = 3.0, step: float = 0.01):
    """Poll ``predicate`` until it returns a truthy value."""
    async with asyncio.timeout(timeout):
        while not (result := predicate()):
            await asyncio.sleep(step)
    return result
