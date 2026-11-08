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
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=phone") as ws:
                page = Page(ws)
                await wait_for(lambda: page.of("ready"))
                await ws.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                session = adapter.session_for("phone")
                rig = Rig(adapter, session, page, conn, events)

                async def pending() -> bool:
                    return rig.pending

                session._approval_words = lambda: APPROVAL_WORDS
                session._approval_pending = pending
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


async def test_more_than_one_word_is_an_ordinary_utterance_even_with_an_approval_pending(rig):
    reply_id = await _reply_in_progress(rig)
    rig.wants(True)
    await _heard(rig, "Yes please.")
    assert rig.events[-1].text == "Yes please."
    assert rig.session.reply_id != reply_id
    assert rig.events[-1].message_id == rig.session.reply_id
    await rig.session.on_reply_delta(reply_id, "t1", 2, "Done. ")
    await asyncio.sleep(0.1)
    assert rig.tts_text() == "Let me check. "


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
