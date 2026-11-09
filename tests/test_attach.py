"""Attach mode: a UI client binds a voice session to a Hermes session it shows itself.

The client gets each finished utterance as a frame and submits it to the chat
on its own, then streams the reply's text back with ``say`` and ``say_end``.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from gateway.config import PlatformConfig
from gateway.session_context import set_session_vars

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"
PLATFORM = "voicemanager_live"
SESSION_ID = "20261107_101500_ab12cd"


class Client:
    """Reads every frame a WebSocket client gets."""

    def __init__(self, ws):
        self.ws, self.frames = ws, []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            if not isinstance(message, bytes):
                self.frames.append(json.loads(message))

    def of(self, kind):
        return [f for f in self.frames if f.get("type") == kind]

    async def say(self, *pieces, reply=None, epoch=None):
        for piece in pieces:
            await self.ws.send(json.dumps({"type": "say", "text": piece, **_named(reply, epoch)}))

    async def say_end(self, reply=None, epoch=None):
        await self.ws.send(json.dumps({"type": "say_end", **_named(reply, epoch)}))


def _named(reply, epoch):
    """The optional fields of a say or say_end frame."""
    return {**({"reply": reply} if reply else {}), **({"epoch": epoch} if epoch is not None else {})}


@pytest.fixture
async def fake_vm(request):
    """A fake VoiceManager; parametrize it indirectly with an error type to refuse the turn engine."""
    async with FakeVoiceManager(turn_engine_refusal=getattr(request, "param", None)) as fake:
        yield fake


@pytest.fixture
async def adapter(plugin, fake_vm):
    adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
        "token": TOKEN, "voicemanager_url": fake_vm.url, "host": "127.0.0.1", "port": 0}))
    messages = []

    async def handle_message(event):
        messages.append(event)

    adapter.handle_message = handle_message
    adapter.messages = messages
    assert await adapter.connect()
    yield adapter
    await adapter.disconnect()


def _url(adapter, query):
    return f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&{query}"


@pytest.fixture
async def ready(adapter, fake_vm):
    """An attached client and its VoiceManager connection, nothing said yet."""
    async with connect(_url(adapter, f"attach={SESSION_ID}")) as ws:
        client = Client(ws)
        await wait_for(lambda: client.of("ready"))
        (conn,) = await fake_vm.wait_connections(1)
        yield client, conn
        client.task.cancel()


@pytest.fixture
async def attached(ready):
    """An attached client, its VoiceManager connection, and one utterance already dispatched."""
    client, conn = ready
    await conn.send({"type": "turn_end", "text": "tell me something", "turn_id": 0})
    await wait_for(lambda: client.of("utterance"))
    return client, conn


def tts_frames(conn):
    return conn.json_frames("tts_stream")


def ended(conn):
    frames = tts_frames(conn)
    return frames if frames and frames[-1]["done"] else None


def voicemanager_join(frames):
    """How VoiceManager's clause mode rebuilds a request's text from its chunks."""
    return " ".join(f["text"].strip() for f in frames if f["text"].strip())


def fire(hook, chat_id, **payload):
    """Call a hook the way Hermes does: off the loop, inside the turn's session context."""
    def run():
        set_session_vars(platform=PLATFORM, chat_id=chat_id)
        hook(**payload)

    thread = threading.Thread(target=contextvars.Context().run, args=(run,))
    thread.start()
    thread.join()


async def test_attach_starts_a_voicemanager_session_bound_to_the_session_id(adapter, fake_vm):
    assert not adapter.is_attached(SESSION_ID)
    async with connect(_url(adapter, f"attach={SESSION_ID}")) as ws:
        client = Client(ws)
        await wait_for(lambda: client.of("ready"))
        (conn,) = await fake_vm.wait_connections(1)
        assert conn.frames[0] == {"type": "turn_engine", "engine": "tyger"}
        assert adapter.is_attached(SESSION_ID)
        assert not adapter.is_attached("voice")
        assert adapter.session_for(SESSION_ID) is None  # not a page: no hook text routes to it
        client.task.cancel()


