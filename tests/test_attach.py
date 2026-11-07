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

    async def say(self, *pieces, reply=None):
        for piece in pieces:
            await self.ws.send(json.dumps({"type": "say", "text": piece,
                                           **({"reply": reply} if reply else {})}))

    async def say_end(self, reply=None):
        await self.ws.send(json.dumps({"type": "say_end", **({"reply": reply} if reply else {})}))


@pytest.fixture
async def fake_vm():
    async with FakeVoiceManager() as fake:
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
        assert not adapter.is_attached("phone")
        assert adapter.session_for(SESSION_ID) is None  # not a page: no hook text routes to it
        client.task.cancel()


async def test_an_utterance_goes_to_the_client_only(attached, adapter):
    client, conn = attached
    assert client.of("utterance") == [{"type": "utterance", "text": "tell me something"}]
    await asyncio.sleep(0.1)
    assert adapter.messages == []


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
    async with connect(_url(adapter, "client=phone")) as ws:
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


async def test_an_attach_id_of_256_characters_is_taken(adapter, fake_vm):
    async with connect(_url(adapter, "attach=" + "a" * 256)) as ws:
        client = Client(ws)
        await wait_for(lambda: client.of("ready"))
        assert adapter.is_attached("a" * 256)
        client.task.cancel()
