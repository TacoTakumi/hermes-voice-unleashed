"""Joining a Hermes session from the page.

The owner is the platform's slash admin, and a connect with ``session=<id>``
sends ``/resume <id> --all`` through the adapter's own message path before the
page is ready. The gateway's reply to that command stays off the page, and a
session that could not be joined closes the connection with its own code.

After the join step, every connect gets one history frame with the spoken
lines of the session the lane is on, before the ready frame.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig
from gateway.slash_access import policy_for_source

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"


def _config(**extra):
    base = {"token": TOKEN, "voicemanager_url": "http://127.0.0.1:1", "host": "127.0.0.1", "port": 0}
    base.update(extra)
    return PlatformConfig(enabled=True, extra=base)


class FakeStore:
    """The gateway's session store, as far as the join needs it: the session each lane key
    points at, and a record of the keys asked for."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.peeked = []

    def peek_session_id(self, session_key):
        self.peeked.append(session_key)
        return self.session_id


class Page:
    """Reads every JSON frame the page gets."""

    def __init__(self, ws):
        self.ws, self.frames = ws, []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            if not isinstance(message, bytes):
                self.frames.append(json.loads(message))

    def of(self, kind):
        return [f for f in self.frames if f.get("type") == kind]


@pytest.fixture
async def fake_vm():
    async with FakeVoiceManager() as fake:
        yield fake


@pytest.fixture
async def adapter(plugin, fake_vm):
    """An adapter whose gateway is a recording handle_message: it notes each event, how many
    VoiceManager connections existed when it ran, and answers a command at once through
    send(), as the gateway's slash dispatch does."""
    adapter = plugin.adapter.VoiceLiveAdapter(_config(voicemanager_url=fake_vm.url))
    messages, vm_connections_at_dispatch = [], []

    async def handle_message(event):
        messages.append(event)
        vm_connections_at_dispatch.append(len(fake_vm.connections))
        if event.text.startswith("/"):
            await adapter.send(event.source.chat_id, adapter.gateway_reply)

    adapter.handle_message = handle_message
    adapter.messages = messages
    adapter.vm_connections_at_dispatch = vm_connections_at_dispatch
    adapter.gateway_reply = "Resumed session abc. Conversation restored."
    assert await adapter.connect()
    yield adapter
    await adapter.disconnect()


def _url(adapter, query):
    return f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&{query}"


async def test_a_connect_with_a_session_sends_resume_before_the_page_is_ready(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]
    (event,) = adapter.messages
    assert event.source.chat_id == "tablet"
    assert event.source.platform == adapter.platform
    # The ready frame follows the VoiceManager connection, which did not exist yet.
    assert adapter.vm_connections_at_dispatch == [0]
    # The join check and the history step each read the lane's key.
    lane_key = adapter._source_session_key(adapter._voice_source("tablet"))
    assert adapter._session_store.peeked == [lane_key, lane_key]


async def test_a_connect_without_a_session_sends_no_command(adapter, fake_vm):
    adapter._session_store = FakeStore("whatever")
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert adapter.messages == []


async def test_the_reply_to_the_resume_command_is_swallowed_and_a_later_reply_is_not(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        assert page.of("reply") == []
        result = await adapter.send("tablet", "hello there")
        await wait_for(lambda: page.of("reply"))
        page.task.cancel()
    assert result.success
    assert page.of("reply") == [{"type": "reply", "text": "hello there"}]


async def test_a_session_that_was_not_joined_is_refused_with_its_own_close_code(adapter, fake_vm, plugin):
    adapter._session_store = FakeStore("old")
    adapter.gateway_reply = "Session not found: abc"
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("join_failed"))
        await asyncio.wait_for(ws.wait_closed(), 3)
        page.task.cancel()
    assert ws.close_code == plugin.server.JOIN_FAILED_CLOSE_CODE == 4002
    (failed,) = page.of("join_failed")
    assert failed["message"]
    assert page.of("ready") == []
    assert page.of("reply") == []
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]
    assert fake_vm.connections == []


