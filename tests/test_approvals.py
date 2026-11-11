"""A bare approval word answers a pending approval; the running reply stays open for the
resumed turn's text, and a notice can be spoken all at once."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"
APPROVAL_WORDS = ("yes", "no")


class Page:
    def __init__(self, ws):
        self.ws, self.frames = ws, []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            if not isinstance(message, bytes):
                self.frames.append(json.loads(message))

    def of(self, kind):
        return [f for f in self.frames if f.get("type") == kind]


class Rig:
    """A ready page, its VoiceManager connection and its live session; approval
    answers are recorded as ``events``."""

    def __init__(self, adapter, session, page, conn, events):
        self.adapter, self.session, self.page, self.conn, self.events = adapter, session, page, conn, events
        self.pending = False
        self.oldest = None  # the request id the gateway holds as its oldest approval

    def wants(self, pending: bool) -> None:
        """Whether the chat waits for an approval right now."""
        self.pending = pending

    def tts_text(self) -> str:
        return "".join(f["text"] for f in self.conn.json_frames("tts_stream"))


@pytest.fixture
async def rig(plugin):
    async with FakeVoiceManager() as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        events = []

        async def handle_message(event):
            events.append(event)

        adapter.handle_message = handle_message
        assert await adapter.connect()
        try:
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=voice") as ws:
                page = Page(ws)
                await wait_for(lambda: page.of("ready"))
                await ws.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                session = adapter.session_for("voice")
                rig = Rig(adapter, session, page, conn, events)

                async def pending() -> bool:
                    return rig.pending

                session._approval_words = lambda: APPROVAL_WORDS
                async def oldest():
                    return rig.oldest

                session._approval_pending = pending
                session._approval_oldest = oldest
                yield rig
                page.task.cancel()
        finally:
            await adapter.disconnect()


async def _heard(rig, text: str) -> None:
    """VoiceManager's turn engine ends a turn with ``text``."""
    count = len(rig.events)
    await rig.conn.send({"type": "turn_end", "text": text, "turn_id": 0})
    await wait_for(lambda: len(rig.events) > count)


async def _reply_in_progress(rig) -> str:
    """An utterance was answered: its reply is open and spoke its first words.
    Returns the utterance's message id."""
    await _heard(rig, "tell me something")
    reply_id = rig.events[-1].message_id
    assert rig.session.reply_id == reply_id
    await rig.session.on_reply_delta(reply_id, "t1", 1, "Let me check. ")
    await wait_for(lambda: rig.tts_text() == "Let me check. ")
    return reply_id


@pytest.mark.parametrize("said, word", [("Yes.", "yes"), ("No!", "no")])
async def test_a_bare_approval_word_is_sent_without_its_punctuation(rig, said, word):
    rig.wants(True)
    await _heard(rig, said)
    assert rig.events[-1].text == word
    await wait_for(lambda: rig.page.of("utterance"))
    assert rig.page.of("utterance")[-1]["text"] == word


async def _resumed_turn_is_spoken(rig, reply_id: str) -> None:
    """After the approval word: the page's prompt audio stopped, the reply for
    ``reply_id`` is open again, and the resumed turn's text goes into a new TTS request."""
    kinds = [f["type"] for f in rig.page.frames]
    word = len(kinds) - 1 - kinds[::-1].index("utterance")  # the frame with the approval word
    assert rig.page.frames[word]["text"] == "yes"
    assert kinds.index("clear") < kinds.index("turn_done") < word
    assert rig.session.reply_id == reply_id and rig.session.reply_open
    assert rig.events[-1].text == "yes" and rig.events[-1].message_id != reply_id
    first_request = rig.conn.json_frames("tts_stream")[0]["request_id"]
    await rig.session.on_reply_delta(reply_id, "t1", 2, "Done. ")
    await wait_for(lambda: rig.tts_text() == "Let me check. Done. ")
    assert rig.conn.json_frames("tts_stream")[-1]["request_id"] != first_request


async def test_an_approval_word_over_a_running_reply_reopens_it_for_the_resumed_turn(rig):
    reply_id = await _reply_in_progress(rig)
    rig.wants(True)
    await _heard(rig, "Yes.")
    await wait_for(lambda: rig.page.of("utterance")[-1:] == [{"type": "utterance", "text": "yes"}])
    await _resumed_turn_is_spoken(rig, reply_id)


