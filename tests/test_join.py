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
import logging
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig
from gateway.slash_access import policy_for_source
from tools import slash_confirm

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
    send(), as the gateway's slash dispatch does, with ``reply_to`` naming the command's
    message as the gateway's reply anchor does. A ``gateway_reply`` of None is a command
    answered some other way (a confirmation the adapter took), so no text is sent."""
    adapter = plugin.adapter.VoiceLiveAdapter(_config(voicemanager_url=fake_vm.url))
    messages, vm_connections_at_dispatch = [], []

    async def handle_message(event):
        messages.append(event)
        vm_connections_at_dispatch.append(len(fake_vm.connections))
        if event.text.startswith("/") and adapter.gateway_reply is not None:
            await adapter.send(event.source.chat_id, adapter.gateway_reply, reply_to=event.message_id)

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
    # The connect notes the lane before its join step; the join check and the history
    # step each read the lane's key as well.
    lane_key = adapter._source_session_key(adapter._voice_source("tablet"))
    assert adapter._session_store.peeked == [lane_key, lane_key, lane_key]


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
    rows it was given, ``get_session`` giving a session's row from ``sessions`` (id to
    its ``parent_session_id``, ``end_reason`` and ``model_config``; a session not in it
    is unknown), and a record of the sessions counted, read and looked up."""

    def __init__(self, counts=None, rows=(), sessions=None):
        self.counts = counts
        self.rows = list(rows)
        self.sessions = sessions or {}
        self.counted = []  # sessions whose message count was asked for
        self.asked = []  # sessions whose messages were read
        self.looked_up = []  # sessions whose row was asked for

    def message_count(self, session_id):
        self.counted.append(session_id)
        if self.counts is None:
            return len(self.rows)
        return self.counts[session_id]

    def get_messages(self, session_id, limit=None, offset=0, latest=True):
        self.asked.append(session_id)
        return self.rows[offset:offset + limit] if limit else self.rows[offset:]

    def get_session(self, session_id):
        self.looked_up.append(session_id)
        if session_id not in self.sessions:
            return None
        row = self.sessions[session_id]
        return {"id": session_id, "parent_session_id": row.get("parent_session_id"),
                "end_reason": row.get("end_reason"), "model_config": row.get("model_config")}


def _compaction_chain(*ids):
    """The session rows of a compaction chain, tip first: each session points at the
    next with ``parent_session_id`` and every parent ended with ``compression``."""
    rows = {}
    for child, parent in zip(ids, ids[1:]):
        rows.setdefault(child, {})["parent_session_id"] = parent
        rows[parent] = {"end_reason": "compression"}
    rows.setdefault(ids[-1], {})
    return rows


def _runner_with_db(adapter, db):
    """A gateway runner that holds the DB behind an async facade, as the real one does."""
    adapter.gateway_runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))


async def test_a_connect_with_session_new_sends_new_when_the_lanes_session_has_messages(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 3})
    _runner_with_db(adapter, db)
    adapter.gateway_reply = "New session started."
    _move_on_new(adapter, "fresh")
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert [m.text for m in adapter.messages] == ["/new"]
    (event,) = adapter.messages
    assert event.source.chat_id == "tablet"
    assert db.counted == ["abc"]
    assert adapter.vm_connections_at_dispatch == [0]
    assert page.of("reply") == []  # the reply to /new stayed off the page
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
    # The lane before the step, the new-session step, then the history.
    assert adapter._session_store.peeked == [lane_key, lane_key, lane_key]
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


# --- a second page while the first one is still connecting ---------------------------------


def _hold_commands(adapter):
    """The gateway sits on every command until ``release`` is set, as a /resume that loads
    a long transcript does; ``dispatched`` is set when the command came in."""
    inner = adapter.handle_message
    dispatched, release = asyncio.Event(), asyncio.Event()

    async def handle_message(event):
        if event.text.startswith("/"):
            dispatched.set()
            await release.wait()
        await inner(event)

    adapter.handle_message = handle_message
    return dispatched, release