async def test_an_utterance_goes_to_the_client_only(attached, adapter):
    client, conn = attached
    assert client.of("utterance") == [{"type": "utterance", "text": "tell me something", "epoch": 1}]
    await asyncio.sleep(0.1)
    assert adapter.messages == []


async def test_end_session_on_an_attached_session_is_an_utterance_for_the_client(attached, adapter):
    """The spoken commands are the page's: an attached client gets every transcript."""
    client, conn = attached
    await conn.send({"type": "turn_end", "text": "end session", "turn_id": 0})
    await wait_for(lambda: len(client.of("utterance")) == 2)
    assert client.of("utterance")[1] == {"type": "utterance", "text": "end session", "epoch": 3}
    await asyncio.sleep(0.2)
    assert client.of("session_end") == []
    assert tts_frames(conn) == []
    assert not client.task.done()  # the connection is still open
    assert adapter.is_attached(SESSION_ID)


async def test_say_frames_reach_tts_in_whole_words_and_say_end_closes_the_reply(attached):
    client, conn = attached
    await client.say("Hel", "lo th", "ere. How a")
    first = await wait_for(lambda: tts_frames(conn))
    assert first[0]["text"] == "Hello "  # sent before the reply ends, the split word held back
    assert first[0]["done"] is False
    await client.say("re y", "ou", "?")
    await client.ws.send(json.dumps({"type": "say_end"}))
    frames = await wait_for(lambda: ended(conn))
    texts = [f["text"] for f in frames]
    assert "".join(texts) == "Hello there. How are you?"
    assert voicemanager_join(frames) == "Hello there. How are you?"
    assert all(t[-1].isspace() for t in texts[:-2])
    assert {f["request_id"] for f in frames} == {frames[0]["request_id"]}
    count = len(tts_frames(conn))
    await client.say("late words ")
    await asyncio.sleep(0.2)
    assert len(tts_frames(conn)) == count  # the reply is closed: no more text taken
    assert "".join(f["text"] for f in client.of("reply_delta")) == "Hello there. How are you?"


async def test_say_end_with_no_text_ends_the_reply(attached, fake_vm):
    client, conn = attached
    await client.ws.send(json.dumps({"type": "say_end"}))
    await asyncio.sleep(0.1)
    await conn.send({"type": "turn_end", "text": "and now", "turn_id": 0})
    await wait_for(lambda: len(client.of("utterance")) == 2)
    await client.say("Second.")
    await client.ws.send(json.dumps({"type": "say_end"}))
    frames = await wait_for(lambda: ended(conn))
    assert frames[0]["text"] == "Second."
    assert conn.json_frames("tts_cancel") == []


async def test_a_keyed_say_end_with_no_text_ends_the_utterance_reply(attached, adapter):
    client, conn = attached
    session = adapter._attached[SESSION_ID]
    assert session.reply_open
    await client.ws.send(json.dumps({"type": "say_end", "reply": "screen-only"}))
    await wait_for(lambda: not session.reply_open)
    await client.ws.send(json.dumps({"type": "say", "text": "Late text. ", "reply": "screen-only"}))
    await asyncio.sleep(0.1)
    assert not session.reply_open
    assert tts_frames(conn) == []


async def test_closing_the_socket_detaches_and_ends_the_voicemanager_session(adapter, fake_vm):
    async with connect(_url(adapter, f"attach={SESSION_ID}")) as ws:
        client = Client(ws)
        await wait_for(lambda: client.of("ready"))
        (conn,) = await fake_vm.wait_connections(1)
        assert adapter.is_attached(SESSION_ID)
    await asyncio.wait_for(conn.closed.wait(), 2.0)
    await wait_for(lambda: not adapter.is_attached(SESSION_ID))