async def test_an_approval_word_after_a_barge_in_reopens_the_reply_for_the_resumed_turn(rig):
    reply_id = await _reply_in_progress(rig)
    rid = rig.conn.json_frames("tts_stream")[0]["request_id"]
    await rig.conn.send({"type": "barge", "request_id": rid, "onset_mic_sample": 10, "mic_sample": 20})
    await wait_for(lambda: rig.page.of("clear"))
    assert not rig.session.reply_open
    rig.wants(True)
    await _heard(rig, "yes")
    await wait_for(lambda: rig.page.of("utterance")[-1:] == [{"type": "utterance", "text": "yes"}])
    await _resumed_turn_is_spoken(rig, reply_id)


async def test_an_approval_word_with_nothing_pending_is_an_ordinary_utterance(rig):
    reply_id = await _reply_in_progress(rig)
    rig.wants(False)
    await _heard(rig, "Yes.")
    assert rig.events[-1].text == "Yes."
    assert rig.session.reply_id != reply_id
    assert rig.events[-1].message_id == rig.session.reply_id
    await rig.session.on_reply_delta(reply_id, "t1", 2, "Done. ")
    await asyncio.sleep(0.1)
    assert rig.tts_text() == "Let me check. "  # the old reply takes no more text


@pytest.mark.parametrize("said", ["Yes, but change the path first.", "No, use the other directory for all of them."])
async def test_a_longer_utterance_is_an_ordinary_utterance_even_with_an_approval_pending(rig, said):
    reply_id = await _reply_in_progress(rig)
    rig.wants(True)
    await _heard(rig, said)
    assert rig.events[-1].text == said
    assert rig.session.reply_id != reply_id
    assert rig.events[-1].message_id == rig.session.reply_id
    await rig.session.on_reply_delta(reply_id, "t1", 2, "Done. ")
    await asyncio.sleep(0.1)
    assert rig.tts_text() == "Let me check. "


@pytest.mark.parametrize("said, word", [
    ("Yes, please.", "yes"), ("Go ahead.", "yes"), ("Yeah.", "yes"), ("OK", "ok"), ("Do it!", "yes"),
    ("I said yes, please", "yes"), ("I said no.", "no"),
    ("No.", "no"), ("No, just leave it.", "no"), ("Nope.", "no"), ("Cancel that", "no"),
])
async def test_a_spoken_approve_or_deny_phrase_answers_the_pending_approval(rig, said, word):
    reply_id = await _reply_in_progress(rig)
    rig.session._approval_words = lambda: ("yes", "no", "ok")
    rig.wants(True)
    await _heard(rig, said)
    assert rig.events[-1].text == word and rig.events[-1].message_id != reply_id
    assert rig.session.reply_id == reply_id and rig.session.reply_open  # the blocked turn's reply
    await wait_for(lambda: rig.page.of("utterance")[-1:] == [{"type": "utterance", "text": word}])


@pytest.mark.parametrize("said", ["Go ahead.", "No, just leave it."])
async def test_an_approve_or_deny_phrase_with_nothing_pending_is_an_ordinary_utterance(rig, said):
    reply_id = await _reply_in_progress(rig)
    rig.wants(False)
    await _heard(rig, said)
    assert rig.events[-1].text == said
    assert rig.session.reply_id != reply_id
    assert rig.events[-1].message_id == rig.session.reply_id


def test_which_utterances_answer_an_approval(plugin):
    answer = plugin.turns.spoken_approval
    assert answer("Yes please") == "yes" and answer("yep") == "yes" and answer("Sure.") == "yes"
    assert answer("yes but wait") is None and answer("sure thing do whatever you like") is None
    assert answer("No thanks") == "no" and answer("no no no no no") == "no"
    assert answer("no I do not want that") is None  # six words: a message
    assert answer("I say no") is None and answer("") is None
    for said in ["I said yes", "I said yes, please.", "I approve", "Go for it.", "Run it", "Yeah, go ahead",
                 "Yes, yes.", "Please do", "That's fine"]:
        assert answer(said) == "yes", said
    for said in ["I said no", "Don't.", "Do not do it", "Stop"]:
        assert answer(said) == "no", said
    assert answer("I said yes but wait") is None and answer("I said") is None


async def test_a_fresh_page_with_an_approval_pending_takes_the_resumed_turn_under_any_id(rig):
    """A page that reconnected while the turn was blocked never saw the utterance."""
    assert rig.session.reply_id is None
    rig.wants(True)
    await _heard(rig, "yes")
    assert rig.session.reply_open and rig.session.reply_id is None
    assert rig.events[-1].text == "yes"
    await rig.session.on_reply_delta("someid", "t1", 2, "Done. ")
    await wait_for(lambda: rig.tts_text() == "Done. ")