async def test_a_second_page_during_the_first_ones_resume_keeps_its_session_and_the_stale_connect_sends_nothing(
        adapter, fake_vm, plugin):
    adapter._session_store = FakeStore("abc")
    _with_db(adapter, FakeDB())
    dispatched, release = _hold_commands(adapter)
    first_ws = await connect(_url(adapter, "session=abc"))
    first = Page(first_ws)
    await asyncio.wait_for(dispatched.wait(), 3)
    # The same key (the default client id), a plain connect, while the first page waits.
    async with connect(_url(adapter, "client=phone")) as ws:
        second = Page(ws)
        await wait_for(lambda: second.of("ready"))
        assert await _replaced(first_ws, first) == plugin.server.REPLACED_CLOSE_CODE == 4001
        live = adapter.session_for("phone")
        assert live is not None
        release.set()
        await wait_for(lambda: len(adapter.messages) == 1)
        await asyncio.sleep(0.5)  # the first page's connect runs to its end
        assert first.frames == [{"type": "replaced"}]
        assert second.of("history") == [{"type": "history", "session_id": "abc", "lines": []}]
        assert len(second.of("ready")) == 1
        assert second.of("replaced") == []
        assert second.of("reply") == []
        assert ws.close_code is None
        assert adapter.session_for("phone") is live
        second.task.cancel()
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]


def _move_on_resume(adapter, session_id):
    """The gateway's /resume moves the lane onto ``session_id`` before it answers."""
    inner = adapter.handle_message

    async def handle_message(event):
        if event.text.startswith("/resume"):
            adapter._session_store.session_id = session_id
        await inner(event)

    adapter.handle_message = handle_message


async def test_a_second_page_gets_the_history_when_the_first_ones_held_resume_moves_the_lane(
        adapter, fake_vm, plugin):
    """The first page's /resume lands after a second page took the chat id: the lane
    moved under the live page, so the live page is told with a fresh history frame
    and the replaced connection gets nothing more."""
    adapter._session_store = FakeStore("old")
    _with_db(adapter, FakeDB())
    _move_on_resume(adapter, "abc")
    dispatched, release = _hold_commands(adapter)
    first_ws = await connect(_url(adapter, "session=abc"))
    first = Page(first_ws)
    await asyncio.wait_for(dispatched.wait(), 3)
    async with connect(_url(adapter, "client=phone")) as ws:
        second = Page(ws)
        await wait_for(lambda: second.of("ready"))
        assert await _replaced(first_ws, first) == plugin.server.REPLACED_CLOSE_CODE
        assert second.of("history") == [{"type": "history", "session_id": "old", "lines": []}]
        live = adapter.session_for("phone")
        release.set()
        await wait_for(lambda: len(second.of("history")) == 2)
        await asyncio.sleep(0.2)  # the first page's connect runs to its end
        kinds = [f["type"] for f in second.frames]
        assert kinds.index("ready") < kinds.index("history", kinds.index("ready"))
        assert second.of("history")[-1] == {"type": "history", "session_id": "abc", "lines": []}
        assert second.of("replaced") == []
        assert ws.close_code is None
        assert adapter.session_for("phone") is live
        assert first.frames == [{"type": "replaced"}]
        second.task.cancel()
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]


def _hold_first_history_read(plugin, monkeypatch):
    """The module's history reader sits on its first call until ``release`` is set, as a
    slow state DB would; ``started`` is set when that call came in. Later calls return
    at once."""
    import threading

    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    calls = []

    def fake_history_lines(db, session_id, limit=40):
        calls.append(session_id)
        if len(calls) == 1:
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
        return []

    monkeypatch.setattr(plugin.sessions, "history_lines", fake_history_lines)
    return started, release


