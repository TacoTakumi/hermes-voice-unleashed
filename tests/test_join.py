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
    rows it was given, ``get_session`` giving a session's parent link from ``parents``
    (a session not in it is unknown), and a record of the sessions counted, read and
    looked up."""

    def __init__(self, counts=None, rows=(), parents=None):
        self.counts = counts
        self.rows = list(rows)
        self.parents = parents or {}
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
        if session_id not in self.parents:
            return None
        return {"id": session_id, "parent_session_id": self.parents[session_id]}


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


# --- a join judged by the compaction chain ---------------------------------------------------


CHAIN = {"tip": "mid", "mid": "abc", "abc": None, "other": None}


async def test_a_resume_of_a_compaction_ancestor_joins_the_lane_on_the_chains_tip(adapter, fake_vm):
    """Compaction moved the lane from abc to tip while the page was connected; the reload
    still carries session=abc. The gateway's /resume lands on the live tip, which is the
    same conversation, so the join passes."""
    adapter._session_store = FakeStore("tip")
    db = FakeDB(parents=CHAIN)
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
    assert db.looked_up == ["tip", "mid"]  # the walk stops at the requested ancestor
    assert ws.close_code in (None, 1000)


async def test_a_resume_that_leaves_the_lane_on_an_unrelated_session_is_refused(adapter, fake_vm, plugin):
    adapter._session_store = FakeStore("other")
    db = FakeDB(parents=CHAIN)
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


async def test_a_resume_that_lands_on_the_requested_session_reads_no_lineage(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = FakeDB(parents=CHAIN)
    _with_db(adapter, db)
    async with connect(_url(adapter, "client=tablet&session=abc")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert page.of("join_failed") == []
    assert db.looked_up == []


def test_the_lineage_walk_finds_an_ancestor_and_not_an_unrelated_session(plugin):
    same = plugin.adapter._same_conversation
    assert same(FakeDB(parents=CHAIN), "tip", "tip")
    assert same(FakeDB(parents=CHAIN), "tip", "mid")
    assert same(FakeDB(parents=CHAIN), "tip", "abc")
    assert not same(FakeDB(parents=CHAIN), "abc", "tip")  # a descendant is not an ancestor
    assert not same(FakeDB(parents=CHAIN), "other", "abc")
    assert not same(FakeDB(parents=CHAIN), None, "abc")
    assert not same(FakeDB(parents={}), "unknown", "abc")  # a session the DB has no row for


def test_the_lineage_walk_stops_on_a_cycle(plugin):
    db = FakeDB(parents={"tip": "mid", "mid": "tip"})
    assert not plugin.adapter._same_conversation(db, "tip", "abc")
    assert db.looked_up == ["tip", "mid"]


def test_the_lineage_walk_stops_at_its_cap(plugin):
    cap = plugin.adapter.LINEAGE_HOPS
    parents = {f"s{i}": f"s{i + 1}" for i in range(cap + 5)}
    assert plugin.adapter._same_conversation(FakeDB(parents=parents), "s0", f"s{cap - 1}")
    db = FakeDB(parents=parents)
    assert not plugin.adapter._same_conversation(db, "s0", f"s{cap}")
    assert len(db.looked_up) == cap