async def test_a_notice_is_spoken_at_once_through_the_running_reply(rig):
    await _reply_in_progress(rig)
    await rig.session.speak_notice("Approval needed to run: ls. Say yes or no.")
    await wait_for(lambda: rig.tts_text().endswith("Say yes or no."))
    frames = rig.conn.json_frames("tts_stream")
    assert {f["request_id"] for f in frames} == {frames[0]["request_id"]}  # the reply's own request
    assert not any(f["done"] for f in frames)  # the reply goes on


async def test_a_notice_does_not_run_into_the_word_the_reply_holds(rig):
    reply_id = await _reply_in_progress(rig)
    await rig.session.on_reply_delta(reply_id, "t1", 1, "that.")  # no space: the word waits
    await rig.session.speak_notice("Approval needed to run: ls. Say yes or no.")
    await wait_for(lambda: rig.tts_text().endswith("Say yes or no."))
    chunks = [f["text"] for f in rig.conn.json_frames("tts_stream")]
    assert "that." in chunks  # sent on its own, before the notice
    assert all(not c.startswith("that.Approval") for c in chunks)
    assert rig.tts_text() == "Let me check. that.Approval needed to run: ls. Say yes or no."


async def test_a_notice_with_no_reply_open_is_still_spoken(rig):
    assert not rig.session.reply_open
    await rig.session.speak_notice("Approval needed to run: ls. Say yes or no.")
    await wait_for(lambda: rig.tts_text() == "Approval needed to run: ls. Say yes or no.")


def _requests(rig) -> list[str]:
    """The ids of the TTS requests so far, in the order they were opened."""
    return list(dict.fromkeys(f["request_id"] for f in rig.conn.json_frames("tts_stream")))


def _ended(rig, request_id: str) -> bool:
    return any(f["request_id"] == request_id and f["done"] for f in rig.conn.json_frames("tts_stream"))


async def test_a_notice_with_no_reply_open_ends_its_own_request(rig):
    assert not rig.session.reply_open
    await rig.session.speak_notice("Approval needed to run: ls. Say yes or no.")
    await wait_for(lambda: _requests(rig) and _ended(rig, _requests(rig)[0]))
    assert rig.session.reply_request is None  # nothing is left open for a reply to drop
    rid = _requests(rig)[0]
    await rig.conn.send({"type": "audio_end", "request_id": rid, "reason": "completed"})
    await wait_for(lambda: rig.page.of("audio_end"))
    await asyncio.sleep(0.1)
    assert rig.page.of("turn_done") == []  # the notice's audio end is no reply's end
    assert rig.session._notice_requests == {}


async def test_two_notices_each_keep_their_id_until_their_own_audio_end(rig):
    await rig.session.notice("New session")
    await rig.session.notice("Could not start a new session.")
    await wait_for(lambda: len(_requests(rig)) == 2 and all(_ended(rig, r) for r in _requests(rig)))
    first, second = _requests(rig)
    assert set(rig.session._notice_requests) == {first, second}
    await rig.conn.send({"type": "audio_end", "request_id": first, "reason": "completed"})
    await wait_for(lambda: len(rig.page.of("audio_end")) == 1)
    assert set(rig.session._notice_requests) == {second}
    await rig.conn.send({"type": "audio_end", "request_id": second, "reason": "completed"})
    await wait_for(lambda: len(rig.page.of("audio_end")) == 2)
    await asyncio.sleep(0.1)
    assert rig.session._notice_requests == {}
    assert rig.page.of("turn_done") == []


async def _prompted(rig, command: str = "rm -rf build") -> None:
    """The gateway asks for an approval: the adapter speaks the prompt on the page."""
    spoken = len(rig.tts_text())
    rig.wants(True)  # the gateway queues the approval before it sends the prompt
    result = await rig.adapter.send_exec_approval(chat_id="voice", command=command, session_key="k",
                                                  description="deletes files", request_id="r1")
    assert result.success
    await wait_for(lambda: rig.tts_text()[spoken:].endswith("Say yes or no."))