async def test_a_second_page_gets_no_extra_history_when_the_replaced_connect_had_no_join_step(
        adapter, fake_vm, plugin, monkeypatch):
    """A plain connect has no join or new step, so the lane cannot have moved under
    the live page: it gets its own history frame and nothing more."""
    adapter._session_store = FakeStore("abc")
    _with_db(adapter, FakeDB())
    started, release = _hold_first_history_read(plugin, monkeypatch)
    first_ws = await connect(_url(adapter, "client=phone"))
    first = Page(first_ws)
    await asyncio.wait_for(started.wait(), 3)
    async with connect(_url(adapter, "client=phone")) as ws:
        second = Page(ws)
        await wait_for(lambda: second.of("ready"))
        assert await _replaced(first_ws, first) == plugin.server.REPLACED_CLOSE_CODE
        live = adapter.session_for("phone")
        assert live is not None
        release.set()
        await asyncio.sleep(0.5)  # the first page's connect runs to its end
        assert second.of("history") == [{"type": "history", "session_id": "abc", "lines": []}]
        assert len(second.of("ready")) == 1
        assert second.of("replaced") == []
        assert ws.close_code is None
        assert first.frames == [{"type": "replaced"}]
        # The stale connect registered no session of its own: the live page keeps
        # its session, and only one VoiceManager connection was opened.
        assert adapter.session_for("phone") is live
        assert len(fake_vm.connections) == 1
        second.task.cancel()
    assert adapter.messages == []


async def test_a_replaced_connect_whose_resume_lands_late_leaves_an_attached_client_alone(
        adapter, fake_vm, plugin):
    """A UI client attached to the session while the first page's /resume was held, then a
    second page took the chat id. The late /resume moves the lane onto the client's
    session, but the replaced connect is no voice device any more: it does not switch
    the client off, and the live page is told where its lane is now."""
    adapter._session_store = FakeStore("old")
    _with_db(adapter, FakeDB())
    _move_on_resume(adapter, "abc")
    dispatched, release = _hold_commands(adapter)
    first_ws = await connect(_url(adapter, "session=abc"))
    first = Page(first_ws)
    await asyncio.wait_for(dispatched.wait(), 3)
    attach_ws, client = await _attach(adapter, fake_vm, "abc")
    async with connect(_url(adapter, "client=phone")) as ws:
        second = Page(ws)
        await wait_for(lambda: second.of("ready"))
        assert await _replaced(first_ws, first) == plugin.server.REPLACED_CLOSE_CODE
        release.set()
        await wait_for(lambda: len(second.of("history")) == 2)
        await asyncio.sleep(0.2)  # the first page's connect runs to its end
        assert client.of("replaced") == []
        assert not client.task.done()
        assert attach_ws.close_code is None
        assert adapter.is_attached("abc")
        assert second.of("history")[-1] == {"type": "history", "session_id": "abc", "lines": []}
        assert first.frames == [{"type": "replaced"}]
        second.task.cancel()
    client.task.cancel()
    await attach_ws.close()


async def test_a_model_reply_that_lands_during_a_held_resume_is_a_reply_line_and_the_commands_reply_ends_the_join(
        adapter, fake_vm):
    """A page reloads mid-turn: the model's answer to the utterance before the reload
    finishes while the join's /resume waits. That answer is a reply to the utterance's
    message, not to the command's, so the page shows it and the join keeps waiting for the
    gateway's reply to /resume, which then stays off the page."""
    adapter._session_store = FakeStore("abc")
    dispatched, release = _hold_commands(adapter)
    async with connect(_url(adapter, "session=abc")) as ws:
        page = Page(ws)
        await asyncio.wait_for(dispatched.wait(), 3)
        result = await adapter.send("phone", "The answer to what you asked before.", reply_to="utterance-1")
        assert result.success
        await wait_for(lambda: page.of("reply"))
        assert page.of("ready") == []  # the join still waits on the command
        release.set()
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert page.of("reply") == [{"type": "reply", "text": "The answer to what you asked before."}]
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]


class FakeConnection:
    """A page socket that records what it was sent and the code it was closed with."""

    def __init__(self):
        self.sent, self.close_code = [], None

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def close(self, code=None, reason=None):
        self.close_code = code


async def test_join_failed_goes_to_the_connecting_socket_not_to_the_second_page_that_holds_the_key(adapter, plugin):
    adapter._session_store = FakeStore("old")
    adapter.gateway_reply = "Session not found: abc"
    stale, holder = FakeConnection(), FakeConnection()
    adapter._clients["phone"] = holder
    assert await adapter._join_session(stale, "phone", "abc") is False
    (failed,) = stale.sent
    assert failed["type"] == "join_failed"
    assert "abc" in failed["message"]
    assert stale.close_code == plugin.server.JOIN_FAILED_CLOSE_CODE
    assert holder.sent == []
    assert holder.close_code is None


