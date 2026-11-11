"""A question the model asks with the clarify tool is spoken, and the turn that
resumes after the spoken answer is spoken too."""

from __future__ import annotations

import asyncio
import json

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
    # The answer resumes the turn that asked: the page keeps that turn's reasoning and clock.
    assert rig.page.of("utterance")[-1] == {"type": "utterance", "text": "the voice path please", "resumed": True}
    spoken = len(rig.tts_text())
    await rig.session.on_reply_delta(asked, "t1", 2, "Standing by. ")
    await wait_for(lambda: rig.tts_text()[spoken:] == "Standing by. ")
    assert [f["text"] for f in rig.page.of("reply_delta")][-1] == "Standing by. "


async def test_an_utterance_with_no_question_waiting_is_an_ordinary_one(rig):
    asked = await _asked(rig)
    _waits_for_an_answer(rig, False)  # answered on another device, or timed out
    await _heard(rig, "never mind, tell me a joke")
    assert rig.page.of("utterance")[-1] == {"type": "utterance", "text": "never mind, tell me a joke"}
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
        "Pick a colour. One: Red. Two: Blue. Say number one or number two, or your answer.")
    many = sentence("Which?", [f"c{i}" for i in range(1, 11)])
    assert "Nine: c9. 10: c10." in many
    assert sentence("Sure", ["Really?", "No!"]) == (
        "Sure. One: Really? Two: No! Say number one or number two, or your answer.")


def test_the_spoken_hint_asks_for_the_long_form_of_a_number(plugin):
    """A number said alone is the answer a speech recognizer loses most: the hint
    names the form with a word before it."""
    sentence = plugin.adapter.clarify_sentence
    assert sentence("Which?", ["a", "b", "c"]).endswith(
        " Say number one, number two or number three, or your answer.")
    assert sentence("Which?", ["a", "b", "c", "d"]).endswith(
        " Say number one, number two, number three or number four, or your answer.")
    long_hint = " Say the number of a choice, for example number one, or your answer."
    assert sentence("Which?", list("abcde")).endswith(long_hint)
    assert sentence("Which?", [f"c{i}" for i in range(1, 11)]).endswith(long_hint)
    assert "Say" not in sentence("Which one do you want?")


def test_the_typed_skip_hint_of_the_gateway_is_not_spoken(plugin):
    """The gateway hands the question over with a line for typed chats after it."""
    from agent.i18n import t
    hint = t("gateway.clarify.skip_hint")
    sentence = plugin.adapter.clarify_sentence
    assert sentence(f"What are we testing?\n{hint}") == "What are we testing?"
    assert sentence(f"Pick a colour\n{hint}", ["Red", "Blue"]) == (
        "Pick a colour. One: Red. Two: Blue. Say number one or number two, or your answer.")
    assert sentence("First line\nsecond line?") == "First line second line?"  # only the hint goes


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
    await asyncio.sleep(0.1)
    # Shown once, by the reply frame: the spoken form adds no line of its own.
    assert all(QUESTION not in f["text"] for f in rig.page.of("reply_delta"))
    assert rig.session._prompt_reply == (reply_id,)  # the reply that asked is remembered
    assert await rig.session._clarify_pending() is True  # wired to the gateway's waiting questions


async def test_a_question_with_choices_speaks_them_and_keeps_the_text_answer_armed(rig, clarify):
    await _reply_in_progress(rig)
    entry, key = clarify("Pick a colour", ["Red", "Blue"])
    assert not entry.awaiting_text
    result = await rig.adapter.send_clarify("voice", "Pick a colour", ["Red", "Blue"], "q1", key)
    assert result.success
    await wait_for(lambda: rig.tts_text().endswith("or your answer."))
    assert "Pick a colour. One: Red. Two: Blue. Say number one or number two, or your answer." in rig.tts_text()
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


COLOURS = ["Red (Recommended)", "Blue", "Green", "Black"]


@pytest.mark.parametrize("said, sent", [
    ("Four.", "4"), ("four", "4"), ("4", "4"), ("Number four.", "4"), ("Option 2", "2"),
    ("choice three please", "3"), ("The fourth one.", "4"), ("the first", "1"), ("Second option", "2"),
    ("For.", "4"), ("To", "2"), ("One", "1"),
    ("red", "Red (Recommended)"), ("Red.", "Red (Recommended)"), ("BLACK!", "Black"),
])
def test_a_spoken_number_or_choice_name_becomes_a_selection(plugin, said, sent):
    assert plugin.adapter.spoken_choice(said, COLOURS) == sent