async def test_an_approval_word_reopens_the_reply_that_raised_the_prompt_not_a_newer_one(rig):
    """The gateway did not interrupt the blocked turn for the utterance in between
    (it steers or queues it), so the approval still waits and the turn resumes under
    its first message id."""
    blocked = await _reply_in_progress(rig)
    await _prompted(rig)
    await _heard(rig, "wait what does that do")
    newer = rig.session.reply_id
    assert newer != blocked
    await _heard(rig, "yes")
    assert rig.session.reply_id == blocked and rig.session.reply_open
    spoken = len(rig.tts_text())
    await rig.session.on_reply_delta(blocked, "t1", 2, "Done. ")
    await wait_for(lambda: rig.tts_text()[spoken:] == "Done. ")
    await rig.session.on_reply_delta(newer, "t2", 1, "It deletes the build. ")
    await asyncio.sleep(0.1)
    assert rig.tts_text()[spoken:] == "Done. "
    assert [f["text"] for f in rig.page.of("reply_delta")][-1] == "Done. "


async def test_the_prompt_reply_is_forgotten_once_the_word_used_it(rig):
    blocked = await _reply_in_progress(rig)
    await _prompted(rig)
    await _heard(rig, "yes")
    assert rig.session._prompt_reply is None
    rig.wants(False)
    await _heard(rig, "tell me more")
    later = rig.session.reply_id
    assert later not in (None, blocked) and rig.events[-1].message_id == later
    spoken = len(rig.tts_text())
    await rig.session.on_reply_delta(later, "t2", 1, "Sure. ")
    await wait_for(lambda: rig.tts_text()[spoken:] == "Sure. ")


async def test_an_approval_word_with_the_approval_gone_is_an_ordinary_utterance_and_forgets_the_prompt(rig):
    blocked = await _reply_in_progress(rig)
    await _prompted(rig)
    rig.wants(False)  # answered on another device, or timed out
    await _heard(rig, "yes")
    assert rig.session._prompt_reply is None
    assert rig.session.reply_id not in (None, blocked)
    assert rig.events[-1].message_id == rig.session.reply_id


async def test_a_prompt_on_a_fresh_page_leaves_no_request_open_when_the_reply_reopens(rig):
    """A page that reconnected while the turn was blocked: no reply is open for the prompt."""
    assert rig.session.reply_id is None and not rig.session.reply_open
    await _prompted(rig)
    (prompt_request,) = _requests(rig)
    await wait_for(lambda: _ended(rig, prompt_request))
    assert rig.session.reply_request is None
    await _heard(rig, "yes")
    assert rig.session.reply_open and rig.session.reply_id is None
    await rig.session.on_reply_delta("someid", "t1", 2, "Done. ")
    await wait_for(lambda: rig.tts_text().endswith("Done. "))
    assert rig.conn.json_frames("tts_stream")[-1]["request_id"] != prompt_request



@pytest.mark.parametrize("part", ["_approval_pending", "_approval_words"])
async def test_a_failing_approval_check_leaves_the_word_an_ordinary_utterance(rig, caplog, part):
    async def pending() -> bool:
        raise RuntimeError("no session key")

    def words() -> frozenset:
        raise RuntimeError("no gateway")

    setattr(rig.session, part, pending if part == "_approval_pending" else words)
    with caplog.at_level("WARNING"):
        await _heard(rig, "yes")
    assert rig.events[-1].text == "yes"
    assert rig.events[-1].message_id == rig.session.reply_id is not None
    assert any("approval check failed" in r.getMessage() for r in caplog.records)
    await _heard(rig, "are you there")  # the session still runs
    assert rig.events[-1].text == "are you there"


async def test_a_notice_leaves_a_finished_reply_that_still_plays_its_own_request(rig):
    reply_id = await _reply_in_progress(rig)
    await rig.session._end_reply(reply_id)  # the text is over; its audio still plays
    reply_request = rig.session.reply_request
    assert reply_request is not None and not rig.session.reply_open
    await rig.session.speak_notice("Approval needed to run: ls. Say yes or no.")
    assert rig.session.reply_request is reply_request
    await wait_for(lambda: len(_requests(rig)) == 2)
    notice = next(r for r in _requests(rig) if r != reply_request.request_id)
    await wait_for(lambda: _ended(rig, notice))
    await rig.conn.send({"type": "audio_end", "request_id": reply_request.request_id, "reason": "completed"})
    await wait_for(lambda: rig.page.of("turn_done"))  # the reply's own audio end ends the turn
    assert rig.session.reply_request is None



SENTENCE = "Approval needed to run rm. Say yes or no."


