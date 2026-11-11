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
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed

from . import vm_client
from .turns import END_SESSION, NEW_SESSION, TurnTracker, heard_words, spoken_approval, spoken_command
from .vm_client import TurnEngineError, VoiceManagerClient

logger = logging.getLogger(__name__)

TURN_ENGINE = "tyger"
# While an approval waits, an utterance of at most this many words that is no answer is
# not sent to Hermes: the user is asked again with the notice below.
UNCLEAR_ANSWER_MAX_WORDS = 3
APPROVAL_REPEAT_NOTICE = "Say yes or no."
# The bare word the gateway takes for each choice of an approval prompt.
APPROVAL_CHOICE_WORDS = {"once": "yes", "session": "session", "always": "always", "deny": "no"}
# How long the reply must be quiet after its Hermes turn ended before its TTS request is
# closed. Each stream hook runs on its own thread, so the turn's end can overtake the
# last text deltas by a few milliseconds.
REPLY_QUIET_S = 0.3
TURN_START_TIMEOUT_S = 5.0
# How long the session waits for VoiceManager to finish playing the "Session ended"
# notice before it closes the page anyway.
SESSION_END_TIMEOUT_S = 10.0
# Both end with a period: VoiceManager adds a comma to a text that ends on a letter,
# and the notice would be spoken as a sentence that goes on.
SESSION_ENDED_NOTICE = "Session ended."
SESSION_NEW_NOTICE = "New session."
CLOSED_KEYS_KEPT = 64  # reply keys of ended or cut-off replies an attached session remembers
# Ends the session expects: the page's socket closed (ConnectionClosed, the replaced
# close code included) or VoiceManager dropped the connection. They are logged in one
# line at info; any other end is a failure with a traceback.
EXPECTED_ENDS = (ConnectionClosed, ConnectionError, vm_client.ConnectionError)
_LAST_SPACE = re.compile(r".*\s", re.S)  # everything up to and including the last whitespace
# A Desktop voice chat's replies start with a spoken summary in a say block, and a
# model keeps writing them once the session's history holds some.
SAY_OPEN = "<say>"
SAY_CLOSE = "</say>"


def strip_say_tags(text: str) -> str:
    """``text`` without say tags; what was inside them stays."""
    return text.replace(SAY_OPEN, "").replace(SAY_CLOSE, "")


def _partial_tag_length(text: str, tag: str) -> int:
    """The length of the longest tail of ``text`` that is the start of ``tag``."""
    for n in range(min(len(text), len(tag) - 1), 0, -1):
        if text.endswith(tag[:n]):
            return n
    return 0


class SayCutter:
    """Splits one reply's streamed text into what the page shows and what TTS speaks.

    A reply that starts, after optional whitespace, with ``<say>`` is spoken by that
    first block alone: the text after its closing tag is shown but not spoken. Any
    other reply is spoken whole. Tags can be split across deltas, so a tail that could
    still become a tag is held back and released as soon as it cannot be one; say tags
    anywhere are dropped from the shown text. One cutter per reply."""

    def __init__(self) -> None:
        # start: only whitespace or the start of an opening tag so far; inside: in the
        # leading say block; after: past it, shown only; plain: no leading block.
        self._stage = "start"
        self._held = ""

    def push(self, delta: str) -> tuple[str, str]:
        """Takes a delta; returns the text to show now and the text to speak now."""
        self._held += delta
        shown, spoken = [], []
        while self._held:
            if self._stage == "start":
                text = self._held.lstrip()
                if not text or SAY_OPEN.startswith(text):
                    break  # it could still be a leading tag
                if not text.startswith(SAY_OPEN):
                    self._stage = "plain"
                    continue
                shown.append(self._held[:len(self._held) - len(text)])
                self._held = text[len(SAY_OPEN):]
                self._stage = "inside"
            elif self._stage == "inside":
                at = self._held.find(SAY_CLOSE)
                if at >= 0:
                    text, self._held = self._held[:at], self._held[at + len(SAY_CLOSE):]
                    self._stage = "after"
                else:
                    keep = _partial_tag_length(self._held, SAY_CLOSE)
                    text, self._held = self._held[:len(self._held) - keep], self._held[len(self._held) - keep:]
                    shown.append(text)
                    spoken.append(text)
                    break
                shown.append(text)
                spoken.append(text)
            else:
                keep = max(_partial_tag_length(self._held, SAY_OPEN),
                           _partial_tag_length(self._held, SAY_CLOSE))
                text = strip_say_tags(self._held[:len(self._held) - keep])
                self._held = self._held[len(self._held) - keep:]
                shown.append(text)
                if self._stage == "plain":
                    spoken.append(text)
                break
        return "".join(shown), "".join(spoken)

    def end(self) -> tuple[str, str]:
        """The reply is complete: returns the held text to show and to speak. A tail that
        is the start of a tag can no longer become one, unless it closes the say block."""
        held, self._held = self._held, ""
        if self._stage == "inside":
            return "", ""  # at most the start of the closing tag, never shown or spoken
        return held, "" if self._stage == "after" else held