async def test_stream_hooks_never_reach_an_attached_session(attached, plugin):
    client, conn = attached
    fire(plugin.stream_hooks.on_stream_delta, SESSION_ID, delta="hook text. ", kind="text",
         turn_id="t1", iteration=1, surface=PLATFORM)
    fire(plugin.stream_hooks.on_session_end, SESSION_ID, turn_id="t1", interrupted=False,
         platform=PLATFORM)
    await asyncio.sleep(0.5)
    assert tts_frames(conn) == []
    assert client.of("reply_delta") == []


async def test_a_page_and_an_attached_client_with_the_same_id_both_stay(adapter, fake_vm):
    async with connect(_url(adapter, f"client={SESSION_ID}")) as page_ws, \
            connect(_url(adapter, f"attach={SESSION_ID}")) as attach_ws:
        page, client = Client(page_ws), Client(attach_ws)
        await wait_for(lambda: page.of("ready") and client.of("ready"))
        await fake_vm.wait_connections(2)
        await asyncio.sleep(0.1)
        assert page.of("replaced") == [] and client.of("replaced") == []
        assert adapter.is_attached(SESSION_ID)
        assert adapter.session_for(SESSION_ID) is not None  # the page's own session
        page.task.cancel()
        client.task.cancel()


async def test_a_page_ignores_say_frames(adapter, fake_vm):
    async with connect(_url(adapter, "client=voice")) as ws:
        page = Client(ws)
        await wait_for(lambda: page.of("ready"))
        (conn,) = await fake_vm.wait_connections(1)
        await conn.send({"type": "turn_end", "text": "hello", "turn_id": 0})
        await wait_for(lambda: adapter.messages)
        await page.say("not from the page. ")
        await ws.send(json.dumps({"type": "say_end"}))
        await asyncio.sleep(0.5)
        assert tts_frames(conn) == []
        page.task.cancel()


def by_request(conn):
    """The tts_stream frames grouped by request id, in the order the requests started."""
    groups = {}
    for f in tts_frames(conn):
        groups.setdefault(f["request_id"], []).append(f)
    return list(groups.values())


def requests_ended(conn, count):
    groups = by_request(conn)
    return groups if len(groups) == count and all(g[-1]["done"] for g in groups) else None


async def test_a_typed_message_reply_is_spoken_in_whole_words(ready):
    client, conn = ready
    await client.say("Sure", ", he", reply="m1")
    first = await wait_for(lambda: tts_frames(conn))
    assert first[0]["text"] == "Sure, "
    assert first[0]["done"] is False
    await client.say("re it", " is.", reply="m1")
    await client.say_end("m1")
    frames = await wait_for(lambda: ended(conn))
    assert "".join(f["text"] for f in frames) == "Sure, here it is."
    assert voicemanager_join(frames) == "Sure, here it is."
    assert client.of("utterance") == []


async def test_after_a_barge_in_the_cut_off_reply_is_dropped_and_a_new_one_spoken(attached):
    client, conn = attached
    await client.say("First part of ", reply="m1")
    (first,) = await wait_for(lambda: tts_frames(conn))
    await conn.send({"type": "barge", "request_id": first["request_id"], "onset_mic_sample": 10,
                     "mic_sample": 20})
    await wait_for(lambda: client.of("clear"))
    await client.say("the old answer. ", reply="m1")
    await client.say_end("m1")
    await client.say("New reply.", reply="m2")
    await client.say_end("m2")
    await wait_for(lambda: len(by_request(conn)) == 2 and by_request(conn)[1][-1]["done"])
    groups = by_request(conn)
    assert "".join(f["text"] for f in groups[0]) == "First part of "
    assert not any(f["done"] for f in groups[0])  # the cut-off request is never ended
    assert "".join(f["text"] for f in groups[1]) == "New reply."
    assert "old answer" not in "".join(f["text"] for f in client.of("reply_delta"))


async def test_a_second_key_ends_the_open_reply_and_starts_its_own(ready):
    client, conn = ready
    await client.say("One more th", reply="m1")
    await client.say("Two.", reply="m2")
    await client.say_end("m2")
    first, second = await wait_for(lambda: requests_ended(conn, 2))
    assert "".join(f["text"] for f in first) == "One more th"
    assert "".join(f["text"] for f in second) == "Two."
    await client.say("late. ", reply="m1")
    await asyncio.sleep(0.2)
    assert len(by_request(conn)) == 2
    assert conn.json_frames("tts_cancel") == []


