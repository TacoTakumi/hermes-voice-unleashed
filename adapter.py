"""Gateway adapter for the voice page.

config.yaml ``gateway.platforms.voice_unleashed.extra`` keys: token,
voicemanager_url, voicemanager_token, host (127.0.0.1), port (8765).
The environment overrides them: VOICE_UNLEASHED_TOKEN,
VOICE_UNLEASHED_VOICEMANAGER_URL, VOICE_UNLEASHED_VOICEMANAGER_TOKEN,
VOICE_UNLEASHED_HOST, VOICE_UNLEASHED_PORT.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import re
import shlex
import sqlite3
import uuid
from typing import Any, NamedTuple, Optional

from agent.i18n import t
from gateway.config import Platform
from gateway.platforms._shared import coerce_port, get_scoped_secret, seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, ExecApprovalPrompt, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_busy import approval_input_words
from hermes_state import SessionDB
from tools import clarify_gateway, slash_confirm
from tools.approval import get_pending_gateway_approval, has_blocking_approval

from . import sessions, stream_hooks
from .server import JOIN_FAILED_CLOSE_CODE, REPLACED_CLOSE_CODE, VoiceServer
from .session import SESSION_NEW_NOTICE, VoiceSession, client_from_url, strip_say_tags

logger = logging.getLogger(__name__)

PLATFORM = "voice_unleashed"
OWNER = "owner"  # the one gateway user: whoever holds the access token
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8765
# The approval.inputs keys whose words answer a plain-text approval prompt, the
# same ones the gateway's run loop accepts.
APPROVAL_INPUT_KEYS = ("approve", "deny", "always", "session")
REASON_SPOKEN_CHARS = 80  # a longer reason is cut to this many characters, "..." included
# How long a command the plugin sent on a lane's behalf may take to be answered. The
# gateway handles a slash command in a background task, so its reply comes through
# send() after handle_message returned; without a reply the lane's session is checked.
COMMAND_REPLY_TIMEOUT_S = 15.0
# How many parent links a compaction chain is followed through when a join is judged.
LINEAGE_HOPS = 32
# The model_config keys under which a /new, /branch or delegate child names the parent
# it was made from. Such a child is not the continuation of its parent's conversation.
FORK_MARKERS = ("_reset_from", "_branched_from", "_delegate_from")
# What sqlite says when a read-only open finds no database file.
_MISSING_DB_FILE = "unable to open database file"


class _EmptyStateDB:
    """A state DB with nothing in it: what the readers get when the profile has no
    state.db file yet. It answers the reads the session list, the history frame, the
    message count and the lineage walk make."""

    def get_session(self, session_id: str) -> None:
        return None

    def get_messages(self, session_id: str, *args: Any, **kwargs: Any) -> list:
        return []

    def message_count(self, session_id: Optional[str] = None) -> int:
        return 0

    def list_sessions_rich(self, *args: Any, **kwargs: Any) -> list:
        return []

    def close(self) -> None:
        pass
# What the page is told, and hears, when /new left its lane on the session it was on.
NEW_SESSION_FAILED_NOTICE = "Could not start a new session; still on the same one."


class PendingReply(NamedTuple):
    """A command the plugin sent on a lane, waiting for the gateway's reply: the id of
    the command's message, and the future that gets the reply text."""
    message_id: str
    reply: asyncio.Future


def _setting(extra: dict, env: str, key: str, default: Any = "") -> Any:
    value = get_scoped_secret(env, "")
    if value:
        return value
    value = extra.get(key)
    return default if value in (None, "") else value