async def _prompt_open(rig, request_id: str = "r1") -> None:
    """The gateway asked for an approval and the page shows the prompt."""
    rig.wants(True)
    await rig.session.open_approval(request_id, SENTENCE, {"type": "approval", "request_id": request_id})
    await wait_for(lambda: rig.page.of("approval"))


async def test_the_prompt_ends_on_the_page_when_the_blocked_tool_ends_with_nothing_pending(rig):
    reply_id = await _reply_in_progress(rig)
    await _prompt_open(rig)
    await rig.session.on_tool_end(reply_id, "c0", "ok", 5)  # another tool; the approval still waits
    await asyncio.sleep(0.05)
    assert not rig.page.of("approval_end")
    rig.wants(False)  # timed out
    await rig.session.on_tool_end(reply_id, "c1", "error", 60000)
    await wait_for(lambda: rig.page.of("approval_end"))
    await rig.session.on_tool_end(reply_id, "c2", "ok", 5)
    await rig.session.on_turn_done(reply_id, "t1", False)
    await asyncio.sleep(0.05)
    assert rig.page.of("approval_end") == [{"type": "approval_end", "request_id": "r1"}]


async def test_an_answer_ends_the_prompt_on_the_page(rig):
    await _reply_in_progress(rig)
    await _prompt_open(rig)
    await _heard(rig, "No, just leave it.")
    assert rig.events[-1].text == "no"
    await wait_for(lambda: rig.page.of("approval_end"))
    kinds = [f["type"] for f in rig.page.frames]
    assert kinds.index("approval_end") < len(kinds) - 1 - kinds[::-1].index("utterance")


async def test_an_utterance_that_is_no_answer_withdraws_the_approval_and_the_prompt_ends(rig):
    """What the gateway does for "You ask me to say yes or no, and I say no": the
    utterance is a new message, the blocked turn is interrupted and its approval is
    withdrawn. The page must lose the prompt and take the new turn."""
    blocked = await _reply_in_progress(rig)
    await rig.session.on_tool_start(blocked, "c1", "terminal", "rm -rf build")
    await _prompt_open(rig)
    said = "You ask me to say yes or no, and I say no"
    await _heard(rig, said)
    assert rig.events[-1].text == said
    newer = rig.events[-1].message_id
    assert newer != blocked and rig.session.reply_id == newer
    assert not rig.page.of("approval_end")  # the gateway has not withdrawn it yet
    rig.wants(False)
    await rig.session.on_tool_end(blocked, "c1", "error", 59000)  # the blocked tool returns
    await rig.session.on_turn_done(blocked, "t1", True)
    await wait_for(lambda: rig.page.of("approval_end"))
    assert not [f for f in rig.page.of("tool_end") if f["id"] == "c1"]  # the old turn's line is not ended
    await rig.session.on_turn_start(newer)
    await rig.session.on_reply_delta(newer, "t2", 1, "Understood. ")
    await wait_for(lambda: rig.page.of("reply_delta")[-1:] == [{"type": "reply_delta", "text": "Understood. "}])
    kinds = [f["type"] for f in rig.page.frames]
    last_utterance = len(kinds) - 1 - kinds[::-1].index("utterance")
    assert "turn_done" in kinds[:last_utterance]  # the blocked turn's lines fold before the new turn
    assert "turn_done" not in kinds[last_utterance:]  # and the new turn still runs


async def test_the_spoken_sentence_of_a_prompt_is_not_shown_as_reply_text(rig):
    await _reply_in_progress(rig)
    await _prompt_open(rig)
    await wait_for(lambda: rig.tts_text().endswith("Say yes or no."))
    assert not any("Approval needed" in f.get("text", "") for f in rig.page.of("reply_delta"))


async def _tap(rig, choice: str, request_id: str = "r1") -> None:
    await rig.page.ws.send(json.dumps({"type": "approval_answer", "request_id": request_id, "choice": choice}))


@pytest.mark.parametrize("choice, word", [("deny", "no"), ("once", "yes"), ("session", "session"), ("always", "always")])
async def test_a_tap_on_a_choice_answers_the_open_prompt(rig, choice, word):
    reply_id = await _reply_in_progress(rig)
    await _prompt_open(rig)
    count = len(rig.events)
    await _tap(rig, choice)
    await wait_for(lambda: len(rig.events) > count)
    assert rig.events[-1].text == word and rig.events[-1].message_id != reply_id
    assert rig.session.reply_id == reply_id and rig.session.reply_open
    await wait_for(lambda: rig.page.of("utterance")[-1:] == [{"type": "utterance", "text": word}])
    assert rig.page.of("approval_end") == [{"type": "approval_end", "request_id": "r1"}]
    kinds = [f["type"] for f in rig.page.frames]
    assert kinds.index("approval") < kinds.index("clear")  # the prompt's audio stops on the tap
    await rig.session.on_reply_delta(reply_id, "t1", 2, "Done. ")  # the blocked turn goes on
    await wait_for(lambda: rig.tts_text().endswith("Done. "))


