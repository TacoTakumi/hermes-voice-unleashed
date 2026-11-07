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
import uuid
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from . import vm_client
from .turns import TurnTracker, heard_words
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
                 on_utterance: Optional[Callable[[str, str], Awaitable[None]]] = None) -> None:
        self.page = page
        self.client = client
        self.client_id = client_id
        self._on_utterance = on_utterance
        self.tracker: Optional[TurnTracker] = None
        # The running reply. reply_id is the message id of the utterance it answers;
        # the stream hooks name it, so text of an older, cancelled turn never lands here.
        self.reply_id: Optional[str] = None
        self.reply_open = False  # the reply still takes text
        self.reply_request: Optional[vm_client.TTSRequest] = None
        self._streamed: dict[tuple[str, int], str] = {}  # (turn, iteration) -> text sent
        self._closed_iterations: set[tuple[str, int]] = set()
        self._dropped: set[str] = set()  # request ids whose audio no longer goes to the page
        self._cleared_turn = False  # classic: the speaker was cleared for this turn's words
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
        self._spawn(self._read_audio())

    async def _read_transcripts(self) -> None:
        async for frame in self.duplex.transcripts():
            await self.tracker.on_transcript(frame)
            if (not self.turn_engine and not self._cleared_turn and not self.tracker.reply_active
                    and heard_words(self.tracker.text)):
                # Classic mode: the user speaks while the last reply's tail may still play.
                self._cleared_turn = True
                await self.send_page({"type": "clear", "request_id": None})
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

    async def _next_turn(self) -> None:
        """The reply is over: in classic mode open the next listening turn."""
        self.tracker.reply_ended()
        if not self.turn_engine:
            self._cleared_turn = False
            self.tracker.open_turn(await self.duplex.turn_start())

    # --- the reply's text, from the stream hooks -----------------------------------

    def _takes(self, reply_id: str) -> bool:
        """Whether hook text for ``reply_id`` belongs to the running reply.

        An empty id (a turn context without a message id) falls back to the
        running reply."""
        return self.reply_open and (not reply_id or reply_id == self.reply_id)

    async def _speak(self, turn_id: str, iteration: int, text: str) -> None:
        if self.reply_request is None:
            self.reply_request = self.duplex.tts_request(chunking="clause")
        key = (turn_id, iteration)
        self._streamed[key] = self._streamed.get(key, "") + text
        self.tracker.reply_text()
        await self.reply_request.send(text)
        await self.send_page({"type": "reply_delta", "text": text})

    async def on_reply_delta(self, reply_id: str, turn_id: str, iteration: int, delta: str) -> None:
        if (turn_id, iteration) in self._closed_iterations or not self._takes(reply_id):
            return
        await self._speak(turn_id, iteration, delta)

    async def on_reply_stream_end(self, reply_id: str, turn_id: str, iteration: int,
                                  final_text: str) -> None:
        """One model call's stream ended: speak any text its deltas have not brought yet."""
        key = (turn_id, iteration)
        if key not in self._closed_iterations and self._takes(reply_id):
            streamed = self._streamed.get(key, "")
            if final_text.startswith(streamed) and len(final_text) > len(streamed):
                await self._speak(turn_id, iteration, final_text[len(streamed):])
        self._closed_iterations.add(key)

    async def on_turn_done(self, reply_id: str, turn_id: str, interrupted: bool) -> None:
        """The Hermes turn finished: end the reply's text so VoiceManager speaks the rest."""
        if not self._takes(reply_id):
            return
        self.reply_open = False
        request = self.reply_request
        if request is None:  # the turn said nothing
            await self._next_turn()
        elif not request.done_sent:
            await request.done()

    # --- VoiceManager's audio, to the page -------------------------------------------

    async def _read_audio(self) -> None:
        async for event in self.duplex.audio():
            kind = event[0]
            if kind == "pcm":
                if event[2] not in self._dropped:
                    with contextlib.suppress(Exception):
                        await self.page.send(event[1])
            elif kind == "audio_start":
                frame = event[1]
                await self.send_page({"type": "audio_start", "request_id": frame.get("request_id"),
                                      "sample_rate": frame.get("sample_rate")})
            elif kind == "audio_end":
                await self._on_audio_end(event[1])
            elif kind == "barge":
                await self._on_barge(event[1])

    async def _on_audio_end(self, frame: dict) -> None:
        rid = frame.get("request_id")
        self._dropped.discard(rid)
        await self.send_page({"type": "audio_end", "request_id": rid, "reason": frame.get("reason")})
        request = self.reply_request
        if request is None or rid != request.request_id or not request.done_sent or request.cancelled:
            return
        self.reply_request = None
        await self._next_turn()

    async def _on_barge(self, frame: dict) -> None:
        """The turn engine heard the user cut in; the server already stopped the reply."""
        request = self.reply_request
        if request is not None and frame.get("request_id") == request.request_id:
            await self.cancel_reply("barge", send_cancel=False)
            self.tracker.reply_ended()
        else:  # the reply's text was done but its tail was still playing
            await self.send_page({"type": "clear", "request_id": frame.get("request_id")})

    async def _dispatch(self, text: str) -> None:
        self.reply_id = uuid.uuid4().hex
        self.reply_open, self.reply_request = True, None
        await self.send_page({"type": "utterance", "text": text})
        if self._on_utterance is not None:
            await self._on_utterance(text, self.reply_id)

    async def cancel_reply(self, reason: str, send_cancel: bool = True) -> None:
        """Stop the running reply: no more of its text, no more of its audio on the page.

        The Hermes turn itself stops when the user's next utterance arrives: the
        gateway interrupts a running turn for a new voice message (its default
        busy_input_mode)."""
        logger.info("voice page %s: reply cancelled (%s)", self.client_id, reason)
        self.reply_open = False
        request, self.reply_request = self.reply_request, None
        if request is not None:
            self._dropped.add(request.request_id)
            if send_cancel and not request.cancelled:
                with contextlib.suppress(Exception):
                    await request.cancel()
        await self.send_page({"type": "clear", "request_id": request.request_id if request else None})

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
        """A JSON control frame from the page: playback edges of a reply."""
        if frame.get("type") != "playback" or not self.turn_engine:
            return
        request_id = frame.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        if frame.get("event") == "start":
            await self.duplex.playback_start(request_id)
        elif frame.get("event") == "stop":
            reply_sample = frame.get("reply_sample")
            if isinstance(reply_sample, int) and not isinstance(reply_sample, bool) and reply_sample >= 0:
                await self.duplex.playback_stop(request_id, reply_sample=reply_sample)

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