def client_from_url(url: str, token: str = "") -> VoiceManagerClient:
    """A VoiceManagerClient for ``http(s)://host:port`` or ``ws(s)://host:port``."""
    parts = urlsplit(url if "://" in url else f"http://{url}")
    secure = parts.scheme in ("https", "wss")
    port = parts.port or (443 if secure else 8255)
    return VoiceManagerClient(host=parts.hostname or "localhost", port=port, token=token,
                              use_ssl=secure)


class VoiceSession:
    """Relays one page to one VoiceManager connection until either side closes."""

    def __init__(self, page, client: VoiceManagerClient, client_id: str = "voice",
                 on_utterance: Optional[Callable[[str, str], Awaitable[None]]] = None,
                 attached: bool = False,
                 approval_words: Optional[Callable[[], Iterable[str]]] = None,
                 approval_pending: Optional[Callable[[], Awaitable[bool]]] = None,
                 approval_oldest: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
                 clarify_pending: Optional[Callable[[], Awaitable[bool]]] = None,
                 clarify_answer: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
                 on_new_session: Optional[Callable[[], Awaitable[None]]] = None) -> None:
        self.page = page
        self.client = client
        self.client_id = client_id
        self._on_utterance = on_utterance
        # Called once the "New session" notice went out: the adapter moves the lane
        # onto a fresh Hermes session and tells the page. Without one the session
        # sends the page a bare session_new frame itself.
        self._on_new_session = on_new_session
        # Spoken approvals: the lower-case words that answer an approval prompt, and
        # whether the chat waits for one right now.
        self._approval_words = approval_words
        self._approval_pending = approval_pending
        # The request id of the oldest approval the chat waits for, the one the
        # gateway gives the next answer to; None when it names none.
        self._approval_oldest = approval_oldest
        # Whether the chat waits for the answer to a question the model asked.
        self._clarify_pending = clarify_pending
        # The form in which a spoken answer to that question goes to the gateway, when
        # it is not the text as heard (a spoken number, a choice's name); else None.
        self._clarify_answer = clarify_answer
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
        self._cutter = SayCutter()  # page path: decides what of the reply's text is spoken
        self._finishing: Optional[asyncio.Task] = None
        self._flushing: Optional[asyncio.Task] = None  # sends the held text after a model response
        self._dropped: set[str] = set()  # request ids whose audio no longer goes to the page
        self._audio_bytes: dict[str, int] = {}  # audio received per TTS request, until its audio end
        self._cleared_turn = False  # classic: the speaker was cleared for this turn's words
        self._turn_ended_at: Optional[float] = None  # loop time of the last dispatch, for the log
        # The TTS requests of notices spoken outside a reply, by request id, until
        # their audio ends: such an audio end is no reply's end. The value is the event
        # to set at that end, for a command that waits for its own notice.
        self._notice_requests: dict[str, Optional[asyncio.Event]] = {}
        # The id of the reply that ran when the last approval prompt was spoken, in a
        # one-item tuple (the id is None on a page that never saw the utterance); None
        # when no prompt waits for its word.
        self._prompt_reply: Optional[tuple[Optional[str]]] = None
        # The request id of the approval prompt the page shows; None when it shows none.
        self._approval_id: Optional[str] = None
        # The prompts that came and were not answered here, by request id: the spoken
        # sentence and the page frame. The page shows the one the gateway holds as its
        # oldest; the map empties when the chat waits for no approval.
        self._approval_prompts: dict[str, tuple[str, dict[str, Any]]] = {}
        # The TTS request of the last spoken prompt while its audio may still play.
        self._prompt_request: Optional[vm_client.TTSRequest] = None
        self._turn_started = False  # a hook event of the open reply's turn was seen
        self.duplex: Optional[vm_client.DuplexSession] = None
        self.turn_engine: Optional[str] = None
        self.turn_engine_refusal: Optional[str] = None
        self._tasks: list[asyncio.Task] = []
        self._failed = False  # the end was logged and the page told; later ends are silent

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
        """The session cannot go on: say why on the page and close it (the page reconnects).

        Every reader ends when VoiceManager drops the connection, so only the first
        end does the work; the ones that follow return. A page that closed its own
        socket gets no error frame: nobody is there to read it."""
        if self._failed:
            return
        self._failed = True
        if isinstance(exc, EXPECTED_ENDS):
            logger.info("voice page %s: session stopped: %s", self.client_id, exc)
        else:
            logger.warning("voice page %s: session stopped: %r", self.client_id, exc, exc_info=exc)
        if not isinstance(exc, ConnectionClosed):
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
                if self.attached and self.reply_open:
                    await self._close_reply("talk-over")  # the client cuts its reply on the clear
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
        if not self.turn_engine:
            # The answered segments leave the text before anything is awaited: a frame
            # that arrives while the turn_start ack is on its way must not dispatch
            # them again with the new words.
            self._cleared_turn = False
            self.tracker.turn_over()
        await self._send_turn_done()
        if not self.turn_engine:
            self.tracker.open_turn(await self.duplex.turn_start(timeout=TURN_START_TIMEOUT_S))

    # --- the reply's text, from the stream hooks -----------------------------------

    @property
    def turn_running(self) -> bool:
        """Whether the open reply's Hermes turn runs: it started (a hook event of it
        was seen) and the reply is not over. Such a turn ends with a turn_done frame."""
        return self.reply_open and self._turn_started

    def _in_turn(self, reply_id: str) -> bool:
        """Whether a hook event for ``reply_id`` belongs to the running reply; if so,
        the reply's turn has started."""
        if not self._takes(reply_id):
            return False
        self._turn_started = True
        return True

    async def on_turn_start(self, reply_id: str) -> None:
        """The turn's model call is about to start: nothing to show, the turn runs."""
        await self._approval_sync()
        self._in_turn(reply_id)

    def turn_failed(self) -> None:
        """The open reply's turn raised: it runs no more, and no end-of-turn hook comes."""
        self._turn_started = False

    def _takes(self, reply_id: str) -> bool:
        """Whether hook text for ``reply_id`` belongs to the running reply.

        An empty id (a turn context without a message id) falls back to the
        running reply. A reply without an id of its own (a page that reconnected
        while a turn was blocked on an approval) takes any id."""
        return self.reply_open and (not reply_id or self.reply_id is None or reply_id == self.reply_id)

    async def _speak(self, text: str) -> None:
        """Show ``text`` on the page now; send it to TTS in whole words.

        Deltas are model tokens, so a word or a sentence's final period can arrive
        in a later delta than the rest. VoiceManager's clause mode treats each chunk
        as whole words (it strips each chunk and joins them with spaces), so a chunk
        that ended inside a word would be spoken split, and a period sent alone is
        synthesised on its own. Text up to the last whitespace goes now; the rest
        waits for more text or for the end of the reply."""
        await self._show(text, text)

    async def _show(self, shown: str, spoken: str) -> None:
        """Show ``shown`` on the page now and queue ``spoken`` for TTS in whole words."""
        self._last_text_at = asyncio.get_running_loop().time()
        self.tracker.reply_text()
        if shown:
            await self.send_page({"type": "reply_delta", "text": shown})
        self._unsent += spoken
        match = _LAST_SPACE.search(self._unsent)
        if match:
            chunk, self._unsent = self._unsent[:match.end()], self._unsent[match.end():]
            await self._send_tts(chunk)

    async def _send_tts(self, chunk: str, request: Optional[vm_client.TTSRequest] = None) -> None:
        """Send ``chunk`` to the reply's TTS request, opened here when it has none, or
        to ``request`` when the text belongs to no reply."""
        if not chunk.strip():
            return
        if request is None:
            if self.reply_request is None:
                self.reply_request = self.duplex.tts_request(chunking="clause")
            request = self.reply_request
        await request.send(chunk)

    async def on_reply_delta(self, reply_id: str, turn_id: str, iteration: int, delta: str) -> None:
        """Reply text from the stream hooks. On the page path the say cutter decides what
        of it is spoken; an attached client's text comes as say frames, already cut."""
        if not self._in_turn(reply_id):
            return
        if self.attached:
            await self._speak(delta)
        else:
            await self._show(*self._cutter.push(delta))

    async def on_reasoning_delta(self, reply_id: str, text: str) -> None:
        """Reasoning of the running reply's turn: shown on the page, never spoken."""
        if not self.attached and self._in_turn(reply_id):
            await self.send_page({"type": "reasoning_delta", "text": text})

    async def on_tool_start(self, reply_id: str, call_id: str, name: str, summary: str) -> None:
        """A tool call of the running reply's turn started: show it on the page."""
        if not self.attached and self._in_turn(reply_id):
            await self.send_page({"type": "tool_start", "id": call_id, "name": name, "summary": summary})

    async def on_tool_end(self, reply_id: str, call_id: str, status: str, duration_ms: int) -> None:
        # The tool that waited for an approval ends when the approval does, also when
        # the gateway withdrew it or it timed out, and also for a turn whose reply a
        # newer utterance replaced.
        await self._approval_sync()
        if not self.attached and self._in_turn(reply_id):
            await self.send_page({"type": "tool_end", "id": call_id, "status": status,
                                  "duration_ms": duration_ms})

    async def on_stream_end(self, reply_id: str) -> None:
        """A model response ended. If the turn goes on with tool calls, no more text comes
        for a while: once the reply's text has gone quiet, send what waits for a space so
        the response's last word is spoken now. The stream end can overtake that response's
        last delta, hence the quiet window instead of sending at once."""
        if not self._in_turn(reply_id) or self._flushing is not None:
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
        await self._approval_sync()
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
        if not self.attached:
            shown, spoken = self._cutter.end()
            if shown:
                await self.send_page({"type": "reply_delta", "text": shown})
            self._unsent += spoken
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
                self._audio_bytes[event[2]] = self._audio_bytes.get(event[2], 0) + len(event[1])
                if event[2] not in self._dropped:
                    with contextlib.suppress(Exception):
                        await self.page.send(event[1])
            elif kind == "audio_start":
                frame = event[1]
                if frame.get("request_id") in self._dropped:
                    continue  # cancelled before its audio began: the page must not adopt it
                await self.send_page({"type": "audio_start", "request_id": frame.get("request_id"),
                                      "sample_rate": frame.get("sample_rate")})
            elif kind == "audio_end":
                await self._on_audio_end(event[1])
            elif kind == "barge":
                await self._on_barge(event[1])

    async def _on_audio_end(self, frame: dict) -> None:
        rid = frame.get("request_id")
        if self._prompt_request is not None and rid == self._prompt_request.request_id:
            self._prompt_request = None
        cancelled = rid in self._dropped
        self._dropped.discard(rid)
        silent = not self._audio_bytes.pop(rid, 0)
        await self.send_page({"type": "audio_end", "request_id": rid, "reason": frame.get("reason")})
        ours = rid in self._notice_requests or (
            self.reply_request is not None and rid == self.reply_request.request_id)
        if ours and silent and not cancelled and not self.attached:
            # A request is opened only for text to speak, so an end with no audio means
            # the speech failed (the engine did not load, for one). VoiceManager ends
            # such a stream like any other, so the missing audio is the only sign.
            logger.warning("voice page %s: no audio came for TTS request %s (%s)",
                           self.client_id, rid, frame.get("reason"))
            await self.send_page({"type": "speech_failed", "request_id": rid})
        if rid in self._notice_requests:
            ended = self._notice_requests.pop(rid)
            if ended is not None:
                ended.set()
            return
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
            if self.attached and self.reply_open:
                # The client cuts the reply it has open on the clear: close it here too.
                await self._close_reply("barge")
                self.tracker.reply_ended()
            await self._send_clear(frame.get("request_id"))

    async def _dispatch(self, text: str) -> None:
        # A page's spoken command is the session's to act on; an attached client
        # gets every utterance and decides itself.
        command = spoken_command(text) if not self.attached else None
        if command == END_SESSION:
            await self._end_session()
            return
        if command == NEW_SESSION:
            await self._new_session()
            return
        self._turn_ended_at = asyncio.get_running_loop().time()
        moved = await self._approval_sync()
        answer = await self._approval_answer(text)
        if answer is not None and moved and self._approval_id is not None:
            # The approval the user answered ended just now and the page shows the
            # next one, spoken a moment ago. The answer was not for that command.
            logger.info("voice page %s: approval answer came as the prompt changed, not sent", self.client_id)
            await self.send_page({"type": "note", "text": "That approval ended. Another one waits for your answer."})
            await self._next_turn()
            return
        if answer is not None:
            await self._answered_approval()  # the page's prompt is over
            # The gateway consumes a bare approval word for the blocked turn, which then
            # resumes under its original message id. The cancel that a new utterance
            # causes closed the reply, so it opens again under that id to take the
            # resumed turn's text. The word goes out under a message id of its own.
            # The id is the one of the reply that raised the prompt: an utterance the
            # gateway did not interrupt the blocked turn for opened a newer reply since.
            # Known race: the approval can resolve (its timeout) between the check
            # above and the gateway's own check of the word. The gateway then runs the
            # word as a new turn under the word's message id, and this reopened reply
            # does not take that turn's text: it is shown by the final send but not
            # spoken. The window is a few milliseconds.
            self._reopen_reply()
            await self.send_page({"type": "utterance", "text": answer, **self._next_epoch()})
            if self._on_utterance is not None:
                await self._on_utterance(answer, uuid.uuid4().hex)
            await self._approval_sync()  # the approval behind the answered one, if it waits
            return
        if await self._unclear_approval_answer(text):
            # A short utterance that is no answer is most likely an answer the
            # recognizer got wrong ("Yet please"). Sent on, it would be a new message:
            # the gateway withdraws the approval for one, and the model may issue the
            # command again. So it stays here, the approval stays open, and the user
            # is asked again. The page shows what was heard.
            logger.info("voice page %s: approval answer not understood, asked again", self.client_id)
            await self.send_page({"type": "note", "text": f'Heard "{text.strip()}". Say yes or no, or tap a choice.'})
            if self.reply_open or self.reply_request is not None:
                await self.cancel_reply("approval answer not understood")
            self._prompt_request = await self.speak_notice(APPROVAL_REPEAT_NOTICE, show=False)
            await self._next_turn()
            return
        if await self._answers_question():
            # The model asked a question and its turn waits for the answer. The gateway
            # takes this utterance as that answer and the turn resumes under its
            # original message id, so the reply that raised the question opens again,
            # as for an approval word. The same race applies: a question that times out
            # between this check and the gateway's makes the utterance a new turn, whose
            # text is then shown but not spoken.
            # The page shows what was said; the gateway gets the answer in the form it
            # takes as a selection when the user said a choice's number or name.
            answer = await self._question_answer(text)
            self._reopen_reply()
            await self.send_page({"type": "utterance", "text": text, **self._next_epoch()})
            if self._on_utterance is not None:
                await self._on_utterance(answer, uuid.uuid4().hex)
            return
        self._open_reply()
        await self.send_page({"type": "utterance", "text": text, **self._next_epoch()})
        if self._on_utterance is not None:
            await self._on_utterance(text, self.reply_id)

    async def _approval_answer(self, text: str) -> Optional[str]:
        """The bare word to send the gateway when ``text`` answers an approval the
        chat waits for: one of the gateway's own words said alone, or the word a
        spoken approve or deny phrase stands for. None for any other utterance."""
        if self._approval_words is None or self._approval_pending is None:
            return None
        try:
            words = heard_words(text)
            if len(words) == 1 and words[0] in self._approval_words():
                answer = words[0]
            else:
                answer = spoken_approval(text)
            if answer is None:
                return None
            if await self._approval_pending():
                return answer
        except Exception:
            # The check asks the gateway; a failure there must not end the voice
            # session. The utterance goes on as ordinary text.
            logger.warning("voice page %s: approval check failed", self.client_id, exc_info=True)
            return None
        self._prompt_reply = None  # the approval was answered elsewhere or timed out
        return None

    async def _unclear_approval_answer(self, text: str) -> bool:
        """Whether ``text``, which is no approval answer, is too short to be a message
        of its own while the chat waits for an approval."""
        if self._approval_words is None or self._approval_pending is None:
            return False
        if not 1 <= len(heard_words(text)) <= UNCLEAR_ANSWER_MAX_WORDS:
            return False
        try:
            return bool(await self._approval_pending())
        except Exception:
            logger.warning("voice page %s: approval check failed", self.client_id, exc_info=True)
            return False

    async def _answers_question(self) -> bool:
        """Whether the chat waits for the answer to a question the model asked."""
        if self._clarify_pending is None:
            return False
        try:
            return bool(await self._clarify_pending())
        except Exception:
            # As for the approval check: a failure must not end the voice session.
            logger.warning("voice page %s: question check failed", self.client_id, exc_info=True)
            return False

    async def _question_answer(self, text: str) -> str:
        """The text to send the gateway for the spoken answer ``text``."""
        if self._clarify_answer is None:
            return text
        try:
            return await self._clarify_answer(text) or text
        except Exception:
            logger.warning("voice page %s: answer mapping failed", self.client_id, exc_info=True)
            return text

    async def open_approval(self, request_id: str, sentence: str, frame: dict[str, Any]) -> None:
        """An approval prompt came: the page shows it with ``frame`` and ``sentence``
        is spoken, at once when it is the oldest approval the chat waits for, else
        when the ones before it have ended.

        The page keeps a prompt until an ``approval_end`` frame with the same request
        id: the user answered, or the gateway withdrew the approval or let it time
        out. The sentence is not shown as reply text: the prompt holds the command."""
        self._approval_prompts[request_id] = (sentence, frame)
        await self._approval_sync()

    async def _approval_sync(self) -> bool:
        """Make the page show the oldest approval the chat waits for, or none.

        The gateway takes an answer, a bare word, for the oldest approval of the chat.
        So that one, and no other, is what the page may show: a card for a newer
        prompt would have its answer run the older command. Which one is oldest is
        the gateway's to say; prompts can arrive in another order, and the oldest can
        leave (a timeout, an answer elsewhere) while a newer one still waits.

        Returns whether the page's prompt closed or changed to another approval: an
        answer under way was then meant for a prompt that is gone."""
        if self._approval_pending is None or (self._approval_id is None and not self._approval_prompts):
            return False
        try:
            pending = await self._approval_pending()
            oldest = await self._approval_oldest() if pending and self._approval_oldest is not None else None
        except Exception:
            logger.warning("voice page %s: approval check failed", self.client_id, exc_info=True)
            return False
        if not pending:  # the chat waits for no approval at all
            self._approval_prompts.clear()
            return await self._end_approval()
        if oldest is None:  # the gateway names none: take the prompts in arrival order
            oldest = self._approval_id or next(iter(self._approval_prompts), None)
        if oldest == self._approval_id:
            return False
        changed = await self._end_approval()
        prompt = self._approval_prompts.get(oldest)
        if prompt is None:  # the oldest approval's prompt has not come yet
            return changed
        sentence, frame = prompt
        self._approval_id = oldest
        await self.send_page(frame)
        await self.speak_prompt(sentence, show=False)
        return True

    async def _answered_approval(self) -> None:
        """The user answered the prompt the page shows: it is over and forgotten."""
        if self._approval_id is not None:
            self._approval_prompts.pop(self._approval_id, None)
        await self._end_approval()

    async def _on_approval_answer(self, frame: dict[str, Any]) -> None:
        """The user tapped a choice of the approval prompt the page shows.

        The gateway gets the choice's bare word, as for a spoken answer, and takes it
        for the oldest approval the chat waits for. So only a tap for the prompt that
        is open now counts: one for an earlier prompt could answer a later command."""
        word = APPROVAL_CHOICE_WORDS.get(frame.get("choice"))
        if word is None or self._approval_id is None or frame.get("request_id") != self._approval_id:
            return
        if await self._approval_sync():
            return  # it ended just now; the page has another prompt, or none
        logger.info("voice page %s: approval answered on the page (%s)", self.client_id, frame.get("choice"))
        await self.cancel_reply("approval answer")  # the prompt's audio stops, as when the user speaks
        await self._answered_approval()
        self._reopen_reply()
        # No utterance came through the tracker for this answer, so it is told here
        # that a reply runs: the next utterance must stop it like any other.
        self.tracker.reply_started()
        await self.send_page({"type": "utterance", "text": word, **self._next_epoch()})
        if self._on_utterance is not None:
            await self._on_utterance(word, uuid.uuid4().hex)
        await self._approval_sync()  # the approval behind the answered one, if it waits

    async def _end_approval(self) -> bool:
        """Close the prompt on the page; whether one was open."""
        if self._approval_id is None:
            return False
        request_id, self._approval_id = self._approval_id, None
        await self.send_page({"type": "approval_end", "request_id": request_id})
        return True

    async def speak_prompt(self, sentence: str, show: bool = True) -> None:
        """Speak a prompt the running turn waits on, an approval prompt or a question
        the model asks, and remember the reply that raised it: the turn behind that
        reply is the one the user's spoken answer resumes."""
        self._prompt_reply = (self.reply_id,)
        spoken = await self.speak_notice(sentence, show=show)
        # The turn waits now, perhaps for a long time. A request left open keeps the
        # page from reporting the end of its audio, and VoiceManager holds the mic for
        # a reply that plays: the user's answer would not reach the recognizer. So the
        # request ends here. The reply stays open, and the text of the turn that goes
        # on opens a new request. The audio end of this one is no reply's end.
        # A prompt spoken with no reply open already has an ended request of its own.
        if spoken is None:
            spoken, self.reply_request = self.reply_request, None
            if spoken is not None and not spoken.cancelled:
                self._notice_requests[spoken.request_id] = None
                await spoken.done()
        # Kept until its audio ends, so an answer given while it plays can stop it.
        self._prompt_request = spoken

    async def speak_notice(self, text: str, ended: Optional[asyncio.Event] = None,
                           show: bool = True) -> Optional[vm_client.TTSRequest]:
        """Speak ``text`` now, through the running reply's TTS request or a new one.

        All of it goes to TTS at once, so nothing waits for more text. A word the
        reply still holds (its last delta ended without a space) goes first, as
        its own chunk, so it is not run together with the notice.

        With no reply open the notice is a TTS request of its own, ended at once so
        nothing stays open on VoiceManager. Its id is kept until its audio ends, so
        that end is not taken for a reply's end; ``ended`` is set then. A finished
        reply whose audio still plays keeps its own request. Returns the notice's
        request in that case, None when the notice went through the running reply.

        With ``show`` False the text is spoken but not sent to the page as reply text:
        the page already shows it."""
        if self.reply_open:
            held, self._unsent = self._unsent, ""
            await self._send_tts(held)
            await self._show(text if show else "", text)
            rest, self._unsent = self._unsent, ""
            await self._send_tts(rest)
            return None
        self._last_text_at = asyncio.get_running_loop().time()
        self.tracker.reply_text()
        if text and show:
            await self.send_page({"type": "reply_delta", "text": text})
        if not text.strip():
            return None
        # The request stays out of reply_request: an audio end or an utterance that
        # arrives while this waits on a send must find the reply state as it is.
        request = self.duplex.tts_request(chunking="clause")
        self._notice_requests[request.request_id] = ended
        await self._send_tts(text, request)
        await request.done()
        return request

    async def _end_session(self) -> None:
        """The user ended the session by voice: stop the reply, say so, and once the
        notice has played (or the wait for its end ran out) tell the page with a
        ``session_end`` frame and close it. The frame waits for the notice: a page
        leaves on it, and audio still in flight would go unheard."""
        logger.info("voice page %s: session ended by voice", self.client_id)
        ended = asyncio.Event()
        request = await self._command_notice(SESSION_ENDED_NOTICE, "session ended", ended)
        if request is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ended.wait(), SESSION_END_TIMEOUT_S)
        await self.send_page({"type": "session_end"})
        with contextlib.suppress(Exception):
            await self.page.close()

    async def _new_session(self) -> None:
        """The user asked for a new session by voice: stop the reply, say so, then
        have the adapter move the lane onto a fresh session and tell the page (or, on
        its own, send the page a ``session_new`` frame). The connection stays open
        and takes the next utterance; nothing reconnects."""
        logger.info("voice page %s: new session by voice", self.client_id)
        self._prompt_reply = None
        await self._command_notice(SESSION_NEW_NOTICE, "new session")
        if self._on_new_session is not None:
            await self._on_new_session()
        else:
            await self.send_page({"type": "session_new"})
        # The notice is no reply: the next utterance has nothing to cancel. The
        # command's turn is over as well: in classic mode the next listening turn
        # opens here, so later words start clean instead of joining the command's
        # segments. (Ending the session needs no next turn: its page closes.)
        await self._next_turn()

    async def notice(self, text: str) -> None:
        """Speak ``text`` as a notice of its own, outside any reply: the adapter's word
        on a command it ran for the page. Its TTS request is ended at once and its audio
        end is no reply's end, so nothing is left open for the next turn."""
        await self._command_notice(text, "notice")

    async def _command_notice(self, text: str, reason: str,
                              ended: Optional[asyncio.Event] = None) -> Optional[vm_client.TTSRequest]:
        """Stop a running reply and speak ``text`` as a TTS request of its own, ended at
        once; ``ended`` is set when its audio has ended. Returns the request, or None
        when no TTS request came of it."""
        if self.reply_open or self.reply_request is not None:
            await self.cancel_reply(reason)
        return await self.speak_notice(text, ended)

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
        self._turn_started = False
        self._cutter = SayCutter()

    def _reopen_reply(self) -> None:
        """The reply that raised the approval prompt takes text again under its own
        id: the running reply, unless a later utterance opened a newer one. Its old TTS
        request is gone: the cancel before this dropped it and cleared the page."""
        self._cancel_finishing()
        if self._prompt_reply is not None:
            (self.reply_id,), self._prompt_reply = self._prompt_reply, None
        self._turn_started = True  # the blocked turn goes on
        self.reply_open, self.reply_request, self._unsent = True, None, ""
        self._cutter = SayCutter()

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
        request = await self._close_reply(reason, send_cancel)
        await self._send_clear(request.request_id if request else None)
        await self._send_turn_done()

    async def _close_reply(self, reason: str, send_cancel: bool = True) -> Optional[vm_client.TTSRequest]:
        """Close the running reply: no more of its text, its TTS request dropped and
        cancelled. Returns that request, if it had one. The page is not told here."""
        logger.info("voice page %s: reply cancelled (%s)", self.client_id, reason)
        self._cancel_finishing()
        self.reply_open = False
        self._unsent = ""  # a closed reply's held word is never spoken, not even by a notice
        self._retire(self.reply_key)
        request, self.reply_request = self.reply_request, None
        prompt, self._prompt_request = self._prompt_request, None
        for stopped in (request, prompt):
            if stopped is not None:
                self._dropped.add(stopped.request_id)
                if send_cancel and not stopped.cancelled:
                    with contextlib.suppress(Exception):
                        await stopped.cancel()
        return request or prompt

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
        attached client the reply's text.

        A playback edge carries the page's ``mic_sample`` when it is a whole number
        >= 0; otherwise the count of mic samples forwarded so far stands in."""
        if self.attached and frame.get("type") in ("say", "say_end"):
            await self._on_say(frame)
            return
        if frame.get("type") == "approval_answer":
            await self._on_approval_answer(frame)
            return
        if frame.get("type") != "playback" or not self.turn_engine:
            return
        request_id = frame.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        mic_sample = frame.get("mic_sample")
        if not (isinstance(mic_sample, int) and not isinstance(mic_sample, bool) and mic_sample >= 0):
            mic_sample = None
        if frame.get("event") == "start":
            await self.duplex.playback_start(request_id, mic_sample)
        elif frame.get("event") == "stop":
            reply_sample = frame.get("reply_sample")
            if isinstance(reply_sample, int) and not isinstance(reply_sample, bool) and reply_sample >= 0:
                await self.duplex.playback_stop(request_id, mic_sample, reply_sample=reply_sample)

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