# --- a new session asked for by voice ------------------------------------------------------


async def _spoken(ws, fake_vm, text):
    """The page sends audio, so its session connects to VoiceManager, which then hears
    ``text`` as a finished turn. Returns the fake's connection."""
    await ws.send(b"\x00\x00" * 160)
    (conn,) = await fake_vm.wait_connections(1)
    await conn.send({"type": "turn_end", "text": text, "turn_id": 0})
    return conn


def _move_on_new(adapter, session_id):
    """The gateway's /new moves the lane onto ``session_id`` before it answers."""
    inner = adapter.handle_message

    async def handle_message(event):
        if event.text == "/new":
            adapter._session_store.session_id = session_id
        await inner(event)

    adapter.handle_message = handle_message


async def test_new_session_by_voice_runs_new_and_sends_the_frames_on_the_same_socket(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 3, "fresh": 0})
    _with_db(adapter, db)
    adapter.gateway_reply = "New session started."
    _move_on_new(adapter, "fresh")
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        conn = await _spoken(ws, fake_vm, "new session")
        await wait_for(lambda: len(page.of("history")) == 2)
        await asyncio.sleep(0.1)
        assert [m.text for m in adapter.messages] == ["/new"]
        assert db.counted == ["abc"]
        assert page.of("session_new") == [{"type": "session_new", "session_id": "fresh"}]
        assert page.of("history")[-1] == {"type": "history", "session_id": "fresh", "lines": []}
        assert db.asked == ["abc", "fresh"]
        kinds = [f["type"] for f in page.frames]
        assert kinds[kinds.index("session_new") + 1] == "history"  # the fresh history follows at once
        assert kinds.count("ready") == 1  # the page did not reconnect
        assert page.of("reply") == []  # the reply to /new stayed off the page
        assert page.of("utterance") == []  # the command never went to Hermes
        # The same socket and session take the next utterance.
        await conn.send({"type": "turn_end", "text": "tell me a joke", "turn_id": 1})
        await wait_for(lambda: len(adapter.messages) == 2)
        assert adapter.messages[1].text == "tell me a joke"
        assert adapter.messages[1].source.chat_id == "tablet"
        assert [f["text"] for f in page.of("utterance")] == ["tell me a joke"]
        assert ws.close_code is None
        page.task.cancel()


async def test_new_session_by_voice_on_an_empty_session_sends_no_new_but_the_frames(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 0})
    _with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        await _spoken(ws, fake_vm, "new session")
        await wait_for(lambda: len(page.of("history")) == 2)
        await asyncio.sleep(0.1)
        assert adapter.messages == []
        assert db.counted == ["abc"]
        assert page.of("session_new") == [{"type": "session_new", "session_id": "abc"}]
        assert page.of("history")[-1] == {"type": "history", "session_id": "abc", "lines": []}
        kinds = [f["type"] for f in page.frames]
        assert kinds[kinds.index("session_new") + 1] == "history"  # the fresh history follows at once
        assert kinds.count("ready") == 1
        assert ws.close_code is None
        page.task.cancel()


