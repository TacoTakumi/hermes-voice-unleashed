"""When the user's turn ends, and what to do with a reply still running.

Two modes, the same as VoiceManager's reference agent (scripts/voice_agent.py
classic):

* Turn engine. The server decides: each ``turn_end`` frame dispatches its
  text. A ``turn_end`` while a reply runs cancels that reply first.
* Classic, when the server refused the turn engine. The latest text of each
  segment is kept, and the turn dispatches on the first endpointed frame
  (``endpoint`` or ``is_final``) with words. While a reply runs, a frame
  whose words differ from the dispatched ones ends it: a change in a
  dispatched segment supersedes the question (the revision is dispatched at
  its endpoint), and words in a new segment are the user talking over the
  reply (barge-in; the answered segments leave the text).

The tracker only decides. ``dispatch(text)`` and ``cancel(reason)`` are the
session's: they send the message to Hermes and stop the reply.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

Dispatch = Callable[[str], Awaitable[None]]
Cancel = Callable[[str], Awaitable[None]]

_WORD = re.compile(r"[\w']+")


def heard_words(text: str) -> list[str]:
    """The words of ``text``, lower case, without punctuation: what counts as said."""
    return [w.lower() for w in _WORD.findall(text or "")]


def normalized_phrase(text: str) -> str:
    """``text`` as one lower-case phrase: punctuation stripped, spaces collapsed."""
    return " ".join(heard_words(text))


# Spoken commands: a whole utterance that the session acts on itself instead of
# sending it to Hermes. The phrase maps to the command it stands for.
END_SESSION = "end"
NEW_SESSION = "new"
SPOKEN_COMMANDS = {
    "end session": END_SESSION,
    "end the session": END_SESSION,
    "end this session": END_SESSION,
    "end of session": END_SESSION,
    "end discussion": END_SESSION,
    "end of discussion": END_SESSION,
    "new session": NEW_SESSION,
    "start a new session": NEW_SESSION,
    "start new session": NEW_SESSION,
}


def spoken_command(text: str) -> Optional[str]:
    """The command ``text`` is, when the whole utterance is one of the phrases."""
    return SPOKEN_COMMANDS.get(normalized_phrase(text))


# Spoken answers to an approval prompt, beyond the gateway's own bare words.
# Approve only on a whole utterance: "yes, but change the path" must not run a command.
APPROVE_PHRASES = frozenset({
    "yes please", "yes go ahead", "yes do it", "yes yes", "yes approve", "yes approve it",
    "yes run it", "yes you can", "yes that's fine", "go ahead", "please go ahead", "go for it",
    "do it", "please do", "run it", "approve it", "approved", "i approve", "yes i approve",
    "allow", "allow it", "that's fine", "ok go ahead", "okay go ahead", "ok do it", "okay do it",
    "yeah", "yeah go ahead", "yep", "yep go ahead", "sure", "sure go ahead",
})
# Deny on a short utterance that starts with one of these: a wrong deny costs nothing,
# since any other utterance makes the gateway withdraw the approval anyway. A longer one
# stays a message, so the model gets what it says.
DENY_FIRST_WORDS = frozenset({"no", "nope", "deny", "reject", "cancel", "don't", "stop"})
DENY_PHRASES = frozenset({"do not", "do not do it", "do not run it"})
DENY_MAX_WORDS = 5
# A user who was not heard says the answer again: "I said yes".
REPEAT_PREFIX = ["i", "said"]
REPEATED_APPROVE_WORDS = frozenset({"yes", "ok", "okay", "approve", "confirm"})
APPROVE, DENY = "yes", "no"  # the bare words the gateway takes in every language


def spoken_approval(text: str) -> Optional[str]:
    """The bare word that ``text`` means as an answer to an approval prompt, or None
    when it is not such an answer."""
    words = heard_words(text)
    if words[:2] == REPEAT_PREFIX:
        words = words[2:]
        if len(words) == 1 and words[0] in REPEATED_APPROVE_WORDS:
            return APPROVE
    phrase = " ".join(words)
    if phrase in APPROVE_PHRASES:
        return APPROVE
    if phrase in DENY_PHRASES or (words and len(words) <= DENY_MAX_WORDS and words[0] in DENY_FIRST_WORDS):
        return DENY
    return None


class TurnTracker:
    def __init__(self, dispatch: Dispatch, cancel: Cancel, turn_engine: Optional[str] = None) -> None:
        self._dispatch = dispatch
        self._cancel = cancel
        self.turn_engine = turn_engine
        self.turn_id: Optional[int] = None  # classic: frames of other turns are ignored
        self.reply_active = False
        self.reply_spoke = False  # the running reply produced text
        self._reset()

    def _reset(self) -> None:
        self.segments: dict[int, str] = {}
        self.dispatched_words: Optional[list[str]] = None
        self._dispatched_keys: set[int] = set()
        self._answered_keys: set[int] = set()

    @property
    def text(self) -> str:
        return " ".join(self.segments[k] for k in sorted(self.segments)).strip()

    def open_turn(self, turn_id: Optional[int]) -> None:
        """A new listening turn (classic mode, after a reply ends)."""
        self.turn_id = turn_id
        if not self.reply_active:
            self._reset()

    def reply_started(self) -> None:
        self.reply_active, self.reply_spoke = True, False

    def reply_text(self) -> None:
        self.reply_spoke = True

    def reply_ended(self) -> None:
        self.reply_active = False

    def turn_over(self) -> None:
        """Classic: the turn's reply is over and the next turn is not open yet. The
        answered segments leave the text now, so words heard before the turn_start
        ack start clean instead of joining them; a late confirmation of an answered
        segment is still ignored."""
        answered = self._answered_keys | self._dispatched_keys
        self._reset()
        self._answered_keys = answered

    def turn_engine_lost(self) -> None:
        self.turn_engine = None
        if not self.reply_active:
            self._reset()

    async def _cancel_reply(self, reason: str) -> None:
        self.reply_active = False
        await self._cancel(reason)

    async def _send(self, text: str, words: list[str]) -> None:
        self.dispatched_words = words
        self._dispatched_keys = set(self.segments)
        self.reply_started()
        await self._dispatch(text)

    async def on_transcript(self, frame: dict) -> None:
        if self.turn_engine:
            self.segments[frame.get("segment_index", 0)] = frame.get("text", "")
            return
        if self.turn_id is not None and frame.get("turn_id", 0) != self.turn_id:
            return
        seg = frame.get("segment_index", 0)
        if seg in self._answered_keys:
            return  # the endpoint hold confirming a segment answered before a barge-in
        self.segments[seg] = frame.get("text", "")
        text = self.text
        words = heard_words(text)
        if not words:
            return
        if self.reply_active:
            if words == self.dispatched_words:
                return  # a confirmation of what was dispatched
            if seg in self._dispatched_keys:
                await self._cancel_reply("superseded")
            else:
                await self._cancel_reply("barge-in")
                for key in self._dispatched_keys:
                    self.segments.pop(key, None)
                self._answered_keys |= self._dispatched_keys
                text = self.text
                words = heard_words(text)
                if not words:
                    return
        endpointed = bool(frame.get("endpoint") or frame.get("is_final"))
        if endpointed and words != self.dispatched_words:
            await self._send(text, words)

    async def on_turn_end(self, frame: dict) -> None:
        if not self.turn_engine:
            return
        text = (frame.get("text") or self.text).strip()
        self.segments = {}
        words = heard_words(text)
        if not words:
            return
        if self.reply_active:
            await self._cancel_reply("barge-in" if self.reply_spoke else "superseded")
        await self._send(text, words)
