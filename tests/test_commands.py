"""Spoken commands: "end session" ends the voice session and "new session" starts a
fresh one, instead of going to Hermes."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig
from gateway.session_context import set_session_vars

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"
PLATFORM = "voice_unleashed"


# --- the phrase helpers ------------------------------------------------------------------


@pytest.mark.parametrize("said, phrase", [
    ("End the session!", "end the session"),
    ("  End   SESSION. ", "end session"),
    ("end, this session?", "end this session"),
    ("", ""),
])
def test_an_end_phrase_is_normalized_to_lower_case_words_without_punctuation(plugin, said, phrase):
    assert plugin.turns.normalized_phrase(said) == phrase


@pytest.mark.parametrize("said", ["End the session.", "end session", "End this session!", "End of session.",
                                  "end discussion", "End of discussion!", "In session.", "in discussion"])
def test_the_end_phrases_match_the_end_command(plugin, said):
    assert plugin.turns.spoken_command(said) == plugin.turns.END_SESSION


@pytest.mark.parametrize("said", ["end session now please", "please end the session", "end", "session",
                                  "that is the end of discussion", "end of discussion for now", "discussion",
                                  "we are in session", "in discussion with the team", "in",
                                  "tell me a story"])
def test_other_text_is_no_end_command(plugin, said):
    assert plugin.turns.spoken_command(said) is None


@pytest.mark.parametrize("said", ["New session!", "start a new session", "Start new session."])
def test_the_new_phrases_match_the_new_command(plugin, said):
    assert plugin.turns.spoken_command(said) == plugin.turns.NEW_SESSION


@pytest.mark.parametrize("said", ["new session please", "start a new session now", "new", "a new session"])
def test_other_text_is_no_new_command(plugin, said):
    assert plugin.turns.spoken_command(said) is None


# --- through the adapter and a fake VoiceManager ------------------------------------------


def fire(hook, message_id, **payload):
    """Call a hook off the loop, inside the session context of the turn answering ``message_id``."""
    def run():
        set_session_vars(platform=PLATFORM, chat_id="voice", message_id=message_id)
        hook(surface=PLATFORM, **payload)

    thread = threading.Thread(target=contextvars.Context().run, args=(run,))
    thread.start()
    thread.join()


class Page:
    """Records what the page receives, in order; ``order`` gets the events the test
    needs to see in sequence: each TTS chunk and page frame the session sends, and
    the moment the page's socket closed."""

    def __init__(self, ws, order):
        self.ws, self.log, self.order = ws, [], order
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            self.log.append(("pcm", message) if isinstance(message, bytes) else ("json", json.loads(message)))
        self.order.append(("closed", self.ws.close_code))

    def of(self, kind):
        return [f for k, f in self.log if k == "json" and f.get("type") == kind]

    @property
    def closed(self):
        return self.task.done()


class Rig:
    def __init__(self, hooks, session, conn, page, messages, order):
        self.hooks, self.session, self.conn, self.page = hooks, session, conn, page
        self.messages, self.order = messages, order
        self.segments = 0  # classic mode: segment indexes handed out so far

    def next_segment(self):
        """A segment index no utterance of this connection used before."""
        self.segments += 1
        return self.segments

    def tts(self):
        return self.conn.json_frames("tts_stream")

    def tts_text(self):
        return "".join(f["text"] for f in self.tts())


@contextlib.asynccontextmanager
async def rig(plugin, refusal=None):
    async with FakeVoiceManager(turn_engine_refusal=refusal) as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        messages, order = [], []

        async def handle_message(event):
            messages.append(event)

        adapter.handle_message = handle_message
        assert await adapter.connect()
        try:
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=voice") as ws:
                page = Page(ws, order)
                await wait_for(lambda: page.of("ready"))
                await ws.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                session = adapter.session_for("voice")
                _record_order(session, order)
                yield Rig(plugin.stream_hooks, session, conn, page, messages, order)
                page.task.cancel()
        finally:
            await adapter.disconnect()