async def test_new_session_by_voice_that_did_not_move_the_lane_tells_the_page_and_speaks_it(adapter, fake_vm, plugin):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 3})
    _with_db(adapter, db)
    adapter.gateway_reply = "Confirm /new: /approve, /always or /cancel"  # the gateway's text prompt; the lane stays
    notice = plugin.adapter.NEW_SESSION_FAILED_NOTICE
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        conn = await _spoken(ws, fake_vm, "new session")
        await wait_for(lambda: page.of("reply"))
        await wait_for(lambda: notice in "".join(f["text"] for f in conn.json_frames("tts_stream")))
        await asyncio.sleep(0.1)
        assert [m.text for m in adapter.messages] == ["/new"]
        assert page.of("session_new") == []  # the page is not told it is on a new session
        assert len(page.of("history")) == 1  # no second history frame either
        # Each spoken notice streams to the page as it is spoken, and a reply frame with
        # the line's own text ends that line: "New session" is ended before the failure
        # streams, and the failure's reply frame follows its last delta, so the page shows
        # two lines and no duplicate.
        first_reply = next(i for i, f in enumerate(page.frames) if f["type"] == "reply")
        before = [f["text"] for f in page.frames[:first_reply] if f["type"] == "reply_delta"]
        after = [f["text"] for f in page.frames[first_reply:] if f["type"] == "reply_delta"]
        assert "".join(before) == plugin.session.SESSION_NEW_NOTICE
        assert "".join(after) == notice
        assert page.of("reply") == [{"type": "reply", "text": plugin.session.SESSION_NEW_NOTICE},
                                    {"type": "reply", "text": notice}]
        kinds = [f["type"] for f in page.frames]
        assert len(kinds) - 1 - kinds[::-1].index("reply") > len(kinds) - 1 - kinds[::-1].index("reply_delta")
        # The notice is a TTS request of its own, ended at once, and leaves no reply
        # request open for the next turn.
        groups = {}
        for f in conn.json_frames("tts_stream"):
            groups.setdefault(f["request_id"], []).append(f)
        spoken = [g for g in groups.values() if notice in "".join(f["text"] for f in g)]
        assert len(spoken) == 1 and spoken[0][-1]["done"]
        assert adapter.session_for("tablet").reply_request is None
        assert page.of("utterance") == []
        assert [f["type"] for f in page.frames].count("ready") == 1  # the page did not reconnect
        # The same socket and session take the next utterance.
        await conn.send({"type": "turn_end", "text": "tell me a joke", "turn_id": 1})
        await wait_for(lambda: len(adapter.messages) == 2)
        assert adapter.messages[1].text == "tell me a joke"
        assert adapter.messages[1].source.chat_id == "tablet"
        assert ws.close_code is None
        page.task.cancel()


# --- a join judged by the compaction chain ---------------------------------------------------


CHAIN = {**_compaction_chain("tip", "mid", "abc"), "other": {}}


async def test_a_resume_of_a_compaction_ancestor_joins_the_lane_on_the_chains_tip(adapter, fake_vm):
    """Compaction moved the lane from abc to tip while the page was connected; the reload
    still carries session=abc. The gateway's /resume lands on the live tip, which is the
    same conversation, so the join passes."""
    adapter._session_store = FakeStore("tip")
    db = FakeDB(sessions=CHAIN)
    _with_db(adapter, db)
    adapter.gateway_reply = "Resumed session tip. Conversation restored."
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert [m.text for m in adapter.messages] == ["/resume abc --all"]
    assert page.of("join_failed") == []
    history = _one_history_frame_before_ready(page)
    assert history == {"type": "history", "session_id": "tip", "lines": []}
    assert db.looked_up == ["tip", "mid", "abc"]  # the ancestor's own row shows how it ended
    assert ws.close_code in (None, 1000)


async def test_a_resume_that_leaves_the_lane_on_an_unrelated_session_is_refused(adapter, fake_vm, plugin):
    adapter._session_store = FakeStore("other")
    db = FakeDB(sessions=CHAIN)
    _with_db(adapter, db)
    adapter.gateway_reply = "Session not found: abc"
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("join_failed"))
        await asyncio.wait_for(ws.wait_closed(), 3)
        page.task.cancel()
    assert ws.close_code == plugin.server.JOIN_FAILED_CLOSE_CODE == 4002
    assert page.of("ready") == []
    assert db.looked_up == ["other"]


async def test_a_resume_that_leaves_the_lane_on_the_sessions_reset_child_is_refused(adapter, fake_vm, plugin):
    """/new ended abc with session_reset and started child, which points back at abc
    like a compaction child does. A lane left on child is on another conversation:
    the page asked for abc and must not get child's history."""
    adapter._session_store = FakeStore("child")
    db = FakeDB(sessions={"child": {"parent_session_id": "abc"}, "abc": {"end_reason": "session_reset"}})
    _with_db(adapter, db)
    adapter.gateway_reply = "Session not found: abc"
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("join_failed"))
        await asyncio.wait_for(ws.wait_closed(), 3)
        page.task.cancel()
    assert ws.close_code == plugin.server.JOIN_FAILED_CLOSE_CODE == 4002
    assert page.of("ready") == []
    assert page.of("history") == []
    assert db.looked_up == ["child", "abc"]