async def test_a_tap_for_another_prompt_or_an_unknown_choice_sends_nothing(rig):
    await _reply_in_progress(rig)
    await _prompt_open(rig)
    count = len(rig.events)
    await _tap(rig, "once", request_id="r0")
    await _tap(rig, "maybe")
    await rig.page.ws.send(json.dumps({"type": "approval_answer"}))
    await asyncio.sleep(0.1)
    assert len(rig.events) == count and not rig.page.of("approval_end")


async def test_a_tap_with_no_approval_pending_sends_nothing_and_ends_the_prompt(rig):
    await _reply_in_progress(rig)
    await _prompt_open(rig)
    rig.wants(False)  # timed out before the tap
    count = len(rig.events)
    await _tap(rig, "once")
    await wait_for(lambda: rig.page.of("approval_end"))
    await _tap(rig, "once")  # no prompt is open any more
    await asyncio.sleep(0.1)
    assert len(rig.events) == count


async def test_the_reply_stream_ends_after_a_spoken_prompt_and_the_resumed_turn_gets_a_new_one(rig):
    """A stream left open while Hermes waits keeps the page from reporting the end of
    its audio, and VoiceManager then holds the mic for the whole wait."""
    reply_id = await _reply_in_progress(rig)
    first = rig.conn.json_frames("tts_stream")[0]["request_id"]
    await _prompt_open(rig)
    await wait_for(lambda: rig.conn.json_frames("tts_stream")[-1]["done"])
    frames = rig.conn.json_frames("tts_stream")
    assert {f["request_id"] for f in frames} == {first}  # the sentence went through the reply's request
    assert rig.tts_text().endswith(SENTENCE)
    assert rig.session.reply_open and rig.session.reply_request is None
    # The end of the prompt's audio is not the end of the turn.
    done = len(rig.page.of("turn_done"))
    await rig.conn.send({"type": "audio_end", "request_id": first, "reason": "complete"})
    await wait_for(lambda: rig.page.of("audio_end"))
    await asyncio.sleep(0.1)
    assert len(rig.page.of("turn_done")) == done and rig.session.reply_open
    # Text of the turn that goes on is spoken through a new request.
    await rig.session.on_reply_delta(reply_id, "t1", 2, "Done. ")
    await wait_for(lambda: rig.conn.json_frames("tts_stream")[-1]["text"] == "Done. ")
    assert rig.conn.json_frames("tts_stream")[-1]["request_id"] != first


async def test_a_tap_while_the_prompt_still_plays_stops_the_prompt_audio(rig):
    await _reply_in_progress(rig)
    first = rig.conn.json_frames("tts_stream")[0]["request_id"]
    await _prompt_open(rig)
    await wait_for(lambda: rig.conn.json_frames("tts_stream")[-1]["done"])
    await _tap(rig, "deny")
    await wait_for(lambda: rig.conn.json_frames("tts_cancel"))
    assert [f["request_id"] for f in rig.conn.json_frames("tts_cancel")] == [first]


async def test_a_tap_after_the_prompt_audio_ended_cancels_nothing(rig):
    await _reply_in_progress(rig)
    first = rig.conn.json_frames("tts_stream")[0]["request_id"]
    await _prompt_open(rig)
    await wait_for(lambda: rig.conn.json_frames("tts_stream")[-1]["done"])
    await rig.conn.send({"type": "audio_end", "request_id": first, "reason": "complete"})
    await wait_for(lambda: rig.page.of("audio_end"))
    count = len(rig.events)
    await _tap(rig, "deny")
    await wait_for(lambda: len(rig.events) > count)
    assert not rig.conn.json_frames("tts_cancel")


