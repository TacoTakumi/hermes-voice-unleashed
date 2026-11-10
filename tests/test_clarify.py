"""A question the model asks with the clarify tool is spoken, and the turn that
resumes after the spoken answer is spoken too."""

from __future__ import annotations

import asyncio

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