async def test_a_resume_that_lands_on_the_requested_session_reads_no_lineage(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = FakeDB(sessions=CHAIN)
    _with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert page.of("join_failed") == []
    assert db.looked_up == []


def test_the_lineage_walk_finds_an_ancestor_and_not_an_unrelated_session(plugin):
    same = plugin.adapter._same_conversation
    assert same(FakeDB(sessions=CHAIN), "tip", "tip")
    assert same(FakeDB(sessions=CHAIN), "tip", "mid")
    assert same(FakeDB(sessions=CHAIN), "tip", "abc")
    assert not same(FakeDB(sessions=CHAIN), "abc", "tip")  # a descendant is not an ancestor
    assert not same(FakeDB(sessions=CHAIN), "other", "abc")
    assert not same(FakeDB(sessions=CHAIN), None, "abc")
    assert not same(FakeDB(sessions={}), "unknown", "abc")  # a session the DB has no row for


def test_the_lineage_walk_follows_a_parent_link_only_when_the_parent_ended_by_compression(plugin):
    """A /new, /branch or delegate child also points at its parent with
    parent_session_id; only a compaction child continues the parent's conversation.
    The gateway's own rule: the parent ended with compression and the child's
    model_config does not name the parent as what it was reset, branched or
    delegated from."""
    same = plugin.adapter._same_conversation

    def db(end_reason, model_config=None):
        return FakeDB(sessions={"child": {"parent_session_id": "abc", "model_config": model_config},
                                "abc": {"end_reason": end_reason}})

    assert same(db("compression"), "child", "abc")
    assert not same(db("session_reset"), "child", "abc")  # /new
    assert not same(db("branched"), "child", "abc")  # /branch
    assert not same(db(None), "child", "abc")  # the parent is still open: a delegate's parent
    for marker in ("_reset_from", "_branched_from", "_delegate_from"):
        assert not same(db("compression", json.dumps({marker: "abc"})), "child", "abc")
    # Compaction copies model_config onto its child: a marker naming another session does not count.
    assert same(db("compression", json.dumps({"_reset_from": "older"})), "child", "abc")
    assert same(db("compression", "not json"), "child", "abc")
    # The link is judged at every hop, not only at the first.
    rows = {**_compaction_chain("tip", "mid", "abc"), "abc": {"end_reason": "session_reset"}}
    assert same(FakeDB(sessions=rows), "tip", "mid")
    assert not same(FakeDB(sessions=rows), "tip", "abc")


def test_the_lineage_walk_stops_on_a_cycle(plugin):
    db = FakeDB(sessions={"tip": {"parent_session_id": "mid", "end_reason": "compression"},
                          "mid": {"parent_session_id": "tip", "end_reason": "compression"}})
    assert not plugin.adapter._same_conversation(db, "tip", "abc")
    assert db.looked_up == ["tip", "mid"]


def test_the_lineage_walk_stops_at_its_cap(plugin):
    cap = plugin.adapter.LINEAGE_HOPS
    rows = _compaction_chain(*(f"s{i}" for i in range(cap + 6)))
    assert plugin.adapter._same_conversation(FakeDB(sessions=rows), "s0", f"s{cap - 1}")
    db = FakeDB(sessions=rows)
    assert not plugin.adapter._same_conversation(db, "s0", f"s{cap}")
    assert len(db.looked_up) == cap


# --- the gateway's confirmation of /new ------------------------------------------------------


class ConfirmingGateway:
    """The gateway's destructive-slash gate, as far as /new needs it: the command registers a
    confirmation in ``tools.slash_confirm`` and offers it to the adapter; the lane moves onto
    ``session_id`` only when the confirmation is resolved with "once" or "always". When the
    adapter does not take the confirmation, the prompt text is the command's reply."""

    PROMPT = "Confirm /new: this starts a new session. /approve, /always or /cancel"

    def __init__(self, adapter, session_id):
        self.adapter, self.session_id = adapter, session_id
        self.choices, self.results, self.confirm_id = [], [], "7"
        inner = adapter.handle_message

        async def handle_message(event):
            if event.text == "/new":
                await self.confirm(event.source.chat_id)
            await inner(event)

        adapter.handle_message = handle_message

    def key(self, chat_id):
        return self.adapter._source_session_key(self.adapter._voice_source(chat_id))

    async def confirm(self, chat_id):
        async def handler(choice):
            self.choices.append(choice)
            if choice in ("once", "always"):
                self.adapter._session_store.session_id = self.session_id
                return "New session started."
            return "Cancelled."

        slash_confirm.register(self.key(chat_id), self.confirm_id, "new", handler)
        result = await self.adapter.send_slash_confirm(chat_id, "/new", self.PROMPT, self.key(chat_id),
                                                       self.confirm_id)
        self.results.append(result)
        if not result.success:
            await self.adapter.send(chat_id, self.PROMPT)


@pytest.fixture
def confirming(adapter):
    """The adapter's gateway asks for confirmation before /new; the lane moves to "fresh"."""
    gateway = ConfirmingGateway(adapter, "fresh")
    adapter.gateway_reply = None  # the confirmation step is the whole answer to /new
    yield gateway
    slash_confirm.clear(gateway.key("tablet"))


async def test_a_connect_with_session_new_passes_the_gateways_confirmation_of_new(adapter, fake_vm, confirming):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 3, "fresh": 0})
    _with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert [m.text for m in adapter.messages] == ["/new"]
    assert confirming.choices == ["once"]
    assert [r.success for r in confirming.results] == [True]
    assert adapter._session_store.session_id == "fresh"
    history = _one_history_frame_before_ready(page)
    assert history == {"type": "history", "session_id": "fresh", "lines": []}
    assert db.asked == ["fresh"]
    assert page.of("reply") == []  # neither the prompt nor the command's reply reached the page
    assert slash_confirm.get_pending(confirming.key("tablet")) is None