def _record_order(session, order):
    """Each TTS chunk is recorded as the text its request has so far, so the notice
    shows up as one event once its last word went out. The new-session callback the
    adapter gave the session is recorded as it is called."""
    send_tts, send_page = session._send_tts, session.send_page
    on_new_session = session._on_new_session
    texts = {}

    async def recording_tts(chunk, request=None):
        await send_tts(chunk, request)
        if chunk.strip():
            rid = (request or session.reply_request).request_id
            texts[rid] = texts.get(rid, "") + chunk
            order.append(("tts", texts[rid].strip()))

    async def recording_page(payload):
        order.append(("page", payload["type"]))
        await send_page(payload)

    async def recording_new_session():
        order.append(("callback", "new_session"))
        await on_new_session()

    session._send_tts, session.send_page = recording_tts, recording_page
    if on_new_session is not None:
        session._on_new_session = recording_new_session


async def _listening(rig, turn):
    """The session listens in classic turn ``turn``: VoiceManager got its turn_start
    and the session got the answer."""
    await wait_for(lambda: len(rig.conn.json_frames("turn_start")) == turn
                   and rig.session.tracker.turn_id == turn)


async def _heard(rig, text, refusal=None):
    """VoiceManager hears ``text`` as a finished turn: a turn_end frame with the turn
    engine, a final transcription frame in classic mode. Each utterance is a segment
    of its own, as VoiceManager numbers them, in the turn the session listens to."""
    if refusal:
        await rig.conn.send({"type": "transcription", "segment_index": rig.next_segment(), "text": text,
                             "endpoint": True, "is_final": True, "turn_id": rig.session.tracker.turn_id})
    else:
        await rig.conn.send({"type": "turn_end", "text": text, "turn_id": 0})


async def _playing_reply(rig, refusal=None):
    """An utterance was answered and its reply is playing on the page."""
    if refusal:
        await _listening(rig, 1)
        await rig.conn.send({"type": "transcription", "segment_index": rig.next_segment(),
                             "text": "tell me a story", "endpoint": True, "is_final": False,
                             "turn_id": rig.session.tracker.turn_id})
    else:
        await rig.conn.send({"type": "turn_end", "text": "tell me a story", "turn_id": 0})
    await wait_for(lambda: rig.messages)
    message_id = rig.messages[-1].message_id
    fire(rig.hooks.on_stream_delta, message_id, delta="Once upon a time ", kind="text", turn_id="t1",
         iteration=1)
    frames = await wait_for(lambda: rig.tts())
    rid = frames[-1]["request_id"]
    await rig.conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await rig.conn.send(b"\x11\x00" * 240)
    await wait_for(lambda: any(k == "pcm" for k, _ in rig.page.log))
    return message_id, rid


async def _notice_played(rig, text):
    """The notice reached VoiceManager as a request of its own that was ended; the fake
    plays it and reports its end. Returns the notice's request id."""
    rid = await wait_for(lambda: _request_saying(rig, text))
    await wait_for(lambda: any(f["request_id"] == rid and f["done"] for f in rig.tts()))
    await rig.conn.send({"type": "audio_start", "request_id": rid, "sample_rate": 24000})
    await rig.conn.send(b"\x22\x00" * 240)
    await rig.conn.send({"type": "audio_end", "request_id": rid, "reason": "completed"})
    return rid


def _request_saying(rig, text):
    """The id of the TTS request whose chunks so far make up ``text``, if any."""
    texts = {}
    for f in rig.tts():
        texts[f["request_id"]] = texts.get(f["request_id"], "") + f["text"]
    return next((rid for rid, t in texts.items() if t.strip() == text), None)


def _sequence(order, *wanted):
    """The events of ``order`` that are among ``wanted``, in order."""
    return [event for event in order if event in wanted]


