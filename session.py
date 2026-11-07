"""One page's voice session: a VoiceManager duplex connection for one page.

The page sends 16 kHz mono 16-bit PCM as binary frames and JSON control
frames as text. The session forwards the mic audio to VoiceManager,
asks for the Tyger turn engine before the first audio frame, and falls
back to the classic end-of-turn rule when the server refuses it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from . import vm_client
from .turns import TurnTracker
from .vm_client import TurnEngineError, VoiceManagerClient

logger = logging.getLogger(__name__)

TURN_ENGINE = "tyger"


def client_from_url(url: str, token: str = "") -> VoiceManagerClient:
    """A VoiceManagerClient for ``http(s)://host:port`` or ``ws(s)://host:port``."""
    parts = urlsplit(url if "://" in url else f"http://{url}")
    secure = parts.scheme in ("https", "wss")
    port = parts.port or (443 if secure else 8255)
    return VoiceManagerClient(host=parts.hostname or "localhost", port=port, token=token,
                              use_ssl=secure)


class VoiceSession:
    """Relays one page to one VoiceManager connection until either side closes."""

    def __init__(self, page, client: VoiceManagerClient, client_id: str = "phone",
                 on_utterance: Optional[Callable[[str], Awaitable[None]]] = None) -> None:
        self.page = page
        self.client = client
        self.client_id = client_id
        self._on_utterance = on_utterance
        self.tracker: Optional[TurnTracker] = None
        self.duplex: Optional[vm_client.DuplexSession] = None
        self.turn_engine: Optional[str] = None
        self.turn_engine_refusal: Optional[str] = None
        self._tasks: list[asyncio.Task] = []

    async def send_page(self, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await self.page.send(json.dumps(payload))

    async def run(self) -> None:
        try:
            await self.client._connect()
        except Exception as exc:
            logger.warning("voice page %s: VoiceManager unreachable: %s", self.client_id, exc)
            await self.send_page({"type": "error", "message": f"VoiceManager unreachable: {exc}"})
            return
        try:
            self.duplex = self.client.duplex()
            await self._opt_in()
            self.tracker = TurnTracker(self._dispatch, self.cancel_reply, self.turn_engine)
            if not self.turn_engine:
                self.tracker.open_turn(await self.duplex.turn_start())
            await self.send_page({"type": "ready", "turn_engine": self.turn_engine})
            self._start_tasks()
            await self._read_page()
        finally:
            await self._stop()

    async def _opt_in(self) -> None:
        try:
            self.turn_engine = await self.duplex.enable_turn_engine(TURN_ENGINE)
        except TurnEngineError as exc:
            self.turn_engine_refusal = exc.error_type or "no answer"
            logger.info("voice page %s: turn engine refused (%s); classic end of turn",
                        self.client_id, self.turn_engine_refusal)

    def _start_tasks(self) -> None:
        """Start the readers of VoiceManager's streams."""
        self._spawn(self._read_transcripts())
        self._spawn(self._read_turn_ends())
        self._spawn(self._read_errors())

    async def _read_transcripts(self) -> None:
        async for frame in self.duplex.transcripts():
            await self.tracker.on_transcript(frame)
            await self.send_page({"type": "transcript", "text": self.tracker.text,
                                  "final": bool(frame.get("is_final"))})

    async def _read_turn_ends(self) -> None:
        async for frame in self.duplex.turn_ends():
            await self.tracker.on_turn_end(frame)

    async def _read_errors(self) -> None:
        async for frame in self.duplex.errors():
            if frame.get("error_type") == "turn_engine_lost":
                self.turn_engine = None
                self.tracker.turn_engine_lost()
                self.tracker.open_turn(await self.duplex.turn_start())
            else:
                logger.info("voice page %s: VoiceManager error frame: %s", self.client_id, frame)

    async def _dispatch(self, text: str) -> None:
        await self.send_page({"type": "utterance", "text": text})
        if self._on_utterance is not None:
            await self._on_utterance(text)

    async def cancel_reply(self, reason: str) -> None:
        """Stop the running reply."""
        logger.info("voice page %s: reply cancelled (%s)", self.client_id, reason)

    def _spawn(self, coro: Awaitable[None]) -> None:
        self._tasks.append(asyncio.create_task(coro))

    async def _read_page(self) -> None:
        async for message in self.page:
            if isinstance(message, bytes):
                if message:
                    await self.duplex.send_audio(message)
                continue
            try:
                frame = json.loads(message)
            except ValueError:
                continue
            if isinstance(frame, dict):
                await self.on_page_frame(frame)

    async def on_page_frame(self, frame: dict[str, Any]) -> None:
        """A JSON control frame from the page."""

    async def _stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()
        if self.duplex is not None:
            await self.duplex.close()
        with contextlib.suppress(Exception):
            await self.client._disconnect()