async def test_say_end_for_a_finished_key_does_nothing(ready):
    client, conn = ready
    await client.say("Done.", reply="m1")
    await client.say_end("m1")
    await wait_for(lambda: requests_ended(conn, 1))
    await client.say("Two ", reply="m2")
    await wait_for(lambda: len(by_request(conn)) == 2)
    await client.say_end("m1")  # the old key again: the open reply goes on
    await client.say("more.", reply="m2")
    await client.say_end("m2")
    first, second = await wait_for(lambda: requests_ended(conn, 2))
    assert "".join(f["text"] for f in second) == "Two more."
    assert [f["done"] for f in second].count(True) == 1
    assert conn.json_frames("tts_cancel") == []


async def test_utterance_and_clear_frames_carry_a_rising_epoch(attached, adapter):
    client, conn = attached
    (first,) = client.of("utterance")
    await client.say("First part of ", reply="m1", epoch=first["epoch"])
    (tts,) = await wait_for(lambda: tts_frames(conn))
    await conn.send({"type": "barge", "request_id": tts["request_id"], "onset_mic_sample": 10,
                     "mic_sample": 20})
    (clear,) = await wait_for(lambda: client.of("clear"))
    await conn.send({"type": "turn_end", "text": "and another thing", "turn_id": 0})
    (second,) = await wait_for(lambda: client.of("utterance")[1:])
    assert first["epoch"] < clear["epoch"] < second["epoch"]
    assert adapter._attached[SESSION_ID].epoch == second["epoch"]


async def test_a_page_gets_no_epoch(adapter, fake_vm):
    async with connect(_url(adapter, "client=voice")) as ws:
        page = Client(ws)
        await wait_for(lambda: page.of("ready"))
        (conn,) = await fake_vm.wait_connections(1)
        await conn.send({"type": "turn_end", "text": "hello", "turn_id": 0})
        await conn.send({"type": "turn_end", "text": "hello again", "turn_id": 0})
        await wait_for(lambda: len(page.of("utterance")) == 2 and page.of("clear"))
        assert all("epoch" not in f for f in page.of("utterance") + page.of("clear"))
        page.task.cancel()


async def test_a_late_say_of_a_superseded_reply_never_reaches_the_next_one(ready, adapter):
    client, conn = ready
    session = adapter._attached[SESSION_ID]
    await conn.send({"type": "turn_end", "text": "first question", "turn_id": 0})
    (old,) = await wait_for(lambda: client.of("utterance"))
    await conn.send({"type": "turn_end", "text": "first question, no, a better one", "turn_id": 0})
    (new,) = await wait_for(lambda: client.of("utterance")[1:])
    reply_id = session.reply_id
    # Sent before the client got the second utterance: they belong to the first reply.
    await client.say("Old answer words. ", reply="old:0", epoch=old["epoch"])
    await client.say_end("old:0", epoch=old["epoch"])
    await client.say("New answer.", reply="new:0", epoch=new["epoch"])
    await client.say_end("new:0", epoch=new["epoch"])
    await wait_for(lambda: ended(conn) and "New answer." in "".join(f["text"] for f in tts_frames(conn)))
    (request,) = by_request(conn)
    assert "".join(f["text"] for f in request) == "New answer."
    assert session.reply_id == reply_id  # spoken as the reply the second utterance opened
    assert "Old" not in "".join(f["text"] for f in client.of("reply_delta"))