@pytest.mark.parametrize("refusal", [None, "turn_engine_unsupported"])
async def test_end_the_session_cancels_the_reply_speaks_the_notice_and_closes_the_page(plugin, refusal):
    async with rig(plugin, refusal) as r:
        message_id, rid = await _playing_reply(r, refusal)
        await _heard(r, "End the session.", refusal)
        notice_rid = await _notice_played(r, "Session ended.")
        await wait_for(lambda: r.page.closed)
        assert notice_rid != rid
        assert r.conn.json_frames("tts_cancel")[0]["request_id"] == rid  # the reply was cancelled
        assert r.page.of("clear")
        assert len(r.messages) == 1  # the command never reached Hermes
        assert r.messages[0].text == "tell me a story"
        assert [f["text"] for f in r.page.of("utterance")] == ["tell me a story"]
        assert r.page.of("session_end") == [{"type": "session_end"}]
        close = r.order[-1]
        assert close[0] == "closed"
        # The notice plays out before the page is told the session ended and is closed.
        assert _sequence(r.order, ("tts", "Session ended."), ("page", "audio_end"), ("page", "session_end"),
                         close) == [("tts", "Session ended."), ("page", "audio_end"), ("page", "session_end"), close]
        fire(r.hooks.on_stream_delta, message_id, delta="there was a dragon", kind="text", turn_id="t1",
             iteration=1)
        await asyncio.sleep(0.1)
        assert "dragon" not in r.tts_text()
        await asyncio.wait_for(r.conn.closed.wait(), 2)  # the VoiceManager connection went with it


async def test_the_page_is_closed_when_the_notice_does_not_end_in_time(plugin, monkeypatch):
    monkeypatch.setattr(plugin.session, "SESSION_END_TIMEOUT_S", 0.2)
    async with rig(plugin) as r:
        await _heard(r, "end session")
        rid = await wait_for(lambda: _request_saying(r, "Session ended."))
        await wait_for(lambda: any(f["request_id"] == rid and f["done"] for f in r.tts()))
        # No audio_end came: the page is told nothing until the wait ran out.
        assert r.page.of("session_end") == []
        assert not r.page.closed
        await wait_for(lambda: r.page.closed)
        assert r.messages == []
        assert ("page", "audio_end") not in r.order
        assert _sequence(r.order, ("tts", "Session ended."), ("page", "session_end")) == [
            ("tts", "Session ended."), ("page", "session_end")]
        assert r.order[-2] == ("page", "session_end")
        assert r.order[-1][0] == "closed"


async def test_the_end_of_another_notice_does_not_end_the_wait_for_the_session_ended_notice(plugin):
    async with rig(plugin) as r:
        await r.session.notice("An earlier notice")
        earlier = await wait_for(lambda: _request_saying(r, "An earlier notice"))
        await _heard(r, "end session")
        ended = await wait_for(lambda: _request_saying(r, "Session ended."))
        await wait_for(lambda: any(f["request_id"] == ended and f["done"] for f in r.tts()))
        await r.conn.send({"type": "audio_end", "request_id": earlier, "reason": "completed"})
        await wait_for(lambda: r.page.of("audio_end"))
        await asyncio.sleep(0.2)
        assert r.page.of("session_end") == []  # still waiting for its own notice
        await r.conn.send({"type": "audio_end", "request_id": ended, "reason": "completed"})
        await wait_for(lambda: r.page.of("session_end"))


async def test_end_session_now_please_is_an_ordinary_utterance(plugin):
    async with rig(plugin) as r:
        await _heard(r, "end session now please")
        await wait_for(lambda: r.messages)
        assert r.messages[0].text == "end session now please"
        await asyncio.sleep(0.1)
        assert r.page.of("session_end") == []
        assert "Session ended." not in r.tts_text()
        assert not r.page.closed


def _page_kinds(page):
    return [f["type"] for k, f in page.log if k == "json"]


