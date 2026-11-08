"""One page's voice session: a VoiceManager duplex connection for one page.

The page sends 16 kHz mono 16-bit PCM as binary frames and JSON control
frames as text. The session forwards the mic audio to VoiceManager,
asks for the Tyger turn engine before the first audio frame, and falls
back to the classic end-of-turn rule when the server refuses it.

An attached session serves a UI client that shows a Hermes session itself:
the client submits each utterance to the chat and sends the reply's text
back as ``say`` frames, ending it with ``say_end``. Each frame names its
reply with a ``reply`` key (the client's id for that reply), so text of a reply
that was ended or cut off never reaches a later one, and a reply the client
starts on its own (an answer to a typed message) is spoken too.

Some say frames are already on the way when the session cuts a reply whose key it
never learned. So each ``utterance`` and ``clear`` frame to an attached client
starts a new epoch and names it, the client puts the last epoch it got into each
``say`` and ``say_end``, and the session drops the frames of an older epoch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from . import vm_client
from .turns import TurnTracker, heard_words
from .vm_client import TurnEngineError, VoiceManagerClient

logger = logging.getLogger(__name__)

TURN_ENGINE = "tyger"
# How long the reply must be quiet after its Hermes turn ended before its TTS request is
# closed. Each stream hook runs on its own thread, so the turn's end can overtake the
# last text deltas by a few milliseconds.
REPLY_QUIET_S = 0.3
TURN_START_TIMEOUT_S = 5.0
CLOSED_KEYS_KEPT = 64  # reply keys of ended or cut-off replies an attached session remembers
_LAST_SPACE = re.compile(r".*\s", re.S)  # everything up to and including the last whitespace


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
                 on_utterance: Optional[Callable[[str, str], Awaitable[None]]] = None,
                 attached: bool = False) -> None:
        self.page = page
        self.client = client
        self.client_id = client_id
        self._on_utterance = on_utterance
        self.attached = attached  # the reply's text comes from the client's say frames
        self.reply_key: Optional[str] = None  # attached: the client's key for the running reply
        self._closed_keys: dict[str, None] = {}  # attached: keys that take no more text, oldest first
        self.epoch = 0  # goes up with each utterance and each clear; attached frames name it
        self.tracker: Optional[TurnTracker] = None
        # The running reply. reply_id is the message id of the utterance it answers;
        # the stream hooks name it, so text of an older, cancelled turn never lands here.
        self.reply_id: Optional[str] = None
        self.reply_open = False  # the reply still takes text
        self.reply_request: Optional[vm_client.TTSRequest] = None
        self._last_text_at = 0.0  # loop time of the reply's last text or of its turn's end
        self._unsent = ""  # reply text after the last whitespace, not yet sent to TTS
        self._finishing: Optional[asyncio.Task] = None
        self._flushing: Optional[asyncio.Task] = None  # sends the held text after a model response
        self._dropped: set[str] = set()  # request ids whose audio no longer goes to the page
        self._cleared_turn = False  # classic: the speaker was cleared for this turn's words
        self._turn_ended_at: Optional[float] = None  # loop time of the last dispatch, for the log
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
                self.tracker.open_turn(await self.duplex.turn_start(timeout=TURN_START_TIMEOUT_S))
            await self.send_page({"type": "ready", "turn_engine": self.turn_engine})
            self._start_tasks()
            await self._read_page()
        except Exception as exc:
            await self._fail(exc)
        finally:
            await self._stop()

    async def _fail(self, exc: BaseException) -> None:
        """The session cannot go on: say why on the page and close it (the page reconnects)."""
        logger.warning("voice page %s: session stopped: %r", self.client_id, exc, exc_info=exc)
        await self.send_page({"type": "error", "message": f"voice session stopped: {exc}"})
        with contextlib.suppress(Exception):
            await self.page.close()

    async def _opt_in(self) -> None:
        try:
            self.turn_engine = await self.duplex.enable_turn_engine(TURN_ENGINE)
        except TurnEngineError as exc:
            self.turn_engine_refusal = exc.error_type or "no answer"
            logger.info("voice page %s: turn engine refused (%s); classic end of turn",
                        self.client_id, self.turn_engine_refusal)

    def _start_tasks(self) -> None:
        """Start the readers of VoiceManager's streams."""
        self._spawn(self._read_transcripts)
        self._spawn(self._read_turn_ends)
        self._spawn(self._read_errors)
        self._spawn(self._read_audio)

    async def _read_transcripts(self) -> None:
        async for frame in self.duplex.transcripts():
            await self.tracker.on_transcript(frame)
            if (not self.turn_engine and not self._cleared_turn and not self.tracker.reply_active
                    and heard_words(self.tracker.text)):
                # Classic mode: the user speaks while the last reply's tail may still play.
                self._cleared_turn = True
                await self._send_clear(None)
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
                self.tracker.open_turn(await self.duplex.turn_start(timeout=TURN_START_TIMEOUT_S))
            else:
                logger.info("voice page %s: VoiceManager error frame: %s", self.client_id, frame)

    async def _next_turn(self) -> None:
        """The reply is over: in classic mode open the next listening turn."""
        self.tracker.reply_ended()
        await self._send_turn_done()
        if not self.turn_engine:
            self._cleared_turn = False
            self.tracker.open_turn(await self.duplex.turn_start(timeout=TURN_START_TIMEOUT_S))

    # --- the reply's text, from the stream hooks -----------------------------------

    def _takes(self, reply_id: str) -> bool:
        """Whether hook text for ``reply_id`` belongs to the running reply.

        An empty id (a turn context without a message id) falls back to the
        running reply."""
        return self.reply_open and (not reply_id or reply_id == self.reply_id)

    async def _speak(self, text: str) -> None:
        """Show ``text`` on the page now; send it to TTS in whole words.

        Deltas are model tokens, so a word or a sentence's final period can arrive
        in a later delta than the rest. VoiceManager's clause mode treats each chunk
        as whole words (it strips each chunk and joins them with spaces), so a chunk
        that ended inside a word would be spoken split, and a period sent alone is
        synthesised on its own. Text up to the last whitespace goes now; the rest
        waits for more text or for the end of the reply."""
        self._last_text_at = asyncio.get_running_loop().time()
        self.tracker.reply_text()
        await self.send_page({"type": "reply_delta", "text": text})
        self._unsent += text
        match = _LAST_SPACE.search(self._unsent)
        if match:
            chunk, self._unsent = self._unsent[:match.end()], self._unsent[match.end():]
            await self._send_tts(chunk)

    async def _send_tts(self, chunk: str) -> None:
        if not chunk.strip():
            return
        if self.reply_request is None:
            self.reply_request = self.duplex.tts_request(chunking="clause")
        await self.reply_request.send(chunk)

    async def on_reply_delta(self, reply_id: str, turn_id: str, iteration: int, delta: str) -> None:
        if self._takes(reply_id):
            await self._speak(delta)

    async def on_reasoning_delta(self, reply_id: str, text: str) -> None:
        """Reasoning of the running reply's turn: shown on the page, never spoken."""
        if not self.attached and self._takes(reply_id):
            await self.send_page({"type": "reasoning_delta", "text": text})

    async def on_tool_start(self, reply_id: str, call_id: str, name: str, summary: str) -> None:
        """A tool call of the running reply's turn started: show it on the page."""
        if not self.attached and self._takes(reply_id):
            await self.send_page({"type": "tool_start", "id": call_id, "name": name, "summary": summary})

    async def on_tool_end(self, reply_id: str, call_id: str, status: str, duration_ms: int) -> None:
        if not self.attached and self._takes(reply_id):
            await self.send_page({"type": "tool_end", "id": call_id, "status": status,
                                  "duration_ms": duration_ms})

    async def on_stream_end(self, reply_id: str) -> None:
        """A model response ended. If the turn goes on with tool calls, no more text comes
        for a while: once the reply's text has gone quiet, send what waits for a space so
        the response's last word is spoken now. The stream end can overtake that response's
        last delta, hence the quiet window instead of sending at once."""
        if not self._takes(reply_id) or self._flushing is not None:
            return
        self._flushing = asyncio.create_task(self._guarded(self._flush_when_quiet(reply_id)))

    async def _flush_when_quiet(self, reply_id: str) -> None:
        loop = asyncio.get_running_loop()
        while (wait := self._last_text_at + REPLY_QUIET_S - loop.time()) > 0:
            await asyncio.sleep(wait)
        if self._flushing is asyncio.current_task():
            self._flushing = None
        if not self._takes(reply_id):
            return
        rest, self._unsent = self._unsent, ""
        await self._send_tts(rest)

    async def on_turn_done(self, reply_id: str, turn_id: str, interrupted: bool) -> None:
        """The Hermes turn finished: once its text has gone quiet, end the reply's TTS
        request so VoiceManager speaks the rest."""
        if not self._takes(reply_id) or self._finishing is not None:
            return
        self._last_text_at = asyncio.get_running_loop().time()
        self._finishing = asyncio.create_task(self._guarded(self._finish_when_quiet(reply_id)))

    async def _finish_when_quiet(self, reply_id: str) -> None:
        loop = asyncio.get_running_loop()
        while (wait := self._last_text_at + REPLY_QUIET_S - loop.time()) > 0:
            await asyncio.sleep(wait)
        if self._finishing is asyncio.current_task():
            self._finishing = None
        await self._end_reply(reply_id)

    async def _end_reply(self, reply_id: str) -> None:
        """Send the rest of the reply's text and end its TTS request."""
        if not self._takes(reply_id):  # cancelled, or a newer utterance took over
            return
        self.reply_open = False
        rest, self._unsent = self._unsent, ""
        await self._send_tts(rest)
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
                if self._turn_ended_at is not None and self.reply_request is not None \
                        and event[2] == self.reply_request.request_id:
                    logger.info("voice page %s: reply audio %.0f ms after the end of turn",
                                self.client_id, (asyncio.get_running_loop().time() - self._turn_ended_at) * 1000)
                    self._turn_ended_at = None
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
        if request is None or rid != request.request_id or request.cancelled:
            return
        self.reply_request = None
        if not request.done_sent:
            # VoiceManager ended the request on its own: it got no text for its flush
            # timeout while the turn ran tools. The rest of the reply's text goes into a
            # new request, and the end of the turn sees that none is open.
            return
        await self._next_turn()

    async def _on_barge(self, frame: dict) -> None:
        """The turn engine heard the user cut in; the server already stopped the reply."""
        request = self.reply_request
        if request is not None and frame.get("request_id") == request.request_id:
            await self.cancel_reply("barge", send_cancel=False)
            self.tracker.reply_ended()
        else:  # the reply's text was done but its tail was still playing
            await self._send_clear(frame.get("request_id"))

    async def _dispatch(self, text: str) -> None:
        self._turn_ended_at = asyncio.get_running_loop().time()
        self._open_reply()
        await self.send_page({"type": "utterance", "text": text, **self._next_epoch()})
        if self._on_utterance is not None:
            await self._on_utterance(text, self.reply_id)

    def _next_epoch(self) -> dict[str, Any]:
        """Start a new epoch: say frames the client sent before it gets this one are
        dropped. Returns the field that names it on a frame to an attached client."""
        self.epoch += 1
        return {"epoch": self.epoch} if self.attached else {}

    async def _send_clear(self, request_id: Optional[str]) -> None:
        """Stop playback on the page; an attached client cuts its reply in flight."""
        await self.send_page({"type": "clear", "request_id": request_id, **self._next_epoch()})

    def _open_reply(self) -> None:
        """Start a new reply that takes text; the one before it takes no more."""
        self._cancel_finishing()
        self._retire(self.reply_key)
        self.reply_id = uuid.uuid4().hex
        self.reply_open, self.reply_request, self._unsent = True, None, ""

    def _retire(self, key: Optional[str]) -> None:
        """Remember that the reply ``key`` takes no more text."""
        if key is None:
            return
        if key == self.reply_key:
            self.reply_key = None
        self._closed_keys.pop(key, None)
        self._closed_keys[key] = None
        while len(self._closed_keys) > CLOSED_KEYS_KEPT:
            del self._closed_keys[next(iter(self._closed_keys))]

    def _cancel_finishing(self) -> None:
        if self._finishing is not None:
            self._finishing.cancel()
            self._finishing = None
        if self._flushing is not None:
            self._flushing.cancel()
            self._flushing = None

    async def cancel_reply(self, reason: str, send_cancel: bool = True) -> None:
        """Stop the running reply: no more of its text, no more of its audio on the page.

        The Hermes turn itself stops when the user's next utterance arrives: the
        gateway interrupts a running turn for a new voice message (its default
        busy_input_mode)."""
        logger.info("voice page %s: reply cancelled (%s)", self.client_id, reason)
        self._cancel_finishing()
        self.reply_open = False
        self._retire(self.reply_key)
        request, self.reply_request = self.reply_request, None
        if request is not None:
            self._dropped.add(request.request_id)
            if send_cancel and not request.cancelled:
                with contextlib.suppress(Exception):
                    await request.cancel()
        await self._send_clear(request.request_id if request else None)
        await self._send_turn_done()

    async def _send_turn_done(self) -> None:
        """Tell the page that nothing more of the turn is coming, so it stops showing
        Hermes at work. An attached client shows the chat's own busy state."""
        if not self.attached:
            await self.send_page({"type": "turn_done"})

    def _spawn(self, reader: Callable[[], Awaitable[None]]) -> None:
        # The reader's coroutine is made inside the task, so a task cancelled before it
        # starts leaves no coroutine behind that was never awaited.
        self._tasks.append(asyncio.create_task(self._supervised(reader)))

    async def _guarded(self, coro: Awaitable[None]) -> None:
        """Run a one-shot task; if it fails, the session cannot go on."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(exc)

    async def _supervised(self, reader: Callable[[], Awaitable[None]]) -> None:
        """Run one reader. A reader that fails, or ends because VoiceManager closed the
        connection, leaves the session deaf or mute, so it ends the session."""
        try:
            await reader()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(exc)
        else:
            await self._fail(ConnectionError("VoiceManager closed the connection"))

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
        """A JSON control frame from the page: playback edges of a reply, and for an
        attached client the reply's text."""
        if self.attached and frame.get("type") in ("say", "say_end"):
            await self._on_say(frame)
            return
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

    async def _on_say(self, frame: dict[str, Any]) -> None:
        """Reply text from an attached client.

        The frames of one WebSocket arrive in order, so ``say_end`` ends the reply at
        once, with no quiet window. A frame of an older epoch was sent before the client
        heard of the last utterance or clear: the reply it belongs to is over."""
        epoch = frame.get("epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch < self.epoch:
            return
        key = frame.get("reply")
        key = key if isinstance(key, str) and key else None
        if frame["type"] == "say_end":
            await self._on_say_end(key)
            return
        text = frame.get("text")
        if isinstance(text, str) and text and await self._say_takes(key):
            await self._speak(text)

    async def _say_takes(self, key: Optional[str]) -> bool:
        """Whether say text with ``key`` goes to the running reply, starting one for a
        new key. Text without a key belongs to the reply the last utterance opened."""
        if key is None:
            return self.reply_open and self.reply_key is None
        if key in self._closed_keys:
            return False  # ended or cut off
        if self.reply_open and self.reply_key in (None, key):
            self.reply_key = key  # the reply the utterance opened gets its key
            return True
        if self.reply_open:  # the client moved on to another reply: end this one
            await self._on_say_end(self.reply_key)
        self._open_reply()  # a reply the client started itself, as for a typed message
        self.reply_key = key
        self.tracker.reply_started()
        return True

    async def _on_say_end(self, key: Optional[str]) -> None:
        """End the reply ``key`` names, if it is the running one; it takes no more text."""
        if key is None:
            if self.reply_key is None:
                await self._end_reply(self.reply_id or "")
            return
        if self.reply_open and self.reply_key is None and key not in self._closed_keys:
            # The reply the utterance opened ends with no say text: it was all screen text.
            self.reply_key = key
        running = key == self.reply_key
        self._retire(key)
        if running:
            await self._end_reply(self.reply_id or "")

    async def _stop(self) -> None:
        self._cancel_finishing()
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