@pytest.mark.parametrize("fake_vm", ["turn_engine_unsupported"], indirect=True)
async def test_a_late_say_after_a_classic_barge_in_opens_no_reply(ready, adapter):
    client, conn = ready
    session = adapter._attached[SESSION_ID]
    await conn.send({"type": "transcription", "segment_index": 0, "text": "tell me a story",
                     "endpoint": True, "is_final": False, "turn_id": 1})
    (utterance,) = await wait_for(lambda: client.of("utterance"))
    await conn.send({"type": "transcription", "segment_index": 1, "text": "wait",
                     "endpoint": False, "is_final": False, "turn_id": 1})
    await wait_for(lambda: client.of("clear"))
    await asyncio.sleep(0.1)
    assert not session.reply_open
    # Sent before the client got the clear: the reply it belongs to was cut off.
    await client.say("Once upon a time. ", reply="old:0", epoch=utterance["epoch"])
    await client.say_end("old:0", epoch=utterance["epoch"])
    await asyncio.sleep(0.2)
    assert not session.reply_open
    assert tts_frames(conn) == []
    assert client.of("reply_delta") == []
    # Say text with the epoch of the last clear is spoken as before.
    current = client.of("clear")[-1]["epoch"]
    await client.say("Typed reply.", reply="typed:0", epoch=current)
    await client.say_end("typed:0", epoch=current)
    frames = await wait_for(lambda: ended(conn))
    assert "".join(f["text"] for f in frames) == "Typed reply."


@pytest.mark.parametrize("fake_vm", ["turn_engine_unsupported"], indirect=True)
async def test_a_classic_talk_over_closes_the_attached_reply_it_clears(ready, adapter):
    """The client cuts its reply on the clear; the session closes it too, so a late say
    of that key reaches no TTS request and the next key opens a reply of its own."""
    client, conn = ready
    session = adapter._attached[SESSION_ID]
    await conn.send({"type": "transcription", "segment_index": 0, "text": "tell me a story",
                     "endpoint": True, "is_final": False, "turn_id": 1})
    await wait_for(lambda: client.of("utterance"))
    await client.say("Once upon ", reply="m1")
    await wait_for(lambda: tts_frames(conn))
    assert session.reply_open and session.reply_key == "m1"
    await conn.send({"type": "transcription", "segment_index": 1, "text": "wait",
                     "endpoint": False, "is_final": False, "turn_id": 1})
    await wait_for(lambda: client.of("clear"))
    assert not session.reply_open
    assert session.reply_key is None
    assert session._finishing is None and session._flushing is None
    await client.say("a time. ", reply="m1")
    await asyncio.sleep(0.2)
    (first,) = by_request(conn)
    assert [f["text"] for f in first] == ["Once upon "]
    await client.say("Typed reply.", reply="m2")
    await client.say_end("m2")
    await wait_for(lambda: len(by_request(conn)) == 2 and by_request(conn)[1][-1]["done"])
    assert "".join(f["text"] for f in by_request(conn)[1]) == "Typed reply."
    assert "time" not in "".join(f["text"] for f in client.of("reply_delta"))


async def test_a_barge_on_a_finished_reply_tail_closes_the_open_reply(ready, adapter):
    """The user talks over the tail of a reply that was done while the client's next
    reply is open: the clear names the barged request, and the open reply is closed
    with its TTS request cancelled."""
    client, conn = ready
    session = adapter._attached[SESSION_ID]
    await client.say("Done.", reply="m1")
    await client.say_end("m1")
    (first,) = await wait_for(lambda: requests_ended(conn, 1))
    tail = first[0]["request_id"]
    await conn.send({"type": "audio_end", "request_id": tail, "reason": "complete"})
    await client.say("Two ", reply="m2")
    await wait_for(lambda: len(by_request(conn)) == 2)
    assert session.reply_open and session.reply_key == "m2"
    await conn.send({"type": "barge", "request_id": tail, "onset_mic_sample": 10, "mic_sample": 20})
    (clear,) = await wait_for(lambda: client.of("clear"))
    assert clear["request_id"] == tail
    assert not session.reply_open
    assert session.reply_key is None
    second = by_request(conn)[1][0]["request_id"]
    assert [f["request_id"] for f in conn.json_frames("tts_cancel")] == [second]
    await client.say("more. ", reply="m2")
    await asyncio.sleep(0.2)
    assert [f["text"] for f in by_request(conn)[1]] == ["Two "]
    await client.say("Three.", reply="m3")
    await client.say_end("m3")
    await wait_for(lambda: len(by_request(conn)) == 3 and by_request(conn)[2][-1]["done"])
    assert "".join(f["text"] for f in by_request(conn)[2]) == "Three."
    assert not any(f["done"] for f in by_request(conn)[1])  # the cut-off request is never ended