async def test_new_session_speaks_the_notice_then_asks_the_adapter_and_keeps_the_page_open(plugin):
    async with rig(plugin) as r:
        await _heard(r, "New session!")
        await _notice_played(r, "New session.")
        await wait_for(lambda: r.page.of("session_new"))
        await wait_for(lambda: len(r.page.of("history")) == 2)
        await asyncio.sleep(0.1)
        assert r.messages == []  # the command never reached Hermes
        assert r.page.of("utterance") == []
        assert r.page.of("session_end") == []
        # The notice first, then the adapter is asked for the new session: it answers
        # with the session_new frame and the history of the new (here unknown) session.
        assert _sequence(r.order, ("tts", "New session."), ("callback", "new_session"),
                         ("page", "session_new")) == [("tts", "New session."), ("callback", "new_session")]
        assert r.page.of("session_new") == [{"type": "session_new", "session_id": None}]
        assert r.page.of("history")[-1] == {"type": "history", "session_id": None, "lines": []}
        kinds = _page_kinds(r.page)
        assert kinds[kinds.index("session_new") + 1] == "history"
        assert kinds.count("ready") == 1  # the page did not reconnect
        assert not r.page.closed
        assert not r.conn.closed.is_set()
        # The same session takes the next utterance.
        await _heard(r, "tell me a joke")
        await wait_for(lambda: r.messages)
        assert r.messages[0].text == "tell me a joke"
        assert [f["text"] for f in r.page.of("utterance")] == ["tell me a joke"]
        assert not r.page.closed


async def test_new_session_without_a_callback_sends_the_session_new_frame_itself(plugin):
    async with rig(plugin) as r:
        r.session._on_new_session = None
        await _heard(r, "New session!")
        await _notice_played(r, "New session.")
        await wait_for(lambda: r.page.of("session_new"))
        await asyncio.sleep(0.1)
        assert r.messages == []
        assert r.page.of("session_new") == [{"type": "session_new"}]
        assert len(r.page.of("history")) == 1  # only the one from the connect
        assert _sequence(r.order, ("tts", "New session."), ("callback", "new_session"),
                         ("page", "session_new")) == [("tts", "New session."), ("page", "session_new")]
        assert not r.page.closed
        await _heard(r, "tell me a joke")
        await wait_for(lambda: r.messages)
        assert r.messages[0].text == "tell me a joke"


@pytest.mark.parametrize("refusal", [None, "turn_engine_unsupported"])
async def test_new_session_cancels_the_playing_reply_before_the_notice(plugin, refusal):
    async with rig(plugin, refusal) as r:
        message_id, rid = await _playing_reply(r, refusal)
        await _heard(r, "start a new session", refusal)
        notice_rid = await _notice_played(r, "New session.")
        await wait_for(lambda: r.page.of("session_new"))
        assert notice_rid != rid
        assert r.conn.json_frames("tts_cancel")[0]["request_id"] == rid  # the reply was cancelled
        assert r.page.of("clear")
        assert len(r.messages) == 1
        assert r.messages[0].text == "tell me a story"
        assert _sequence(r.order, ("tts", "New session."), ("callback", "new_session")) == [
            ("tts", "New session."), ("callback", "new_session")]
        fire(r.hooks.on_stream_delta, message_id, delta="there was a dragon", kind="text", turn_id="t1",
             iteration=1)
        await asyncio.sleep(0.1)
        assert "dragon" not in r.tts_text()
        assert not r.page.closed
        assert not r.conn.closed.is_set()
        if refusal:
            await _listening(r, 2)  # the command ended its turn; the next one is open
        await _heard(r, "tell me a joke", refusal)
        await wait_for(lambda: len(r.messages) == 2)
        assert r.messages[1].text == "tell me a joke"