@pytest.mark.parametrize("said", [
    "Five.", "zero", "number nine", "the dark one", "I like blue best", "for the dark one",
    "four or maybe two", "", "...",
])
def test_any_other_answer_is_left_as_it_was_heard(plugin, said):
    assert plugin.adapter.spoken_choice(said, COLOURS) is None


@pytest.mark.parametrize("said, sent", [
    ("I said one", "1"), ("I pick two.", "2"), ("Option three please", "3"), ("I'll take number four", "4"),
    ("Let us go with three", "3"), ("My answer is two", "2"), ("the answer is 1", "1"), ("I said the first", "1"),
    ("I said blue", "Blue"), ("Let's go with red", "Red (Recommended)"), ("I pick black please", "Black"),
])
def test_a_short_sentence_that_ends_in_a_number_or_a_choice_name_is_a_selection(plugin, said, sent):
    """An answer that got no text is said again as a sentence: "I said one"."""
    assert plugin.adapter.spoken_choice(said, COLOURS) == sent


@pytest.mark.parametrize("said", [
    "one more question", "the dark one", "that one", "for the dark one", "not that one",
    "one or two", "I said one or two", "I think two of them are wrong", "blue is not the one",
    "I really do not want to pick number one", "I said five", "I said purple", "not one", "not blue",
])
def test_a_sentence_that_only_holds_a_number_or_a_name_is_left_as_it_was_heard(plugin, said):
    assert plugin.adapter.spoken_choice(said, COLOURS) is None


@pytest.mark.parametrize("said, choices, sent", [
    ("I said yes", ["Yes", "No"], "Yes"),
    ("I pick OK", ["OK", "Cancel"], "OK"),
    ("I said go ahead", ["Go ahead", "Stop"], "Go ahead"),
    ("I pick the short version", ["The long version", "The short version"], "The short version"),
    ("I said the first one", ["The last one", "The first one"], "The first one"),
    ("I said the first one", ["Red", "Blue"], "1"),
])
def test_a_choice_name_that_begins_with_a_lead_in_word_is_still_found_in_a_sentence(plugin, said, choices, sent):
    assert plugin.adapter.spoken_choice(said, choices) == sent


def test_a_sentence_answer_is_at_most_five_words_long(plugin):
    """A longer utterance is a message in the user's own words, also when each of its
    words could lead into an answer."""
    assert plugin.adapter.spoken_choice("I think I pick two", COLOURS) == "2"
    assert plugin.adapter.spoken_choice("Well I think I pick two", COLOURS) is None
    assert plugin.adapter.spoken_choice("I think I pick blue please", COLOURS) == "Blue"  # please is not counted
    assert plugin.adapter.spoken_choice("Okay well I would like blue", COLOURS) is None


def test_a_choice_name_wins_over_a_number_and_an_open_question_maps_nothing(plugin):
    assert plugin.adapter.spoken_choice("Two.", ["Two", "One"]) == "Two"
    assert plugin.adapter.spoken_choice("Four.", None) is None
    assert plugin.adapter.spoken_choice("Four.", []) is None


async def test_a_spoken_number_reaches_the_gateway_as_the_digit(rig, clarify):
    reply_id = await _reply_in_progress(rig)
    _, key = clarify("Which colour?", COLOURS)
    await rig.adapter.send_clarify("voice", "Which colour?", COLOURS, "q1", key)
    await wait_for(lambda: rig.tts_text().endswith("or your answer."))
    await _heard(rig, "Four.")
    assert rig.events[-1].text == "4"  # the gateway resolves the digit to the choice
    assert rig.page.of("utterance")[-1]["text"] == "Four."  # the page shows what was said
    assert rig.session.reply_id == reply_id and rig.session.reply_open


async def test_a_free_answer_and_an_answer_to_several_choices_go_through_as_heard(rig, clarify):
    await _reply_in_progress(rig)
    _, key = clarify("Which colour?", COLOURS)
    await rig.adapter.send_clarify("voice", "Which colour?", COLOURS, "q1", key)
    await _heard(rig, "The dark one.")
    assert rig.events[-1].text == "The dark one."
    clarify_gateway.clear_session(key)
    clarify_gateway.register("q2", key, "Which colours?", COLOURS, multi_select=True)
    await _heard(rig, "Four.")
    assert rig.events[-1].text == "Four."



