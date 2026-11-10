"""A question the model asks with the clarify tool is spoken, and the turn that
resumes after the spoken answer is spoken too."""

from __future__ import annotations

import asyncio

import pytest

from tools import clarify_gateway

from fake_vm import wait_for
from test_approvals import _heard, _reply_in_progress, rig  # noqa: F401  (rig is a fixture)

QUESTION = "What are we testing this cycle?"


def _waits_for_an_answer(rig, waiting: bool = True) -> None:
    """Whether the chat waits for the answer to a question right now."""
    async def pending() -> bool:
        return waiting

    rig.session._clarify_pending = pending


async def _asked(rig) -> str:
    """A reply runs and its turn asks a question, which is spoken. Returns the reply's id."""
    reply_id = await _reply_in_progress(rig)
    await rig.session.speak_prompt(QUESTION)
    await wait_for(lambda: rig.tts_text().endswith(QUESTION))
    _waits_for_an_answer(rig)
    return reply_id


async def test_an_utterance_that_answers_a_question_reopens_the_reply_that_asked(rig):
    asked = await _asked(rig)
    await _heard(rig, "the voice path please")
    assert rig.events[-1].text == "the voice path please"  # whole, not cut to a word
    assert rig.events[-1].message_id != asked  # under a message id of its own
    assert rig.session.reply_id == asked and rig.session.reply_open
    assert rig.page.of("utterance")[-1]["text"] == "the voice path please"
    spoken = len(rig.tts_text())
    await rig.session.on_reply_delta(asked, "t1", 2, "Standing by. ")
    await wait_for(lambda: rig.tts_text()[spoken:] == "Standing by. ")
    assert [f["text"] for f in rig.page.of("reply_delta")][-1] == "Standing by. "


async def test_an_utterance_with_no_question_waiting_is_an_ordinary_one(rig):
    asked = await _asked(rig)
    _waits_for_an_answer(rig, False)  # answered on another device, or timed out
    await _heard(rig, "never mind, tell me a joke")
    assert rig.session.reply_id not in (None, asked)
    assert rig.events[-1].message_id == rig.session.reply_id
    spoken = len(rig.tts_text())
    await rig.session.on_reply_delta(asked, "t1", 2, "Standing by. ")
    await asyncio.sleep(0.1)
    assert rig.tts_text()[spoken:] == ""  # the older reply takes no more text


async def test_a_failing_question_check_leaves_the_utterance_an_ordinary_one(rig, caplog):
    asked = await _asked(rig)

    async def failing() -> bool:
        raise RuntimeError("no session key")

    rig.session._clarify_pending = failing
    with caplog.at_level("WARNING"):
        await _heard(rig, "the voice path please")
    assert rig.events[-1].text == "the voice path please"
    assert rig.events[-1].message_id == rig.session.reply_id != asked
    assert any("question check failed" in r.getMessage() for r in caplog.records)


def test_the_spoken_question_reads_the_choices_with_their_numbers(plugin):
    sentence = plugin.adapter.clarify_sentence
    assert sentence("What are we  testing\nthis cycle?") == "What are we testing this cycle?"
    assert sentence("Pick a colour", ["Red", "Blue."]) == (
        "Pick a colour. One: Red. Two: Blue. Say the number or your answer.")
    many = sentence("Which?", [f"c{i}" for i in range(1, 11)])
    assert "Nine: c9. 10: c10." in many


@pytest.fixture
def clarify(rig):
    """Registers a waiting question for the page's chat, as the clarify tool does."""
    key = rig.adapter._source_session_key(rig.adapter._voice_source("voice"))

    def register(question, choices=None, clarify_id="q1"):
        return clarify_gateway.register(clarify_id, key, question, choices), key

    yield register
    clarify_gateway.clear_session(key)


async def test_an_open_question_is_shown_and_spoken(rig, clarify):
    reply_id = await _reply_in_progress(rig)
    _, key = clarify(QUESTION)
    result = await rig.adapter.send_clarify("voice", QUESTION, None, "q1", key)
    assert result.success
    await wait_for(lambda: rig.page.of("reply"))
    assert QUESTION in rig.page.of("reply")[-1]["text"]
    await wait_for(lambda: rig.tts_text().endswith(QUESTION))
    assert rig.session._prompt_reply == (reply_id,)  # the reply that asked is remembered
    assert await rig.session._clarify_pending() is True  # wired to the gateway's waiting questions


async def test_a_question_with_choices_speaks_them_and_keeps_the_text_answer_armed(rig, clarify):
    await _reply_in_progress(rig)
    entry, key = clarify("Pick a colour", ["Red", "Blue"])
    assert not entry.awaiting_text
    result = await rig.adapter.send_clarify("voice", "Pick a colour", ["Red", "Blue"], "q1", key)
    assert result.success
    await wait_for(lambda: rig.tts_text().endswith("Say the number or your answer."))
    assert "Pick a colour. One: Red. Two: Blue. Say the number or your answer." in rig.tts_text()
    shown = rig.page.of("reply")[-1]["text"]
    assert "Red" in shown and "Blue" in shown
    assert entry.awaiting_text  # the next utterance is taken as the answer


async def test_a_spoken_answer_to_a_registered_question_resumes_the_turn_that_asked(rig, clarify):
    reply_id = await _reply_in_progress(rig)
    _, key = clarify(QUESTION)
    await rig.adapter.send_clarify("voice", QUESTION, None, "q1", key)
    await wait_for(lambda: rig.tts_text().endswith(QUESTION))
    await _heard(rig, "the voice path please")
    assert rig.session.reply_id == reply_id and rig.session.reply_open
    assert rig.events[-1].text == "the voice path please"


async def test_a_question_for_a_chat_with_no_page_is_not_spoken(rig, clarify):
    spoken = rig.tts_text()
    result = await rig.adapter.send_clarify("tablet", QUESTION, None, "q9", "some-key")
    assert not result.success  # the gateway default found no page to send to
    await asyncio.sleep(0.1)
    assert rig.tts_text() == spoken
    assert await rig.session._clarify_pending() is False