def test_the_finished_keys_kept_are_bounded(plugin):
    session_module = plugin.session
    session = session_module.VoiceSession(None, None, attached=True)
    for n in range(session_module.CLOSED_KEYS_KEPT + 30):
        session._retire(f"m{n}")
    assert len(session._closed_keys) == session_module.CLOSED_KEYS_KEPT
    assert f"m{session_module.CLOSED_KEYS_KEPT + 29}" in session._closed_keys
    assert "m0" not in session._closed_keys


async def test_an_attach_id_longer_than_256_characters_is_refused(adapter, fake_vm):
    async with connect(_url(adapter, "attach=" + "a" * 257)) as ws:
        with pytest.raises(ConnectionClosed) as info:
            while True:
                await asyncio.wait_for(ws.recv(), 2)
    assert info.value.rcvd.code == 1008
    assert "256" in info.value.rcvd.reason
    assert fake_vm.connections == []
    assert not adapter.is_attached("a" * 257)


async def test_an_empty_attach_id_is_refused(adapter, fake_vm):
    async with connect(_url(adapter, "attach=")) as ws:
        with pytest.raises(ConnectionClosed) as info:
            while True:
                await asyncio.wait_for(ws.recv(), 2)
    assert info.value.rcvd.code == 1008
    assert "empty" in info.value.rcvd.reason
    assert fake_vm.connections == []
    assert not adapter.is_attached("")


async def test_an_attach_id_of_256_characters_is_taken(adapter, fake_vm):
    async with connect(_url(adapter, "attach=" + "a" * 256)) as ws:
        client = Client(ws)
        await wait_for(lambda: client.of("ready"))
        assert adapter.is_attached("a" * 256)
        client.task.cancel()


class LaneStore:
    """The gateway's session store, as far as the attach needs it: every lane is on
    the one session given."""

    def __init__(self, session_id):
        self.session_id = session_id

    def peek_session_id(self, session_key):
        return self.session_id


async def test_an_attach_takes_the_session_from_the_page_on_it(adapter, fake_vm, plugin):
    adapter._session_store = LaneStore(SESSION_ID)
    async with connect(_url(adapter, "client=voice")) as page_ws:
        page = Client(page_ws)
        await wait_for(lambda: page.of("ready"))
        async with connect(_url(adapter, f"attach={SESSION_ID}")) as attach_ws:
            client = Client(attach_ws)
            await wait_for(lambda: client.of("ready"))
            await wait_for(lambda: page.of("replaced"))
            await asyncio.wait_for(page_ws.wait_closed(), 3)
            assert page_ws.close_code == plugin.server.REPLACED_CLOSE_CODE == 4001
            assert page.of("replaced") == [{"type": "replaced"}]
            await wait_for(lambda: adapter.session_for("voice") is None)
            assert adapter.is_attached(SESSION_ID)
            assert client.of("replaced") == []
            client.task.cancel()
        page.task.cancel()


async def test_a_page_on_another_session_is_not_replaced_by_an_attach(adapter, fake_vm):
    adapter._session_store = LaneStore("another")
    async with connect(_url(adapter, "client=voice")) as page_ws, \
            connect(_url(adapter, f"attach={SESSION_ID}")) as attach_ws:
        page, client = Client(page_ws), Client(attach_ws)
        await wait_for(lambda: page.of("ready") and client.of("ready"))
        await asyncio.sleep(0.2)
        assert page.of("replaced") == []
        assert not page.task.done()
        assert adapter.session_for("voice") is not None
        assert adapter.is_attached(SESSION_ID)
        page.task.cancel()
        client.task.cancel()