async def test_new_session_by_voice_passes_the_gateways_confirmation_of_new(adapter, fake_vm, confirming):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 3, "fresh": 0})
    _with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        await _spoken(ws, fake_vm, "new session")
        await wait_for(lambda: len(page.of("history")) == 2)
        await asyncio.sleep(0.1)
        assert [m.text for m in adapter.messages] == ["/new"]
        assert confirming.choices == ["once"]
        assert page.of("session_new") == [{"type": "session_new", "session_id": "fresh"}]
        assert page.of("history")[-1] == {"type": "history", "session_id": "fresh", "lines": []}
        assert db.asked == ["abc", "fresh"]
        assert page.of("reply") == []
        assert ws.close_code is None
        page.task.cancel()


async def test_a_confirm_that_is_not_for_a_plugin_command_is_not_supported(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    key = adapter._source_session_key(adapter._voice_source("tablet"))
    choices = []

    async def handler(choice):
        choices.append(choice)

    slash_confirm.register(key, "9", "new", handler)
    try:
        result = await adapter.send_slash_confirm("tablet", "/new", "Confirm /new", key, "9")
        assert not result.success
        assert result.error == "Not supported"
        assert choices == []
        assert slash_confirm.get_pending(key)["confirm_id"] == "9"  # left for the text prompt
    finally:
        slash_confirm.clear(key)


async def test_a_new_on_connect_that_did_not_move_the_lane_sends_the_old_history_then_the_failure_reply(
        adapter, fake_vm, plugin, caplog):
    adapter._session_store = FakeStore("abc")
    db = FakeDB({"abc": 3})
    _with_db(adapter, db)
    adapter.gateway_reply = "Confirm /new: /approve, /always or /cancel"  # the gateway's text prompt
    caplog.set_level(logging.WARNING)
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert [m.text for m in adapter.messages] == ["/new"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "/new" in r.getMessage()]
    assert len(warnings) == 1
    assert "tablet" in warnings[0].getMessage()
    assert "abc" in warnings[0].getMessage()
    history = _one_history_frame_before_ready(page)
    assert history["session_id"] == "abc"  # the page is still on the old session, and is told so
    assert page.of("reply") == [{"type": "reply", "text": plugin.adapter.NEW_SESSION_FAILED_NOTICE}]
    kinds = [f["type"] for f in page.frames]
    assert kinds.index("history") < kinds.index("reply") < kinds.index("ready")
    assert page.of("session_new") == []