# --- the question card: the choices as buttons on the page -------------------------------
async def _asked_with_choices(rig, clarify, clarify_id="q1"):
    reply_id = await _reply_in_progress(rig)
    _, key = clarify("Which colour?", COLOURS, clarify_id)
    await rig.adapter.send_clarify("voice", "Which colour?", COLOURS, clarify_id, key)
    await wait_for(lambda: rig.tts_text().endswith("or your answer."))
    return reply_id


async def _tap(rig, number, request_id="q1"):
    await rig.page.ws.send(json.dumps({"type": "question_answer", "request_id": request_id, "number": number}))


async def test_a_question_with_choices_opens_a_card_with_a_button_per_choice(rig, clarify):
    await _asked_with_choices(rig, clarify)
    assert rig.page.of("question") == [{
        "type": "question", "request_id": "q1", "question": "Which colour?",
        "choices": [{"number": i + 1, "label": label} for i, label in enumerate(COLOURS)]}]
    assert rig.page.of("question_end") == []


async def test_a_question_without_choices_and_one_with_several_answers_open_no_buttons(rig, clarify):
    await _reply_in_progress(rig)
    _, key = clarify(QUESTION)
    await rig.adapter.send_clarify("voice", QUESTION, None, "q1", key)
    await wait_for(lambda: rig.page.of("question"))
    assert rig.page.of("question")[-1]["choices"] == []
    clarify_gateway.clear_session(key)
    clarify_gateway.register("q2", key, "Which colours?", COLOURS, multi_select=True)
    await rig.adapter.send_clarify("voice", "Which colours?", COLOURS, "q2", key)
    await wait_for(lambda: len(rig.page.of("question")) == 2)
    assert rig.page.of("question")[-1]["choices"] == []  # a tap takes one choice only


async def test_a_tap_on_a_choice_answers_the_question_as_its_number_does(rig, clarify):
    reply_id = await _asked_with_choices(rig, clarify)
    sent = len(rig.events)
    await _tap(rig, 2)
    await wait_for(lambda: len(rig.events) == sent + 1)
    assert rig.events[-1].text == "2"  # the form the gateway takes as a selection
    assert rig.events[-1].message_id != reply_id
    assert rig.session.reply_id == reply_id and rig.session.reply_open  # the turn that asked goes on
    assert rig.page.of("utterance")[-1] == {"type": "utterance", "text": "Blue", "resumed": True}
    assert rig.page.of("question_end") == [{"type": "question_end", "request_id": "q1"}]


@pytest.mark.parametrize("frame", [
    {"type": "question_answer", "request_id": "q0", "number": 2},   # another question's card
    {"type": "question_answer", "request_id": "q1", "number": 9},   # no such choice
    {"type": "question_answer", "request_id": "q1", "number": "2"},
    {"type": "question_answer", "request_id": "q1"},
])
async def test_a_tap_that_fits_no_choice_of_the_open_question_answers_nothing(rig, clarify, frame):
    await _asked_with_choices(rig, clarify)
    sent = len(rig.events)
    await rig.page.ws.send(json.dumps(frame))
    await asyncio.sleep(0.2)
    assert len(rig.events) == sent
    assert rig.page.of("question_end") == []


async def test_a_tap_on_a_question_that_no_longer_waits_closes_the_card_and_answers_nothing(rig, clarify):
    await _asked_with_choices(rig, clarify)
    key = rig.adapter._source_session_key(rig.adapter._voice_source("voice"))
    clarify_gateway.clear_session(key)  # it timed out, or was answered on another device
    sent = len(rig.events)
    await _tap(rig, 2)
    await wait_for(lambda: rig.page.of("question_end"))
    assert len(rig.events) == sent


async def test_a_spoken_answer_closes_the_card(rig, clarify):
    await _asked_with_choices(rig, clarify)
    await _heard(rig, "Number four.")
    assert rig.events[-1].text == "4"
    assert rig.page.of("question_end") == [{"type": "question_end", "request_id": "q1"}]
    sent = len(rig.events)
    await _tap(rig, 2)  # a late tap on a card that is gone
    await asyncio.sleep(0.2)
    assert len(rig.events) == sent