async def test_the_notice_does_not_speak_the_word_the_cancelled_reply_held(plugin):
    async with rig(plugin) as r:
        message_id, rid = await _playing_reply(r)
        fire(r.hooks.on_stream_delta, message_id, delta="Standing by", kind="text", turn_id="t1",
             iteration=1)  # no space after it: "by" waits for more text
        await wait_for(lambda: "Standing " in r.tts_text())
        assert r.session._unsent == "by"
        await _heard(r, "new session")
        notice_rid = await _notice_played(r, "New session.")
        await wait_for(lambda: r.page.of("session_new"))
        assert notice_rid != rid
        assert "".join(f["text"] for f in r.tts() if f["request_id"] == notice_rid) == "New session."
        assert all("by" not in f["text"] for f in r.tts())


async def test_new_session_in_classic_mode_ends_its_turn_so_the_next_segment_starts_clean(plugin):
    refusal = "turn_engine_unsupported"
    async with rig(plugin, refusal) as r:
        await _listening(r, 1)
        await _heard(r, "start a new session", refusal)
        await _notice_played(r, "New session.")
        await wait_for(lambda: r.page.of("session_new"))
        # The command's turn is over: the session asks for the next listening turn and
        # keeps nothing of the command's words.
        await _listening(r, 2)
        assert r.session.tracker.text == ""
        assert r.session.tracker.dispatched_words is None
        # The next words come in a later segment, as VoiceManager numbers them.
        await _heard(r, "tell me a joke", refusal)
        await wait_for(lambda: r.messages)
        await asyncio.sleep(0.1)
        assert [m.text for m in r.messages] == ["tell me a joke"]
        assert [f["text"] for f in r.page.of("utterance")] == ["tell me a joke"]
        assert not r.page.closed


async def test_new_session_please_is_an_ordinary_utterance(plugin):
    async with rig(plugin) as r:
        await _heard(r, "new session please")
        await wait_for(lambda: r.messages)
        assert r.messages[0].text == "new session please"
        await asyncio.sleep(0.1)
        assert r.page.of("session_new") == []
        assert "New session." not in r.tts_text()
        assert not r.page.closed


# --- the end command as the last words of a turn ------------------------------------------
async def _finals_then_turn_end(r, *finals):
    """The recognizer gives each of ``finals`` as a segment of its own, and the turn
    engine then ends one turn that holds them all."""
    for text in finals:
        await r.conn.send({"type": "transcription", "segment_index": r.next_segment(), "text": text,
                           "is_final": True})
    await r.conn.send({"type": "turn_end", "text": " ".join(finals), "turn_id": 0})


@pytest.mark.parametrize("finals", [
    ("End session", "End session"),            # said again before the turn ended
    ("I didn't say", "End session"),           # other words first, then the command
    ("Oh come on", "end the session."),
])
async def test_a_turn_whose_last_words_are_the_end_command_ends_the_session(plugin, finals):
    """A command that did not act at once is said again, or after a remark, and the
    turn engine joins it all into one turn. The last thing said decides."""
    async with rig(plugin) as r:
        await _playing_reply(r)
        sent = len(r.messages)
        await _finals_then_turn_end(r, *finals)
        await _notice_played(r, "Session ended.")
        await wait_for(lambda: r.page.closed)
        assert len(r.messages) == sent  # nothing of it went to Hermes


@pytest.mark.parametrize("finals", [
    ("End session", "no wait"),                # the last words are no command
    ("please do not end session",),            # one sentence that only holds the phrase
    ("I was in session", "all day"),
])
async def test_a_turn_whose_last_words_are_no_command_goes_to_hermes(plugin, finals):
    async with rig(plugin) as r:
        await _playing_reply(r)
        sent = len(r.messages)
        await _finals_then_turn_end(r, *finals)
        await wait_for(lambda: len(r.messages) == sent + 1)
        assert r.messages[-1].text == " ".join(finals)
        assert not r.page.closed