class FakeDB:
    """The profile's state DB, as far as the connect steps need it: the stored message
    count per session (else the number of rows given), ``get_messages`` returning the
    rows it was given, and a record of the sessions counted and read."""

    def __init__(self, counts=None, rows=()):
        self.counts = counts
        self.rows = list(rows)
        self.counted = []  # sessions whose message count was asked for
        self.asked = []  # sessions whose messages were read

    def message_count(self, session_id):
        self.counted.append(session_id)
        if self.counts is None:
            return len(self.rows)
        return self.counts[session_id]

    def get_messages(self, session_id, limit=None, offset=0, latest=True):
        self.asked.append(session_id)
        return self.rows[offset:offset + limit] if limit else self.rows[offset:]


def _runner_with_db(adapter, db):
    """A gateway runner that holds the DB behind an async facade, as the real one does."""
    adapter.gateway_runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))


async def test_a_connect_with_session_new_sends_new_when_the_lanes_session_has_messages(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 3})
    _runner_with_db(adapter, db)
    adapter.gateway_reply = "New session started."
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert [m.text for m in adapter.messages] == ["/new"]
    (event,) = adapter.messages
    assert event.source.chat_id == "tablet"
    assert db.counted == ["abc"]
    assert adapter.vm_connections_at_dispatch == [0]
    assert page.of("reply") == []
    assert page.of("join_failed") == []


async def test_a_connect_with_session_new_sends_nothing_when_the_lanes_session_is_empty(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 0})
    _runner_with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert adapter.messages == []
    assert db.counted == ["abc"]
    assert adapter._session_store.session_id == "abc"
    lane_key = adapter._source_session_key(adapter._voice_source("tablet"))
    assert adapter._session_store.peeked == [lane_key, lane_key]  # the new-session step, then the history
    assert page.of("join_failed") == []


async def test_a_connect_with_session_new_sends_nothing_when_the_lane_has_no_session(adapter, fake_vm):
    adapter._session_store = FakeStore(None)
    db = FakeDB({})
    _runner_with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert adapter.messages == []
    assert db.counted == []
    assert page.of("join_failed") == []


def test_the_owner_is_the_slash_admin_without_configuration(plugin):
    config = _config()
    adapter = plugin.adapter.VoiceLiveAdapter(config)
    assert config.extra["allow_admin_from"] == [plugin.adapter.OWNER]
    gateway_config = SimpleNamespace(platforms={adapter.platform: config})
    policy = policy_for_source(gateway_config, adapter._voice_source("kitchen"))
    assert policy.enabled
    assert policy.is_admin(plugin.adapter.OWNER)


def test_a_configured_admin_list_is_kept(plugin):
    config = _config(allow_admin_from=["alice"])
    plugin.adapter.VoiceLiveAdapter(config)
    assert config.extra["allow_admin_from"] == ["alice"]


def _with_db(adapter, db):
    """The runner's state DB sits behind an async facade; the adapter unwraps it."""
    adapter.gateway_runner = SimpleNamespace(session_store=adapter._session_store,
                                             _session_db=SimpleNamespace(_db=db))


def _record_history_lines(plugin, monkeypatch, lines):
    """Replace the module's history reader with one that records its arguments."""
    calls = []

    def fake_history_lines(db, session_id, limit=40):
        calls.append((db, session_id))
        return lines

    monkeypatch.setattr(plugin.sessions, "history_lines", fake_history_lines)
    return calls


def _one_history_frame_before_ready(page):
    (history,) = page.of("history")
    kinds = [f.get("type") for f in page.frames]
    assert kinds.index("history") < kinds.index("ready")
    return history


LINES = [{"role": "user", "text": "hello"}, {"role": "hermes", "text": "hi there"}]


async def test_a_plain_connect_gets_the_history_of_the_lanes_session_before_ready(adapter, fake_vm, plugin, monkeypatch):
    adapter._session_store = FakeStore("abc")
    db = FakeDB()
    _with_db(adapter, db)
    calls = _record_history_lines(plugin, monkeypatch, LINES)
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    history = _one_history_frame_before_ready(page)
    assert history == {"type": "history", "session_id": "abc", "lines": LINES}
    assert calls == [(db, "abc")]
    assert adapter.messages == []