async def test_a_newer_question_replaces_the_card_of_the_one_before(rig, clarify):
    await _asked_with_choices(rig, clarify)
    key = rig.adapter._source_session_key(rig.adapter._voice_source("voice"))
    clarify_gateway.clear_session(key)
    clarify_gateway.register("q2", key, "And which size?", ["Small", "Large"])
    await rig.adapter.send_clarify("voice", "And which size?", ["Small", "Large"], "q2", key)
    await wait_for(lambda: len(rig.page.of("question")) == 2)
    kinds = [f["type"] for f in rig.page.frames if f["type"].startswith("question")]
    assert kinds == ["question", "question_end", "question"]
    sent = len(rig.events)
    await _tap(rig, 1)  # the old card's id
    await asyncio.sleep(0.2)
    assert len(rig.events) == sent
    await _tap(rig, 2, "q2")
    await wait_for(lambda: len(rig.events) == sent + 1)
    assert rig.events[-1].text == "2"


# --- the user spoke and no words came: turn_empty from VoiceManager ----------------------
from test_approvals import _prompt_open  # noqa: E402

EMPTY = {"type": "turn_empty", "mic_sample": 1000, "by": "silence", "speech": True}
NOT_CAUGHT = "I did not catch that."
# What the notice adds for the four-choice question of these tests: the long forms.
NOT_CAUGHT_FOUR = NOT_CAUGHT + " Say number one, number two, number three or number four, or tap a choice."


async def _prompt_audio_ended(rig):
    """The audio of the spoken question or approval prompt has all come: it no longer
    counts as being spoken."""
    request_id = rig.session._prompt_request.request_id
    await rig.conn.send({"type": "audio_start", "request_id": request_id, "sample_rate": 24000})
    await rig.conn.send({"type": "audio_end", "request_id": request_id, "reason": "complete"})
    await wait_for(lambda: rig.session._prompt_request is None)


@pytest.fixture
def quick_notice(plugin, monkeypatch):
    """The wait before the notice, short for a test."""
    monkeypatch.setattr(plugin.session, "NOT_CAUGHT_WAIT_S", 0.05)


async def test_a_turn_empty_while_a_question_waits_is_answered_with_a_notice(rig, clarify, quick_notice):
    """The recognizer gave no text for the answer. Hermes says so, and the question
    and its card stay open."""
    await _asked_with_choices(rig, clarify)
    await _prompt_audio_ended(rig)
    sent = len(rig.events)
    await rig.conn.send(EMPTY)
    await wait_for(lambda: rig.tts_text().endswith(NOT_CAUGHT_FOUR))
    assert rig.page.of("note")[-1] == {"type": "note", "text": NOT_CAUGHT_FOUR}
    await asyncio.sleep(0.1)
    assert len(rig.events) == sent  # nothing went to Hermes
    assert await rig.session._clarify_pending() is True
    assert rig.page.of("question_end") == []
    await _heard(rig, "Number four.")  # and the answer still works
    assert rig.events[-1].text == "4"


async def test_a_second_turn_empty_soon_after_gets_no_second_notice(rig, clarify, quick_notice):
    await _asked_with_choices(rig, clarify)
    await _prompt_audio_ended(rig)
    await rig.conn.send(EMPTY)
    await wait_for(lambda: rig.tts_text().endswith(NOT_CAUGHT_FOUR))
    await _prompt_audio_ended(rig)  # the notice itself has been spoken
    await rig.conn.send(EMPTY)
    await rig.conn.send(EMPTY)
    await asyncio.sleep(0.4)
    assert rig.tts_text().count(NOT_CAUGHT) == 1


@pytest.mark.parametrize("frame", [
    {**EMPTY, "speech": False},                                        # room noise: the recognizer heard no speech
    {"type": "turn_empty", "mic_sample": 1000, "by": "silence"},      # a server that does not say
])
async def test_a_turn_empty_without_speech_gets_no_notice(rig, clarify, quick_notice, frame):
    await _asked_with_choices(rig, clarify)
    await _prompt_audio_ended(rig)
    await rig.conn.send(frame)
    await asyncio.sleep(0.4)
    assert NOT_CAUGHT not in rig.tts_text() and not rig.page.of("note")