def _forked_from_parent(row: dict, parent_id: str) -> bool:
    """Whether the session row names ``parent_id`` under a fork marker in its
    model_config: it was made from the parent by /new, /branch or a delegate. Only a
    marker that names this parent counts: compaction copies model_config onto its
    child, so an older marker rides along on a genuine continuation."""
    cfg = row.get("model_config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            return False
    if not isinstance(cfg, dict):
        return False
    return any(cfg.get(marker) == parent_id for marker in FORK_MARKERS)


def _same_conversation(db, lane_id: Optional[str], requested: str) -> bool:
    """Whether the session ``lane_id`` is ``requested`` or a compaction descendant of it.

    Compaction ends a session and goes on in a child that points back at it with
    ``parent_session_id``, so the chain is walked from the lane's session towards
    its root, through the state DB's session rows, up to ``LINEAGE_HOPS`` links. A
    link is followed only when it is a compaction link, by the gateway's own rule:
    the parent row ended with ``compression`` and the child does not name the parent
    under a fork marker. A /new, /branch or delegate child points at its parent the
    same way but is another conversation. A session without a row, a link that is
    not a compaction link, a cycle or the cap ends the walk as no match."""
    if not lane_id:
        return False
    if lane_id == requested:
        return True
    seen = {lane_id}
    row = db.get_session(lane_id)
    while row and len(seen) < LINEAGE_HOPS:
        parent_id = row.get("parent_session_id")
        if not parent_id or parent_id in seen or _forked_from_parent(row, parent_id):
            return False
        parent = db.get_session(parent_id)
        if not parent or parent.get("end_reason") != "compression":
            return False
        if parent_id == requested:
            return True
        seen.add(parent_id)
        row = parent
    return False


def approval_words() -> set[str]:
    """The lower-case words that answer an approval prompt, from the gateway's
    language catalog. Read on each call: the language can change while running."""
    words: set[str] = set()
    for key in APPROVAL_INPUT_KEYS:
        words.update(approval_input_words(key))
    return words


# The spoken number of a choice; a longer list goes on in digits.
CHOICE_NUMBERS = ("One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine")


def clarify_sentence(question: str, choices: Optional[list] = None) -> str:
    """The spoken form of a question the model asks: the question and, when it comes
    with choices, each choice with its number and how to answer.

    The gateway appends a line for typed chats to the question, how to skip it by
    typing a word. That line is screen text and is not read out."""
    lines = (question or "").splitlines()
    try:
        typed_hint = t("gateway.clarify.skip_hint").strip()
    except Exception:
        typed_hint = ""
    while lines and (not lines[-1].strip() or lines[-1].strip() == typed_hint):
        lines.pop()
    spoken = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if spoken and spoken[-1] not in ".?!":
        spoken += "."
    if not choices:
        return spoken
    parts = [spoken] if spoken else []
    for index, choice in enumerate(choices):
        number = CHOICE_NUMBERS[index] if index < len(CHOICE_NUMBERS) else str(index + 1)
        text = re.sub(r"\s+", " ", str(choice)).strip()
        parts.append(f"{number}: {text}" + ("" if text[-1:] in (".", "?", "!") else "."))
    parts.append("Say the number or your answer.")
    return " ".join(parts)


_CARDINALS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")
_ORDINALS = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
             "eleventh", "twelfth")
# What a speech recognizer writes for a number said on its own. Used only for a
# one-word answer, where the word cannot be part of a sentence.
_SOUND_ALIKES = {"won": 1, "to": 2, "too": 2, "for": 4, "fore": 4, "ate": 8}
_BEFORE_A_NUMBER = ("number", "option", "choice")
_AFTER_AN_ORDINAL = ("one", "option", "choice")


def _spoken_words(text: str) -> list[str]:
    """The lower-case words of ``text`` without punctuation or a note in brackets."""
    return re.findall(r"[a-z0-9']+", re.sub(r"\([^)]*\)", " ", str(text)).lower())


def _spoken_number(words: list[str]) -> Optional[int]:
    """The choice number ``words`` name, said as "four", "number four", "option 4",
    "the fourth" or "the fourth one"; None when they say anything else."""
    if words and words[-1] == "please":
        words = words[:-1]
    if len(words) == 2 and words[0] in _BEFORE_A_NUMBER:
        words = words[1:]
        sound_alikes = {}
    else:
        sound_alikes = _SOUND_ALIKES
    if len(words) == 1:
        word = words[0]
        if word.isdigit():
            return int(word)
        if word in _CARDINALS:
            return _CARDINALS.index(word) + 1
        if word in _ORDINALS:
            return _ORDINALS.index(word) + 1
        return sound_alikes.get(word)
    if words[:1] == ["the"]:
        words = words[1:]
    if len(words) == 2 and words[1] in _AFTER_AN_ORDINAL:
        words = words[:1]
    if len(words) == 1 and words[0] in _ORDINALS:
        return _ORDINALS.index(words[0]) + 1
    return None


def spoken_choice(text: str, choices: Optional[list]) -> Optional[str]:
    """What to send the gateway for a spoken answer to a question with ``choices``, so
    that it counts as a selection: the exact choice text when the user said a choice's
    name, the digit when they said its number. None when the answer is neither; it
    then goes through as it was heard, as a free answer.

    The gateway matches a typed digit or the exact choice text. A speech recognizer
    writes "Four." or "Red.", which matches neither."""
    if not choices:
        return None
    words = _spoken_words(text)
    if not words:
        return None
    for choice in choices:  # a name wins over a number: a choice may be called "Two"
        if _spoken_words(choice) == words:
            return str(choice).strip()
    number = _spoken_number(words)
    if number is not None and 1 <= number <= len(choices):
        return str(number)
    return None


# Words before the program of a command that are not the program itself, each with
# its options that take a value in the next word: the value is not the program either.
_COMMAND_PREFIXES: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t", "--user", "--group",
                       "--host", "--prompt", "--close-from", "--chdir", "--chroot", "--command-timeout",
                       "--other-user", "--role", "--type"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata", "--pid"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "exec": frozenset({"-a"}),
    "time": frozenset({"-o", "-f", "--output", "--format"}),
    "nohup": frozenset(), "command": frozenset(), "setsid": frozenset(),
}
# Options that make a prefix word do something else than run the next program.
_NOT_A_RUN: dict[str, frozenset[str]] = {"sudo": frozenset({"-e", "--edit", "-l", "--list", "-v", "--validate"})}
# Long options of those words that take no value. Any other long option without "="
# may take one, and then the program cannot be told.
_PLAIN_LONG_OPTIONS = frozenset({"--", "--login", "--preserve-env", "--non-interactive", "--stdin", "--shell",
                                 "--set-home", "--reset-timestamp", "--ignore-environment", "--null",
                                 "--foreground", "--preserve-status", "--verbose", "--wait", "--fork"})