async def test_a_session_connect_gets_the_history_of_the_joined_session_before_ready(adapter, fake_vm, plugin, monkeypatch):
    adapter._session_store = FakeStore("abc")
    db = FakeDB()
    _with_db(adapter, db)
    calls = _record_history_lines(plugin, monkeypatch, LINES)
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    history = _one_history_frame_before_ready(page)
    assert history == {"type": "history", "session_id": "abc", "lines": LINES}
    assert calls == [(db, "abc")]
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]


async def test_a_new_session_connect_gets_an_empty_history_before_ready(adapter, fake_vm):
    adapter._session_store = FakeStore("fresh")
    db = FakeDB()  # the new session has no rows
    _with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    history = _one_history_frame_before_ready(page)
    assert history == {"type": "history", "session_id": "fresh", "lines": []}
    assert db.asked == ["fresh"]
    assert not any(m.text.startswith("/resume") for m in adapter.messages)


async def test_the_history_lines_come_from_the_stored_rows(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = FakeDB(rows=[
        {"role": "user", "content": "hello"},
        {"role": "tool", "content": "ignored"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
    ])
    _with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    history = _one_history_frame_before_ready(page)
    assert history["lines"] == LINES


async def test_a_failed_history_read_sends_an_empty_history_and_the_page_still_connects(adapter, fake_vm, plugin, monkeypatch):
    adapter._session_store = FakeStore("abc")
    _with_db(adapter, FakeDB())

    def broken(db, session_id, limit=40):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(plugin.sessions, "history_lines", broken)
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    history = _one_history_frame_before_ready(page)
    assert history == {"type": "history", "session_id": "abc", "lines": []}


async def test_without_a_session_store_the_history_is_empty(adapter, fake_vm, plugin, monkeypatch):
    calls = _record_history_lines(plugin, monkeypatch, LINES)
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    history = _one_history_frame_before_ready(page)
    assert history == {"type": "history", "session_id": None, "lines": []}
    assert calls == []


async def _attach(adapter, fake_vm, session_id):
    """A UI client with voice attached to ``session_id``, its ready frame seen."""
    ws = await connect(_url(adapter, f"attach={session_id}"))
    client = Page(ws)
    await wait_for(lambda: client.of("ready"))
    await wait_for(lambda: adapter.is_attached(session_id))
    return ws, client


async def _replaced(ws, client):
    """The frames and close code of a client the adapter switched off."""
    await wait_for(lambda: client.of("replaced"))
    await asyncio.wait_for(ws.wait_closed(), 3)
    client.task.cancel()
    return ws.close_code


@pytest.mark.parametrize("query", ["client=tablet", "client=tablet&session=abc"])
async def test_a_page_connect_takes_the_session_from_the_attached_client(adapter, fake_vm, plugin, query):
    adapter._session_store = FakeStore("abc")
    _with_db(adapter, FakeDB())
    attach_ws, client = await _attach(adapter, fake_vm, "abc")
    async with connect(_url(adapter, query)) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        assert await _replaced(attach_ws, client) == plugin.server.REPLACED_CLOSE_CODE == 4001
        assert client.of("replaced") == [{"type": "replaced"}]
        await wait_for(lambda: not adapter.is_attached("abc"))
        assert adapter.session_for("tablet") is not None
        assert page.of("replaced") == []
        page.task.cancel()
    _one_history_frame_before_ready(page)


async def test_a_client_attached_to_another_session_is_not_replaced_by_a_page_connect(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    _with_db(adapter, FakeDB())
    attach_ws, client = await _attach(adapter, fake_vm, "other")
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        await asyncio.sleep(0.2)
        assert client.of("replaced") == []
        assert not client.task.done()
        assert adapter.is_attached("other")
        page.task.cancel()
    client.task.cancel()
    await attach_ws.close()