@pytest.mark.parametrize("said", ["Yet please", "By answered.", "Hmm"])
async def test_a_short_utterance_that_is_no_answer_keeps_the_approval_and_is_asked_again(rig, said):
    reply_id = await _reply_in_progress(rig)
    await _prompt_open(rig)
    count = len(rig.events)
    await rig.conn.send({"type": "turn_end", "text": said, "turn_id": 0})
    await wait_for(lambda: rig.page.of("note"))
    assert said.strip() in rig.page.of("note")[-1]["text"]
    await wait_for(lambda: rig.tts_text().endswith("Say yes or no.Say yes or no.") or
                   rig.tts_text().endswith(SENTENCE + "Say yes or no."))
    await asyncio.sleep(0.1)
    assert len(rig.events) == count  # nothing reached the gateway
    assert not rig.page.of("utterance")[1:] and not rig.page.of("approval_end")
    last = rig.conn.json_frames("tts_stream")[-1]
    assert last["done"]  # the notice leaves no stream open while the wait goes on
    # The answer that follows still resumes the blocked turn's reply.
    await _heard(rig, "yes")
    assert rig.events[-1].text == "yes" and rig.session.reply_id == reply_id and rig.session.reply_open


async def test_a_longer_utterance_that_is_no_answer_still_goes_to_hermes(rig):
    await _reply_in_progress(rig)
    await _prompt_open(rig)
    await _heard(rig, "what does that command do")
    assert rig.events[-1].text == "what does that command do"
    assert not rig.page.of("note")


async def test_a_short_utterance_with_nothing_pending_is_an_ordinary_utterance(rig):
    await _reply_in_progress(rig)
    rig.wants(False)
    await _heard(rig, "Yet please")
    assert rig.events[-1].text == "Yet please" and not rig.page.of("note")


@pytest.mark.parametrize("asked_again", [False, True])
async def test_a_new_utterance_cancels_the_resumed_reply_after_a_tap(rig, asked_again):
    """The reply that a tap reopened is a running reply like any other: the next
    utterance stops it, also when a misheard answer was asked again before the tap."""
    reply_id = await _reply_in_progress(rig)
    await _prompt_open(rig)
    if asked_again:
        await rig.conn.send({"type": "turn_end", "text": "Yet please", "turn_id": 0})
        await wait_for(lambda: rig.page.of("note"))
    count = len(rig.events)
    await _tap(rig, "once")
    await wait_for(lambda: len(rig.events) > count)
    await rig.session.on_reply_delta(reply_id, "t1", 2, "Done. ")
    await wait_for(lambda: rig.tts_text().endswith("Done. "))
    resumed = rig.conn.json_frames("tts_stream")[-1]["request_id"]
    clears, dones = len(rig.page.of("clear")), len(rig.page.of("turn_done"))
    rig.wants(False)
    await _heard(rig, "tell me more about it")
    assert resumed in [f["request_id"] for f in rig.conn.json_frames("tts_cancel")]
    assert len(rig.page.of("clear")) > clears and len(rig.page.of("turn_done")) > dones


SECOND = "Approval needed to run dd. Say yes or no."


async def _second_prompt_open(rig) -> None:
    """A second approval of the chat asks while the first still waits."""
    await rig.session.open_approval("r2", SECOND, {"type": "approval", "request_id": "r2"})
    await asyncio.sleep(0.05)


@pytest.mark.parametrize("by", ["tap", "voice"])
async def test_a_second_prompt_waits_and_the_answer_is_for_the_oldest_one(rig, by):
    """The gateway gives an answer to the oldest approval: the page shows that one."""
    reply_id = await _reply_in_progress(rig)
    await _prompt_open(rig)
    await _second_prompt_open(rig)
    assert [f["request_id"] for f in rig.page.of("approval")] == ["r1"]  # the card keeps the first
    assert SECOND not in rig.tts_text()
    count = len(rig.events)
    await _tap(rig, "once", request_id="r2")  # no card shows r2: nothing happens
    await asyncio.sleep(0.1)
    assert len(rig.events) == count
    if by == "tap":
        await _tap(rig, "once")
        await wait_for(lambda: len(rig.events) > count)
    else:
        await _heard(rig, "yes")
    assert rig.events[-1].text == "yes"
    # The first prompt ends, then the second is shown and spoken, in the blocked turn's reply.
    await wait_for(lambda: [f["request_id"] for f in rig.page.of("approval")] == ["r1", "r2"])
    assert rig.page.of("approval_end") == [{"type": "approval_end", "request_id": "r1"}]
    kinds = [(f["type"], f.get("request_id")) for f in rig.page.frames]
    assert kinds.index(("approval_end", "r1")) < kinds.index(("approval", "r2"))
    await wait_for(lambda: rig.tts_text().endswith(SECOND))
    assert rig.session.reply_id == reply_id and rig.session._prompt_reply == (reply_id,)
    await _tap(rig, "deny", request_id="r2")
    await wait_for(lambda: rig.events[-1].text == "no")
    await wait_for(lambda: len(rig.page.of("approval_end")) == 2)