_NUMBER_OR_DURATION = re.compile(r"\d+(\.\d+)?[smhd]?$")
_PROGRAM_NAME = re.compile(r"[\w.+-]+$")


def command_program(command: str) -> str:
    """The name of the first program ``command`` runs: no directory, no ``sudo`` or
    other prefix word with its options, no environment assignment. Empty when it
    cannot be told: a command that starts with a redirect, a shell keyword or a
    grouping names no program in its first word, an unknown option of a prefix word
    may take the next word as its value, and a wrong name is worse than none."""
    line = next((line for line in command.splitlines() if line.strip()), "")
    try:
        tokens, split_by_shell_rules = shlex.split(line), True
    except ValueError:  # an open quote, closed on a later line: only plain words are safe
        tokens, split_by_shell_rules = line.split(), False
    value_flags: frozenset[str] = frozenset()
    not_a_run: frozenset[str] = frozenset()
    skip_value = prefixed = False
    for token in tokens:
        if not split_by_shell_rules and ("'" in token or '"' in token):
            return ""
        if skip_value:
            skip_value = False
            continue
        if re.match(r"\w+=", token):
            continue
        if re.match(r"\d*[<>]|&>", token):
            return ""  # a redirect comes first: what runs is not in the first words
        if prefixed and token.startswith("--"):
            if token in not_a_run:
                return ""
            if token in value_flags:
                skip_value = True
            elif "=" not in token and token not in _PLAIN_LONG_OPTIONS:
                return ""
            continue
        if prefixed and token.startswith("-"):
            # Short options, perhaps several in one word ("-iu postgres"). The first
            # that takes a value ends the word: the value is the rest of the word,
            # or the next word when nothing is left.
            for at, letter in enumerate(token[1:], start=1):
                if "-" + letter in not_a_run:
                    return ""
                if "-" + letter in value_flags:
                    skip_value = at == len(token) - 1
                    break
            continue
        if prefixed and _NUMBER_OR_DURATION.match(token):
            continue  # the duration of timeout
        if token in _COMMAND_PREFIXES:
            value_flags, prefixed = _COMMAND_PREFIXES[token], True
            not_a_run = _NOT_A_RUN.get(token, frozenset())
            continue
        name = token.rsplit("/", 1)[-1]
        if name in _SHELL_WORDS or not _PROGRAM_NAME.match(name):
            return ""
        return name
    return ""


# First words of a command that are shell syntax, not a program.
_SHELL_WORDS = frozenset({"if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done",
                          "case", "esac", "function", "select", "time", "coproc"})


def spoken_reason(reason: str) -> str:
    """``reason`` as words to speak: a note in brackets is dropped, a slash between
    alternatives is said as "or", and what is left of code characters goes."""
    reason = re.sub(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}", " ", str(reason or ""))
    reason = re.sub(r"(?<=\w)/(?=[\w-])", " or ", reason)
    reason = re.sub(r"[\"'`;|/<>$(){}\[\]\\]", " ", reason)
    return re.sub(r"\s+", " ", reason).strip().rstrip(".,:")


def approval_sentence(command: str, reason: str = "") -> str:
    """The spoken form of an approval prompt: why the command needs approval, the
    program it runs, and how to answer.

    The command itself is not spoken: code read aloud tells the listener nothing,
    and the page shows it. ``reason`` is the gateway's reason for the prompt; its
    default text, which names no reason, counts as none.
    """
    program = command_program(command)
    if str(reason or "").strip().rstrip(".") == t("gateway.exec_approval.default_reason").strip().rstrip("."):
        reason = ""
    reason = spoken_reason(reason)
    if len(reason) > REASON_SPOKEN_CHARS:
        reason = reason[:REASON_SPOKEN_CHARS - 3] + "..."
    if reason and program:
        return f"Approval needed: {reason}, with {program}. Say yes or no."
    if reason:
        return f"Approval needed: {reason}. Say yes or no."
    if program:
        return f"Approval needed to run {program}. Say yes or no."
    return "Approval needed. Say yes or no."