@pytest.mark.parametrize("finals", [
    ("What did we do", "this session"),
    ("How long have we been", "in session"),
    ("That was the", "end of discussion"),
    ("At the", "end of session"),
    ("Remind me of it in the", "new session"),
])
async def test_an_ordinary_end_of_a_sentence_that_reads_as_a_command_goes_to_hermes(plugin, finals):
    """Only the forms that are said as an order ("end session", "start a new session")
    act as the last words of a turn. The other phrases are also how a sentence ends,
    when a pause splits it: they act only as the whole turn."""
    async with rig(plugin) as r:
        await _playing_reply(r)
        sent = len(r.messages)
        await _finals_then_turn_end(r, *finals)
        await wait_for(lambda: len(r.messages) == sent + 1)
        assert r.messages[-1].text == " ".join(finals)
        await asyncio.sleep(0.2)
        assert not r.page.closed and r.page.of("session_new") == []
        assert "Session ended." not in r.tts_text() and "New session." not in r.tts_text()


@pytest.mark.parametrize("finals", [
    ("This session", "this session"),          # the recognizer's form, said again
    ("In session", "End discussion"),
])
async def test_a_command_said_again_in_any_of_its_forms_ends_the_session(plugin, finals):
    """Every final of the turn is a command phrase: the user said nothing else."""
    async with rig(plugin) as r:
        await _playing_reply(r)
        sent = len(r.messages)
        await _finals_then_turn_end(r, *finals)
        await _notice_played(r, "Session ended.")
        await wait_for(lambda: r.page.closed)
        assert len(r.messages) == sent


async def test_new_session_said_again_starts_a_new_session(plugin):
    async with rig(plugin) as r:
        await _playing_reply(r)
        sent = len(r.messages)
        await _finals_then_turn_end(r, "New session", "new session")
        await _notice_played(r, "New session.")
        await wait_for(lambda: r.page.of("session_new"))
        assert len(r.messages) == sent and not r.page.closed


def test_the_last_words_rule_takes_the_order_forms_and_a_turn_of_commands_only(plugin):
    last = plugin.turns.last_words_command
    end, new = plugin.turns.END_SESSION, plugin.turns.NEW_SESSION
    assert last(["I didn't say", "End session."]) == end
    assert last(["Oh come on", "end the session"]) == end
    assert last(["no", "Start a new session."]) == new
    assert last(["This session", "this session"]) == end
    assert last(["End session", "new session"]) == new
    assert last(["What did we do", "this session"]) is None
    assert last(["End session", "no wait"]) is None
    assert last([]) is None


@pytest.mark.parametrize("arrived, final, turn_text", [
    ("End session", True, "End session no wait"),
    ("New session", True, "New session is what I said last time"),
    ("End session", False, "End session now please"),   # a partial of one longer utterance
])
async def test_finals_that_are_not_all_of_the_turn_decide_no_command(plugin, arrived, final, turn_text):
    """The transcription frames and the turn end come through two readers. When the
    page takes its frames slowly, the turn end can be handled before the turn's last
    finals: what has arrived is then only the start of what the user said."""
    async with rig(plugin) as r:
        await _playing_reply(r)
        sent = len(r.messages)
        await r.conn.send({"type": "transcription", "segment_index": r.next_segment(), "text": arrived,
                           "is_final": final})
        await r.conn.send({"type": "turn_end", "text": turn_text, "turn_id": 0})
        await wait_for(lambda: len(r.messages) == sent + 1)
        assert r.messages[-1].text == turn_text
        await asyncio.sleep(0.2)
        assert not r.page.closed and r.page.of("session_new") == []
        assert "Session ended." not in r.tts_text() and "New session." not in r.tts_text()


def test_this_session_is_the_end_command_too(plugin):
    """What the recognizer gave for "end session" on a phone."""
    assert plugin.turns.spoken_command("This session.") == plugin.turns.END_SESSION
    assert plugin.turns.spoken_command("this session is over") is None