async def test_a_turn_empty_with_nothing_waiting_gets_no_notice(rig, quick_notice):
    await _reply_in_progress(rig)
    rig.wants(False)
    await rig.conn.send(EMPTY)
    await asyncio.sleep(0.4)
    assert NOT_CAUGHT not in rig.tts_text() and not rig.page.of("note")


async def test_a_turn_empty_while_an_approval_waits_asks_for_yes_or_no(rig, quick_notice):
    await _reply_in_progress(rig)
    await _prompt_open(rig)
    await _prompt_audio_ended(rig)
    sent = len(rig.events)
    await rig.conn.send(EMPTY)
    await wait_for(lambda: rig.tts_text().endswith(NOT_CAUGHT + " Say approve or deny, or tap a choice."))
    await asyncio.sleep(0.1)
    assert len(rig.events) == sent and not rig.page.of("approval_end")


async def test_words_that_arrive_after_a_turn_empty_stop_the_notice(rig, clarify, plugin, monkeypatch):
    """An utterance can be cut in two: the first part ends empty and the words follow."""
    monkeypatch.setattr(plugin.session, "NOT_CAUGHT_WAIT_S", 0.4)
    await _asked_with_choices(rig, clarify)
    await _prompt_audio_ended(rig)
    await rig.conn.send(EMPTY)
    await rig.conn.send({"type": "transcription", "segment_index": 7, "text": "number", "is_final": False})
    await asyncio.sleep(0.8)
    assert NOT_CAUGHT not in rig.tts_text()


async def test_a_turn_empty_while_the_question_is_still_spoken_does_not_cut_it(rig, clarify, quick_notice):
    """A noise or a word while the question plays must not replace the choices with
    a notice. Once the question has been spoken, the notice comes as usual."""
    await _asked_with_choices(rig, clarify)
    await rig.conn.send(EMPTY)
    await asyncio.sleep(0.4)
    assert NOT_CAUGHT not in rig.tts_text() and not rig.page.of("note")
    assert rig.conn.json_frames("tts_cancel") == []  # the question's audio goes on
    await _prompt_audio_ended(rig)
    await rig.conn.send(EMPTY)  # not held back by the one before: nothing was said then
    await wait_for(lambda: rig.tts_text().endswith(NOT_CAUGHT_FOUR))


async def test_a_turn_empty_without_the_turn_engine_gets_no_notice(rig, clarify, quick_notice):
    await _asked_with_choices(rig, clarify)
    await _prompt_audio_ended(rig)
    rig.session.turn_engine = None
    await rig.conn.send(EMPTY)
    await asyncio.sleep(0.4)
    assert NOT_CAUGHT not in rig.tts_text()


async def test_the_card_of_a_question_that_waits_no_more_closes_when_its_tool_ends(rig, clarify):
    """The question timed out or was answered on another device: the turn goes on,
    and the page must not go on asking for an answer."""
    reply_id = await _asked_with_choices(rig, clarify)
    await rig.session.on_tool_end(reply_id, "c0", "ok", 5)  # another tool; the question still waits
    await asyncio.sleep(0.05)
    assert rig.page.of("question_end") == []
    key = rig.adapter._source_session_key(rig.adapter._voice_source("voice"))
    clarify_gateway.clear_session(key)
    await rig.session.on_tool_end(reply_id, "c1", "ok", 120000)  # the question's own tool call
    await wait_for(lambda: rig.page.of("question_end"))
    assert rig.page.of("question_end") == [{"type": "question_end", "request_id": "q1"}]


async def test_the_not_caught_notice_for_a_question_without_choices_asks_for_a_few_words(rig, clarify, quick_notice):
    await _reply_in_progress(rig)
    _, key = clarify(QUESTION)
    await rig.adapter.send_clarify("voice", QUESTION, None, "q1", key)
    await wait_for(lambda: rig.tts_text().endswith(QUESTION))
    await _prompt_audio_ended(rig)
    await rig.conn.send(EMPTY)
    await wait_for(lambda: rig.tts_text().endswith(NOT_CAUGHT + " Say it again in a few words."))


def test_the_not_caught_notice_names_one_example_for_a_long_list_of_choices(plugin):
    forms = plugin.turns.number_forms
    assert forms(2) == "number one or number two"
    assert forms(4) == "number one, number two, number three or number four"
    assert forms(5) is None and forms(0) is None