class VoiceLiveAdapter(BasePlatformAdapter):
    """Serves the voice page and relays each connected page to Hermes."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = getattr(config, "extra", {}) or {}
        # The owner is the only user, so they are the platform's slash admin without
        # configuration: a cross-session command such as /resume --all needs a user
        # listed in the gateway's DM slash policy, not just an ungated one.
        if isinstance(getattr(config, "extra", None), dict) and not config.extra.get("allow_admin_from"):
            config.extra["allow_admin_from"] = [OWNER]
        self.token = str(_setting(extra, "VOICE_UNLEASHED_TOKEN", "token"))
        self.vm_url = str(_setting(extra, "VOICE_UNLEASHED_VOICEMANAGER_URL", "voicemanager_url"))
        self.vm_token = str(_setting(extra, "VOICE_UNLEASHED_VOICEMANAGER_TOKEN", "voicemanager_token"))
        self.host = str(_setting(extra, "VOICE_UNLEASHED_HOST", "host", DEFAULT_HOST))
        self.port = coerce_port(_setting(extra, "VOICE_UNLEASHED_PORT", "port", DEFAULT_PORT), DEFAULT_PORT)
        self._server: Optional[VoiceServer] = None
        self._clients: dict[str, Any] = {}  # chat_id -> the page's WebSocket connection
        self._sessions: dict[str, VoiceSession] = {}  # chat_id -> its voice session
        # Hermes session id -> the voice session of the UI client attached to it. Kept
        # apart from the pages, so the stream hooks and send() never reach one.
        self._attached: dict[str, VoiceSession] = {}
        # chat_id -> the command the plugin sent on that lane and is waiting on. While one
        # is pending, send() of a reply to the command's message resolves it instead of
        # showing a reply frame.
        self._pending_replies: dict[str, PendingReply] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def name(self) -> str:
        return "Voice Unleashed"

    @property
    def bound_port(self) -> int:
        return self._server.bound_port if self._server else self.port

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.token or not self.vm_url:
            self._set_fatal_error(
                "config_missing", "VOICE_UNLEASHED_TOKEN and VOICE_UNLEASHED_VOICEMANAGER_URL must be set",
                retryable=False)
            return False
        try:
            self._server = VoiceServer(self.host, self.port, self.token, self._on_client,
                                       on_attach=self._on_attach, on_sessions=self._session_rows)
            await self._server.start()
        except OSError as exc:
            logger.error("voice page: cannot listen on %s:%s: %s", self.host, self.port, exc)
            self._server = None
            self._set_fatal_error("listen_failed", str(exc), retryable=True)
            return False
        self.loop = asyncio.get_running_loop()
        stream_hooks.attach(self)
        self._mark_connected()
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        stream_hooks.detach(self)
        self._mark_disconnected()
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self._clients.clear()

    async def _on_client(self, connection, client_id: str, **options) -> None:
        """A page connected; ``options`` are the server's connect options (``session``)."""
        self._clients[client_id] = connection
        try:
            await self._run_client(connection, client_id, **options)
        finally:
            if self._clients.get(client_id) is connection:
                del self._clients[client_id]

    def is_attached(self, session_id: str) -> bool:
        """Whether a UI client has voice attached to the Hermes session ``session_id``."""
        return session_id in self._attached

    async def _on_attach(self, connection, session_id: str) -> None:
        """Voice for a UI client that shows the Hermes session itself.

        Each finished utterance goes to the client only (it submits it to the chat),
        and the reply's text comes back from the client, not from the stream hooks.
        One voice device per session: a page whose lane is on the session is switched
        off first."""
        for client_id, page in list(self._clients.items()):
            if self.lane_session_id(client_id) == session_id:
                logger.info("voice page %s: a UI client took voice for session %s", client_id, session_id)
                # Dropped here, not only at the page's own cleanup: a page still in its
                # connect steps checks this entry to see that it was replaced.
                del self._clients[client_id]
                await self._replace(page)
        session = VoiceSession(connection, client_from_url(self.vm_url, self.vm_token),
                               f"attach:{session_id}", attached=True)
        self._attached[session_id] = session
        logger.info("voice attached to session %s", session_id)
        try:
            await session.run()
        finally:
            if self._attached.get(session_id) is session:
                del self._attached[session_id]
            logger.info("voice detached from session %s", session_id)

    async def _replace(self, connection) -> None:
        """Switch a voice device off: another one took its session. It is told so, and
        the close code keeps it from reconnecting (the page and the UI client both end
        voice on it)."""
        with contextlib.suppress(Exception):
            await connection.send(json.dumps({"type": "replaced"}))
        with contextlib.suppress(Exception):
            await connection.close(code=REPLACED_CLOSE_CODE, reason="replaced")

    async def _take_voice(self, client_id: str, session_id: Optional[str]) -> None:
        """The page ``client_id`` is on the session ``session_id`` now: a UI client with
        voice attached to it is switched off. Dropped from the attached sessions at once,
        so the session list does not show it attached until its own cleanup runs."""
        session = self._attached.get(session_id) if session_id else None
        if session is None:
            return
        logger.info("voice page %s: took voice for session %s from a UI client", client_id, session_id)
        del self._attached[session_id]
        await self._replace(session.page)

    def _new_session(self, connection, client_id: str) -> VoiceSession:
        async def on_utterance(text: str, message_id: str) -> None:
            await self.dispatch_utterance(client_id, text, message_id)

        async def approval_pending() -> bool:
            # The gateway keeps the blocked approvals by session key; the page's
            # chat maps to the same key every utterance is handled under.
            return has_blocking_approval(self._source_session_key(self._voice_source(client_id)))

        async def approval_oldest() -> Optional[str]:
            # The gateway resolves a bare word against the first approval of the
            # session's queue; this is that approval's id.
            entry = get_pending_gateway_approval(self._source_session_key(self._voice_source(client_id)))
            return (entry or {}).get("request_id") or None

        async def clarify_pending() -> bool:
            # A question the model asked waits under the same session key. The gateway
            # takes the next message of the chat as its answer, whatever kind it is.
            key = self._source_session_key(self._voice_source(client_id))
            return clarify_gateway.get_pending_for_session(key, include_choice_prompts=True) is not None

        async def clarify_answer(text: str) -> Optional[str]:
            # A spoken number or choice name becomes the form the gateway takes as a
            # selection. A question that takes several choices keeps the text as heard.
            key = self._source_session_key(self._voice_source(client_id))
            entry = clarify_gateway.get_pending_for_session(key, include_choice_prompts=True)
            if entry is None or entry.multi_select:
                return None
            return spoken_choice(text, entry.choices)

        async def on_new_session() -> None:
            await self._new_session_by_voice(client_id)

        return VoiceSession(connection, client_from_url(self.vm_url, self.vm_token), client_id,
                            on_utterance=on_utterance, approval_words=approval_words,
                            approval_pending=approval_pending, approval_oldest=approval_oldest,
                            clarify_pending=clarify_pending,
                            clarify_answer=clarify_answer,
                            on_new_session=on_new_session)

    async def _new_session_by_voice(self, client_id: str) -> None:
        """The page asked for a new session by voice: move its lane onto a fresh
        session (the same rule as a connect with session=new), then tell the page which
        session it is on now with a ``session_new`` frame and the new session's history,
        which is empty. The socket stays: a reconnect would leave a gap in which a late
        reply could be redelivered into the new session.

        When /new left the lane where it was, the page gets no ``session_new`` frame:
        it is told in a reply, and hears the same words, that it is still on the old
        session."""
        if not await self._start_new_session(client_id):
            # The page shows a notice as it is spoken, on the current line; a reply frame
            # with the line's text ends the line without adding one. So the "New session"
            # line is ended first, the failure is spoken on a line of its own, and the
            # reply frame that follows it adds no second line.
            await self._send_json(client_id, {"type": "reply", "text": SESSION_NEW_NOTICE})
            session = self.session_for(client_id)
            if session is not None:
                await session.notice(NEW_SESSION_FAILED_NOTICE)
            await self._send_json(client_id, {"type": "reply", "text": NEW_SESSION_FAILED_NOTICE})
            return
        await self._send_json(client_id, {"type": "session_new", "session_id": self.lane_session_id(client_id)})
        await self._send_history(client_id)

    def _voice_source(self, chat_id: str, message_id: Optional[str] = None):
        """The source every message from the page's chat ``chat_id`` is handled under."""
        return self.build_source(chat_id=chat_id, chat_name="Voice", chat_type="dm",
                                 user_id=OWNER, user_name="Owner", message_id=message_id)

    async def dispatch_utterance(self, chat_id: str, text: str, message_id: Optional[str] = None) -> None:
        """Hand one finished utterance to Hermes as a voice message.

        The text is already transcribed, so the event carries no audio file and
        the gateway does not transcribe it again. Being a voice message makes the
        turn a voice turn, which applies auxiliary.voice_chat (reasoning off by
        default). The message id comes back in the turn's session context, which
        is how the stream hooks tell this reply's text from an older one.
        """
        message_id = message_id or uuid.uuid4().hex
        source = self._voice_source(chat_id, message_id)
        await self.handle_message(MessageEvent(
            text=text, message_type=MessageType.VOICE, source=source,
            message_id=message_id, timestamp=datetime.datetime.now()))

    async def send_command(self, chat_id: str, text: str) -> Optional[str]:
        """Send the slash command ``text`` on the lane ``chat_id`` as if the page had said it,
        and return the gateway's reply, which stays off the page. None when no reply came
        in time. The command's message gets an id of its own, and send() knows the reply
        by it: the gateway sends a command's reply with ``reply_to`` set to that id."""
        loop = asyncio.get_running_loop()
        pending = PendingReply(uuid.uuid4().hex, loop.create_future())
        self._pending_replies[chat_id] = pending
        try:
            await self.handle_message(MessageEvent(
                text=text, message_type=MessageType.TEXT, source=self._voice_source(chat_id),
                message_id=pending.message_id, timestamp=datetime.datetime.now()))
            async with asyncio.timeout(COMMAND_REPLY_TIMEOUT_S):
                return await pending.reply
        except TimeoutError:
            logger.warning("voice page %s: no reply to %r", chat_id, text)
            return None
        finally:
            if self._pending_replies.get(chat_id) is pending:
                del self._pending_replies[chat_id]

    def _gateway_session_store(self):
        """The gateway's session store: the one set on this adapter, else the runner's."""
        store = getattr(self, "_session_store", None)
        if store is None:
            runner = getattr(self._message_handler, "__self__", None)
            store = getattr(runner, "session_store", None)
        return store

    def lane_session_id(self, chat_id: str) -> Optional[str]:
        """The Hermes session the lane ``chat_id`` points at, None when unknown."""
        store = self._gateway_session_store()
        if store is None:
            return None
        return store.peek_session_id(self._source_session_key(self._voice_source(chat_id)))

    async def _join_session(self, connection, client_id: str, session_id: str) -> bool:
        """Move the lane ``client_id`` onto the Hermes session ``session_id`` with /resume.

        The command goes through the gateway's slash dispatch like any message of the
        lane, so the lane's session key moves. When the key does not point at the
        session afterwards, the page is told and the connection is closed with a code
        of its own, so the page does not reconnect into a conversation it did not ask
        for."""
        reply = await self.send_command(client_id, f"/resume {session_id} --all")
        if await self._lane_on_session(client_id, session_id):
            return True
        message = f"could not join session {session_id}"
        if reply:
            message = f"{message}: {reply}"
        logger.info("voice page %s: %s", client_id, message)
        # On the connecting socket itself: another page may hold the chat id by now.
        await self._send_to(connection, {"type": "join_failed", "message": message})
        with contextlib.suppress(Exception):
            await connection.close(code=JOIN_FAILED_CLOSE_CODE, reason="join failed")
        return False

    async def _lane_on_session(self, client_id: str, session_id: str) -> bool:
        """Whether the lane ``client_id`` is on the conversation ``session_id``: on that
        session, or on a compaction descendant of it. The gateway's /resume follows the
        compaction chain to its live tip, so the lane lands on the tip even when the
        page asked for an ancestor (a session the lane was on before a compaction while
        the page was connected). A chain that cannot be read counts as no match."""
        lane_id = self.lane_session_id(client_id)
        if lane_id == session_id:
            return True

        def walk() -> bool:
            with self._session_db() as db:
                return _same_conversation(db, lane_id, session_id)

        try:
            # SQLite reads block; keep them off the event loop.
            return await asyncio.to_thread(walk)
        except Exception:
            logger.warning("voice page %s: cannot read the lineage of session %s",
                           client_id, lane_id, exc_info=True)
            return False

    def session_for(self, chat_id: str) -> Optional[VoiceSession]:
        return self._sessions.get(chat_id)

    @contextlib.contextmanager
    def _session_db(self):
        """The state DB to read sessions from, for the duration of the block.

        The runner keeps the DB behind an async facade; the readers want the plain
        SessionDB under it. A runner without a DB (or no runner, as in a test)
        gets the profile's state DB opened read-only for this one block.
        """
        runner = getattr(self, "gateway_runner", None)
        try:
            db = getattr(runner, "_session_db", None)
        except Exception:
            logger.debug("voice page: the runner's session DB is not available", exc_info=True)
            db = None
        db = getattr(db, "_db", db)
        if db is not None:
            yield db
            return
        try:
            fallback = SessionDB(read_only=True)
        except sqlite3.OperationalError as exc:
            # A profile that has not stored a session yet has no state DB file, which
            # is normal on first use: a read-only open cannot create it, so the
            # readers see an empty DB instead.
            if _MISSING_DB_FILE not in str(exc):
                raise
            logger.debug("voice page: no state DB yet, reading it as empty")
            yield _EmptyStateDB()
            return
        try:
            yield fallback
        finally:
            fallback.close()

    def _session_rows(self, client_id: str) -> list[dict[str, Any]]:
        """The session list for the page's chat ``client_id``: the profile's sessions
        from the runner's state DB, the one its lane is on marked as current, and the
        one a UI client has voice attached to marked ``voice_attached``."""
        runner = getattr(self, "gateway_runner", None)
        store = getattr(runner, "session_store", None) or getattr(self, "_session_store", None)
        if store is None:
            logger.warning("voice page: no session store yet, the session list is empty")
            return []
        lane_key = self._source_session_key(self._voice_source(client_id))
        with self._session_db() as db:
            rows = sessions.session_rows(store, db, lane_key)
        # A compacted chain is one row whose id is its live tip, the id a client attaches to.
        for row in rows:
            row["voice_attached"] = row.get("id") in self._attached
        return rows

    def _history_lines(self, session_id: str) -> list[dict[str, Any]]:
        """The spoken lines of the session ``session_id`` from the state DB."""
        with self._session_db() as db:
            return sessions.history_lines(db, session_id)

    async def _send_history(self, client_id: str, connection=None) -> Optional[str]:
        """Send the page the history frame: the session its lane is on and that
        session's spoken lines. A session the lane does not have yet, or a read that
        fails, gives an empty history; the connect goes on either way. Returns the
        session id the frame named.

        The frame goes on ``connection`` when given: a connecting page, which may not
        hold the chat id any more by the time its join step is done. Without one it
        goes to the page that holds the chat id, the live one."""
        session_id = self.lane_session_id(client_id)
        lines: list[dict[str, Any]] = []
        if session_id:
            try:
                # SQLite reads block; keep them off the event loop.
                lines = await asyncio.to_thread(self._history_lines, session_id)
            except Exception:
                logger.warning("voice page %s: cannot read the history of session %s",
                               client_id, session_id, exc_info=True)
                lines = []
        frame = {"type": "history", "session_id": session_id, "lines": lines}
        if connection is None:
            await self._send_json(client_id, frame)
        else:
            await self._send_to(connection, frame)
        return session_id

    async def _start_new_session(self, client_id: str) -> bool:
        """Move the lane ``client_id`` onto a fresh session with /new, unless the one it
        is on is empty already: a session without a stored message is as good as new,
        and sending /new would only stack up empty sessions. A lane with no session yet
        gets its first one from its first utterance. The command's reply stays off the
        page, like /resume's.

        Returns True when the lane is on a session as good as new afterwards: it moved,
        or no /new was needed. False when /new left the lane on the session it was on,
        as when the gateway answered with a text prompt instead of starting one."""
        session_id = self.lane_session_id(client_id)
        if session_id is None:
            return True

        def count_messages() -> int:
            with self._session_db() as db:
                return int(db.message_count(session_id))

        try:
            # SQLite reads block; keep them off the event loop.
            count = await asyncio.to_thread(count_messages)
        except Exception:
            # The user asked for a new session; without a count, /new is the safe side.
            logger.warning("voice page %s: cannot count the messages of session %s, sending /new",
                           client_id, session_id, exc_info=True)
            count = 1
        if count > 0:
            await self.send_command(client_id, "/new")
            if self.lane_session_id(client_id) == session_id:
                logger.warning("voice page %s: the lane is still on session %s after /new",
                               client_id, session_id)
                return False
        return True

    async def _run_client(self, connection, client_id: str, session: Optional[str] = None) -> None:
        # session=new asks for a fresh session, not a join; the history is of
        # whatever session the lane is on once the connect steps are done.
        lane_before = self.lane_session_id(client_id)
        new_session_failed = False
        if session == "new":
            new_session_failed = not await self._start_new_session(client_id)
        elif session and not await self._join_session(connection, client_id, session):
            return
        if self._clients.get(client_id) is not connection:
            # Another page took the chat id while this one waited on its connect steps:
            # this connection was closed as replaced and is no voice device any more.
            # When its step moved the lane, the live page is on another session now
            # and is told with a fresh history frame; otherwise nothing more is sent.
            logger.debug("voice page %s: replaced during its connect", client_id)
            if self.lane_session_id(client_id) != lane_before:
                await self._send_history(client_id)
            return
        session_id = await self._send_history(client_id, connection)
        if new_session_failed:
            # The history frame named the old session; the page is told why. Sent here,
            # before the guards below, so that nothing is awaited between the last guard
            # and the session's registration.
            await self._send_to(connection, {"type": "reply", "text": NEW_SESSION_FAILED_NOTICE})
        if self._clients.get(client_id) is not connection:
            # Replaced while the history was read or sent: the lane did not move, so the
            # live page's own history frame stands, and this connect is no voice device.
            logger.debug("voice page %s: replaced during its history read", client_id)
            return
        # The page is the newest voice device of its session: a UI client attached
        # to it is switched off before the page's voice starts.
        await self._take_voice(client_id, session_id)
        if self._clients.get(client_id) is not connection:
            logger.debug("voice page %s: replaced during its voice takeover", client_id)
            return
        session = self._new_session(connection, client_id)
        self._sessions[client_id] = session
        try:
            await session.run()
        finally:
            if self._sessions.get(client_id) is session:
                del self._sessions[client_id]

    async def _send_json(self, chat_id: str, payload: dict) -> bool:
        """Send ``payload`` to the page that holds the chat ``chat_id``."""
        connection = self._clients.get(chat_id)
        if connection is None:
            return False
        return await self._send_to(connection, payload)

    async def _send_to(self, connection, payload: dict) -> bool:
        """Send ``payload`` on ``connection``; False when it is gone."""
        try:
            await connection.send(json.dumps(payload))
        except Exception:
            return False
        return True

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Text from the gateway for the lane ``chat_id``. The gateway sends every reply
        with ``reply_to`` set to the id of the message it answers, a command's reply and
        a model reply alike, so the reply to a command the plugin sent is the one whose
        ``reply_to`` is that command's message id: it is for the plugin, not the page.
        Any other text, such as the model's answer to the utterance before a reload
        that lands while a /resume waits, is a reply line on the page."""
        pending = self._pending_replies.get(chat_id)
        if pending is not None and not pending.reply.done() and reply_to == pending.message_id:
            pending.reply.set_result(content)
            return SendResult(success=True, message_id=None)
        # The page shows the reply's text without the say tags a Desktop voice chat's
        # replies carry; the session already spoke what it should.
        frame: dict[str, Any] = {"type": "reply", "text": strip_say_tags(content)}
        session = self.session_for(chat_id)
        if reply_to is None and session is not None and session.turn_running:
            # Text the gateway sends during a turn on its own account, a status line
            # before the answer for one: the page must not take it for the whole answer
            # of a turn that never ran. The turn's own turn_done frame ends it. The
            # turn's reply itself (its answer, or its error text when the turn failed
            # and no turn_done follows) comes with reply_to and is not marked.
            frame["turn_running"] = True
        if await self._send_json(chat_id, frame):
            return SendResult(success=True, message_id=None)
        return SendResult(success=False, error="the voice page is not connected")

    async def _notify_turn_error(self, event, e):
        """The gateway reports a turn that raised. No end-of-turn hook follows it, so
        the session's turn is over here: the error notice then reaches the page as the
        turn's whole answer."""
        with contextlib.suppress(Exception):
            session = self.session_for(event.source.chat_id)
            if session is not None:
                session.turn_failed()
        return await super()._notify_turn_error(event, e)

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str,
                                 confirm_id: str, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """The gateway asks before a destructive command such as /new. A command the plugin
        itself sent on the lane (its reply is pending) was asked for by the user, by tapping
        New or saying it, so it is confirmed at once and the command's reply becomes the
        pending reply: the gateway sends no text of its own once the confirmation is taken.
        Any other confirmation is left to the gateway's text prompt."""
        pending = self._pending_replies.get(chat_id)
        if pending is None or pending.reply.done():
            return await super().send_slash_confirm(chat_id, title, message, session_key, confirm_id, metadata)
        logger.info("voice page %s: %s confirmed, the page asked for it", chat_id, title)
        reply = await slash_confirm.resolve(session_key, confirm_id, "once")
        if not pending.reply.done():
            pending.reply.set_result(reply or "")
        return SendResult(success=True, message_id=None)

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Show and speak an approval prompt.

        The page gets the command, the gateway's reason and the gateway's choices in
        a frame of its own, and shows them as a prompt with one button per choice.
        The short sentence is spoken, so a user who is not looking at the page knows
        the turn waits and how to answer. Without a connected page the prompt is not
        sent, and the gateway falls back to its text prompt.
        """
        session = self.session_for(prompt.chat_id)
        if self._clients.get(prompt.chat_id) is None or session is None:
            return SendResult(success=False, error="the voice page is not connected")
        request_id = prompt.request_id or uuid.uuid4().hex
        frame = {"type": "approval", "request_id": request_id, "command": prompt.command,
                 "reason": prompt.description,
                 "choices": [{"choice": choice, "label": label, "style": style}
                             for label, choice, style in prompt.actions]}
        await session.open_approval(request_id, approval_sentence(prompt.command, prompt.description), frame)
        return SendResult(success=True, message_id=None)

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[dict[str, Any]] = None) -> SendResult:
        """Show and speak a question the model asks.

        The gateway's default shows the question (and its numbered choices) on the
        page as a reply and takes the chat's next message as the answer. The question
        is also spoken, so a user who is not looking at the page knows the turn waits
        for them. Without a connected page only the default runs.
        """
        result = await super().send_clarify(chat_id, question, choices, clarify_id, session_key,
                                            metadata=metadata)
        session = self.session_for(chat_id)
        if getattr(result, "success", False) and session is not None:
            try:
                # The default send above already put the question on the page.
                await session.speak_prompt(clarify_sentence(question, choices), show=False)
            except Exception:
                logger.warning("voice page %s: the question was shown but not spoken", chat_id, exc_info=True)
        return result

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        """Never: VoiceManager speaks every reply, so Hermes must not synthesize one too.

        The gateway asks this before it makes a TTS call for a reply to a voice message,
        whatever ``voice.auto_tts`` or ``/voice`` say; a call made here would be paid for
        and then dropped by ``play_tts``."""
        return False

    async def play_tts(self, chat_id: str, audio_path: str, **kwargs) -> SendResult:
        """VoiceManager already speaks every reply; a Hermes TTS file would say it twice."""
        return SendResult(success=True, message_id=None)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """The page shows its own state; no typing indicator."""

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}


def check_requirements() -> bool:
    """Passive check: websockets and httpx ship with Hermes, so only config matters."""
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(_setting(extra, "VOICE_UNLEASHED_TOKEN", "token") and
                _setting(extra, "VOICE_UNLEASHED_VOICEMANAGER_URL", "voicemanager_url"))


def _env_enablement() -> Optional[dict]:
    token = get_scoped_secret("VOICE_UNLEASHED_TOKEN", "").strip()
    url = get_scoped_secret("VOICE_UNLEASHED_VOICEMANAGER_URL", "").strip()
    if not (token and url):
        return None
    seed = seed_extra_from_env((
        ("VOICE_UNLEASHED_VOICEMANAGER_TOKEN", "voicemanager_token", None),
        ("VOICE_UNLEASHED_HOST", "host", None),
        ("VOICE_UNLEASHED_PORT", "port", int),
    ))
    return {"token": token, "voicemanager_url": url, **seed}


def register(ctx):
    """Plugin entry point."""
    stream_hooks.register(ctx)
    ctx.register_platform(
        name=PLATFORM,
        label="Voice Unleashed",
        dashboard_description="A hands-free voice interface from any browser, on your VoiceManager server.",
        adapter_factory=VoiceLiveAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=validate_config,
        required_env=["VOICE_UNLEASHED_TOKEN", "VOICE_UNLEASHED_VOICEMANAGER_URL"],
        install_hint="No extra packages needed",
        env_enablement_fn=_env_enablement,
        allowed_users_env="VOICE_UNLEASHED_ALLOWED_USERS",
        emoji="🎙",
        # The page's activity panel shows each tool call from the tool hooks; the
        # gateway's progress messages would land in the conversation log as well.
        display_overrides={"tool_progress": "off"},
        platform_hint=(
            "You are talking with the user by voice. Your reply is spoken aloud as you write it. "
            "Answer in short, plain spoken sentences. Do not use markdown, tables, code blocks "
            "or lists unless the user asks for them."),
    )