# --- the frames of a turn reach the turn tracker in the server's order ---------------------
def _record_dispatch(session):
    """The texts that the turn tracker handed to the session's dispatch, in order."""
    texts, inner = [], session.tracker._dispatch

    async def dispatch(text):
        texts.append(text)
        await inner(text)

    session.tracker._dispatch = dispatch
    return texts


async def _stall_the_page(r):
    """The page stops reading and its socket fills until a send to it waits, as a page
    on a slow link takes its frames. Returns the send that waits."""
    r.page.task.cancel()
    filler = json.dumps({"type": "note", "text": "x" * 65536})
    async with asyncio.timeout(20):
        while True:
            send = asyncio.ensure_future(r.session.page.send(filler))
            done, _ = await asyncio.wait([send], timeout=0.3)
            if not done:
                return send


async def test_a_turn_end_is_handled_with_all_its_finals_while_the_page_reads_nothing(plugin):
    """The page takes no frames. The turn end must still come after the turn's finals,
    and it must not wait for the page. The words of that turn must not be left over for
    the next one, where they would keep its end command from acting."""
    async with rig(plugin) as r:
        tracker, dispatched = r.session.tracker, _record_dispatch(r.session)
        waiting = await _stall_the_page(r)
        await _finals_then_turn_end(r, "End session", "no wait")
        await wait_for(lambda: dispatched == ["End session no wait"], timeout=1)
        assert tracker.turn_finals == ["End session", "no wait"] and tracker.segments == {}
        r.page = Page(r.page.ws, r.order)  # the page reads again
        await asyncio.wait_for(waiting, 5)
        await wait_for(lambda: len(r.messages) == 1)
        assert r.messages[0].text == "End session no wait" and tracker.segments == {}
        await _finals_then_turn_end(r, "I didn't say", "End session")
        await _notice_played(r, "Session ended.")
        await wait_for(lambda: r.page.closed)
        assert len(r.messages) == 1  # the second turn went to no one: it ended the session


async def test_a_partial_that_comes_with_a_turn_end_belongs_to_the_next_turn(plugin):
    """Two finals, their turn end and the first partial of the next utterance arrive
    together. The turn that ended is made of its two finals, and the partial is the
    text that the next turn starts with."""
    async with rig(plugin) as r:
        tracker = r.session.tracker
        partial = r.next_segment()
        for run in range(20):
            first, second, partial = partial, r.next_segment(), r.next_segment()
            finals = [f"and then {run}", f"what happened {run}"]
            frames = [
                {"type": "transcription", "segment_index": first, "text": finals[0], "is_final": True},
                {"type": "transcription", "segment_index": second, "text": finals[1], "is_final": True},
                {"type": "turn_end", "text": " ".join(finals), "turn_id": 0},
                {"type": "transcription", "segment_index": partial, "text": f"next {run}", "is_final": False},
            ]
            for frame in frames:  # one after the other, nothing awaited from the session between them
                await r.conn.send(frame)
            await wait_for(lambda: len(r.messages) == run + 1)
            assert r.messages[-1].text == " ".join(finals)
            await wait_for(lambda: tracker.text == f"next {run}")
            assert tracker.turn_finals == finals, run
            assert tracker.segments == {partial: f"next {run}"}, run


async def test_the_page_gets_the_newest_heard_line_and_the_empty_one_after_a_turn_end(plugin):
    async with rig(plugin) as r:
        await r.conn.send({"type": "transcription", "segment_index": r.next_segment(), "text": "tell me",
                           "is_final": False})
        await wait_for(lambda: r.page.of("transcript") and r.page.of("transcript")[-1]["text"] == "tell me")
        await _finals_then_turn_end(r, "tell me a story")
        await wait_for(lambda: r.messages)
        await wait_for(lambda: r.page.of("transcript")[-1] == {"type": "transcript", "text": "", "final": True})
        kinds = _page_kinds(r.page)
        assert kinds.index("utterance") < len(kinds) - 1 - kinds[::-1].index("transcript")