async def test_the_second_prompt_is_forgotten_when_the_chat_waits_for_no_approval(rig):
    reply_id = await _reply_in_progress(rig)
    await _prompt_open(rig)
    await _second_prompt_open(rig)
    rig.wants(False)  # both were withdrawn
    await rig.session.on_tool_end(reply_id, "c1", "error", 100)
    await wait_for(lambda: rig.page.of("approval_end"))
    await asyncio.sleep(0.05)
    assert [f["request_id"] for f in rig.page.of("approval")] == ["r1"]
    assert not rig.session._approval_prompts and rig.session._approval_id is None


async def _two_prompts(rig) -> str:
    """Two approvals wait in the gateway's queue, r1 the oldest, and the page shows r1."""
    reply_id = await _reply_in_progress(rig)
    rig.oldest = "r1"
    await _prompt_open(rig)
    await _second_prompt_open(rig)
    assert [f["request_id"] for f in rig.page.of("approval")] == ["r1"]
    return reply_id


async def test_the_card_moves_when_the_oldest_approval_leaves_the_queue_unanswered(rig):
    """The shown approval timed out, or was answered elsewhere, while a newer one
    still waits: the gateway now gives an answer to the newer one."""
    reply_id = await _two_prompts(rig)
    rig.oldest = "r2"
    await rig.session.on_tool_end(reply_id, "c1", "error", 60000)
    await wait_for(lambda: [f["request_id"] for f in rig.page.of("approval")] == ["r1", "r2"])
    assert rig.page.of("approval_end") == [{"type": "approval_end", "request_id": "r1"}]
    await wait_for(lambda: rig.tts_text().endswith(SECOND))


@pytest.mark.parametrize("by", ["tap", "voice"])
async def test_an_answer_for_the_oldest_approval_is_not_sent_once_the_queue_moved_on(rig, by):
    """No hook told the session yet that r1 is gone. The answer for the r1 card must
    not reach the gateway: it would resolve r2, a command the page never showed."""
    await _two_prompts(rig)
    rig.oldest = "r2"
    count = len(rig.events)
    if by == "tap":
        await _tap(rig, "once")
    else:
        await rig.conn.send({"type": "turn_end", "text": "yes", "turn_id": 0})
    await wait_for(lambda: [f["request_id"] for f in rig.page.of("approval")] == ["r1", "r2"])
    await asyncio.sleep(0.1)
    assert len(rig.events) == count  # nothing was sent
    assert rig.session._approval_id == "r2"
    # The answer the user gives now is for r2, which the page shows.
    await _tap(rig, "deny", request_id="r2")
    await wait_for(lambda: len(rig.events) > count)
    assert rig.events[-1].text == "no"


async def test_a_prompt_that_arrives_before_the_oldest_one_waits_for_it_in_queue_order(rig):
    """Two tools ask at once and the newer approval's prompt is delivered first."""
    await _reply_in_progress(rig)
    rig.wants(True)
    rig.oldest = "r1"
    await _second_prompt_open(rig)  # r2 comes first
    assert not rig.page.of("approval") and SECOND not in rig.tts_text()
    count = len(rig.events)
    await _tap(rig, "once", request_id="r2")
    await asyncio.sleep(0.05)
    assert len(rig.events) == count
    await rig.session.open_approval("r1", SENTENCE, {"type": "approval", "request_id": "r1"})
    await wait_for(lambda: rig.page.of("approval"))
    assert [f["request_id"] for f in rig.page.of("approval")] == ["r1"]
    # r1 is answered and the gateway's oldest is then r2: its kept prompt is shown.
    await _tap(rig, "once")
    await wait_for(lambda: len(rig.events) > count)
    rig.oldest = "r2"
    await rig.session.on_tool_end("any", "c1", "ok", 10)
    await wait_for(lambda: [f["request_id"] for f in rig.page.of("approval")] == ["r1", "r2"])


async def test_the_card_closes_when_the_queue_of_approvals_is_empty(rig):
    reply_id = await _two_prompts(rig)
    rig.wants(False)
    rig.oldest = None
    await rig.session.on_turn_done(reply_id, "t1", True)
    await wait_for(lambda: rig.page.of("approval_end"))
    assert rig.session._approval_id is None and not rig.session._approval_prompts
