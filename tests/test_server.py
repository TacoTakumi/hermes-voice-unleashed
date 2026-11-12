"""The page and its WebSocket are reachable only with the access token."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from gateway.config import PlatformConfig

TOKEN = "s3cret-token"


def _config(**extra):
    base = {"token": TOKEN, "voicemanager_url": "http://127.0.0.1:1", "host": "127.0.0.1", "port": 0}
    base.update(extra)
    return PlatformConfig(enabled=True, extra=base)


@pytest.fixture
async def adapter(plugin):
    adapter = plugin.adapter.VoiceLiveAdapter(_config())
    calls = []

    async def record(connection, client_id):
        calls.append(client_id)
        async for _ in connection:
            pass

    adapter._run_client = record
    adapter.client_calls = calls
    assert await adapter.connect()
    yield adapter
    await adapter.disconnect()


def _ws_url(adapter, token=None, client=None):
    query = []
    if token is not None:
        query.append(f"token={token}")
    if client is not None:
        query.append(f"client={client}")
    return f"ws://127.0.0.1:{adapter.bound_port}/ws" + ("?" + "&".join(query) if query else "")


@pytest.mark.parametrize("token", [None, "", "wrong"])
async def test_websocket_without_the_right_token_is_refused(adapter, token):
    with pytest.raises(InvalidStatus) as info:
        async with connect(_ws_url(adapter, token)):
            pass
    assert info.value.response.status_code == 401
    await asyncio.sleep(0.05)
    assert adapter.client_calls == []


async def test_websocket_with_the_token_reaches_a_client_session(adapter):
    async with connect(_ws_url(adapter, TOKEN, "kitchen")):
        for _ in range(50):
            if adapter.client_calls:
                break
            await asyncio.sleep(0.01)
    assert adapter.client_calls == ["kitchen"]


async def test_page_needs_the_token(adapter):
    base = f"http://127.0.0.1:{adapter.bound_port}/"
    async with httpx.AsyncClient() as http:
        refused = await http.get(base)
        wrong = await http.get(base, params={"token": "nope"})
        page = await http.get(base, params={"token": TOKEN})
    assert refused.status_code == 401
    assert wrong.status_code == 401
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<html" in page.text


async def test_send_reaches_the_connected_page(adapter):
    async with connect(_ws_url(adapter, TOKEN, "voice")) as ws:
        for _ in range(50):
            if adapter.client_calls:
                break
            await asyncio.sleep(0.01)
        result = await adapter.send("voice", "hello there")
        frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
    assert result.success
    assert frame == {"type": "reply", "text": "hello there"}


async def test_connect_refuses_without_a_token(plugin):
    adapter = plugin.adapter.VoiceLiveAdapter(_config(token=""))
    assert not await adapter.connect()


async def test_the_variable_names_of_releases_before_the_rename_are_not_read(plugin, monkeypatch):
    monkeypatch.setenv("VMLIVE_TOKEN", "old-token")
    monkeypatch.setenv("VMLIVE_VOICEMANAGER_URL", "http://127.0.0.1:8255")
    adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={}))
    assert not await adapter.connect()
    assert adapter.fatal_error_code == "config_missing"
    assert plugin.adapter._env_enablement() is None


def test_register_adds_one_platform(plugin):
    class Ctx:
        def __init__(self):
            self.platforms, self.hooks = [], []

        def register_platform(self, **kwargs):
            self.platforms.append(kwargs)

        def register_hook(self, name, callback):
            self.hooks.append(name)

    ctx = Ctx()
    plugin.register(ctx)
    assert [p["name"] for p in ctx.platforms] == ["voice_unleashed"]
    entry = ctx.platforms[0]
    assert entry["adapter_factory"] is plugin.adapter.VoiceLiveAdapter
    assert entry["allowed_users_env"] == "VOICE_UNLEASHED_ALLOWED_USERS"
    assert sorted(ctx.hooks) == ["on_session_end", "on_stream_delta", "on_stream_end", "post_tool_call",
                                 "pre_llm_call", "pre_tool_call"]


def test_gateway_tool_progress_is_off_by_default_for_the_voice_platform(plugin, monkeypatch):
    """The activity panel shows tool calls; the gateway's progress messages would show them twice."""
    from dataclasses import fields

    from gateway import display_config
    from gateway.platform_registry import PlatformEntry, platform_registry

    class Ctx:
        def register_platform(self, **kwargs):
            self.entry = kwargs

        def register_hook(self, name, callback):
            pass

    ctx = Ctx()
    plugin.register(ctx)
    assert ctx.entry["display_overrides"] == {"tool_progress": "off"}
    known = {f.name for f in fields(PlatformEntry)}
    entry = PlatformEntry(**{k: v for k, v in ctx.entry.items() if k in known})
    monkeypatch.setattr(platform_registry, "get", lambda name: entry if name == "voice_unleashed" else None)
    assert display_config.resolve_tool_progress({}, "voice_unleashed") == ("off", False)
    configured = {"display": {"platforms": {"voice_unleashed": {"tool_progress": "all"}}}}
    assert display_config.resolve_tool_progress(configured, "voice_unleashed")[0] == "all"


# GET /sessions: the session list the page shows, answered from a callback the adapter supplies.

ROWS = [{"id": "abc123", "title": None, "preview": "hello there", "last_active": 1_700_000_000.0,
         "message_count": 2, "source": "voice_unleashed", "current": True, "voice_attached": False}]


@pytest.fixture
async def sessions_server(plugin):
    """A voice server whose sessions callback is a fake that records the lane it was asked for."""
    calls = []

    async def no_client(connection, client_id):
        pass

    def rows(client_id):
        calls.append(client_id)
        return ROWS

    server = plugin.server.VoiceServer("127.0.0.1", 0, TOKEN, no_client, on_sessions=rows)
    server.sessions_calls = calls
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_sessions_route_answers_the_callbacks_rows_for_the_lane(sessions_server):
    url = f"http://127.0.0.1:{sessions_server.bound_port}/sessions"
    async with httpx.AsyncClient() as http:
        answer = await http.get(url, params={"token": TOKEN, "client": "tablet"})
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "application/json"
    assert answer.headers["cache-control"] == "no-store"
    assert answer.json() == ROWS
    assert sessions_server.sessions_calls == ["tablet"]


async def test_sessions_route_defaults_to_the_voice_lane(sessions_server):
    url = f"http://127.0.0.1:{sessions_server.bound_port}/sessions"
    async with httpx.AsyncClient() as http:
        answer = await http.get(url, params={"token": TOKEN})
    assert answer.status_code == 200
    assert sessions_server.sessions_calls == ["voice"]


@pytest.mark.parametrize("token", [None, "", "wrong"])
async def test_sessions_route_needs_the_token(sessions_server, token):
    url = f"http://127.0.0.1:{sessions_server.bound_port}/sessions"
    params = {"client": "tablet"}
    if token is not None:
        params["token"] = token
    async with httpx.AsyncClient() as http:
        answer = await http.get(url, params=params)
    assert answer.status_code == 401
    assert sessions_server.sessions_calls == []


async def test_sessions_route_without_a_callback_is_not_found(plugin):
    async def no_client(connection, client_id):
        pass

    server = plugin.server.VoiceServer("127.0.0.1", 0, TOKEN, no_client)
    await server.start()
    try:
        url = f"http://127.0.0.1:{server.bound_port}/sessions"
        async with httpx.AsyncClient() as http:
            answer = await http.get(url, params={"token": TOKEN, "client": "tablet"})
    finally:
        await server.stop()
    assert answer.status_code == 404


def test_adapter_sessions_callback_reads_the_runners_store_and_db(plugin, monkeypatch):
    """The adapter lists sessions from the runner's store and state DB for the lane's key.
    The runner holds the DB behind an async facade; the plain DB is what the reader needs."""
    adapter = plugin.adapter.VoiceLiveAdapter(_config())
    store, db = object(), object()
    adapter.gateway_runner = SimpleNamespace(session_store=store, _session_db=SimpleNamespace(_db=db))
    seen = {}

    def fake_rows(store_, db_, lane_key, limit=20):
        seen.update(store=store_, db=db_, lane_key=lane_key)
        return [{"id": "x"}]

    monkeypatch.setattr(plugin.sessions, "session_rows", fake_rows)
    assert adapter._session_rows("tablet") == [{"id": "x", "voice_attached": False}]
    assert seen["store"] is store
    assert seen["db"] is db
    assert seen["lane_key"] == adapter._source_session_key(adapter._voice_source("tablet"))
    assert seen["lane_key"] != adapter._source_session_key(adapter._voice_source("voice"))


def test_adapter_sessions_callback_marks_the_row_whose_session_has_voice_attached(plugin, monkeypatch):
    """One voice device per session: the row of a session a UI client has voice
    attached to carries voice_attached true, every other row false."""
    adapter = plugin.adapter.VoiceLiveAdapter(_config())
    adapter.gateway_runner = SimpleNamespace(session_store=object(), _session_db=SimpleNamespace(_db=object()))
    adapter._attached["b"] = object()

    def fake_rows(store_, db_, lane_key, limit=20):
        return [{"id": "a", "current": True}, {"id": "b", "current": False}, {"id": "c", "current": False}]

    monkeypatch.setattr(plugin.sessions, "session_rows", fake_rows)
    rows = adapter._session_rows("tablet")
    assert [(row["id"], row["voice_attached"]) for row in rows] == [("a", False), ("b", True), ("c", False)]
    assert rows[0]["current"] is True  # the other fields stay as the reader gave them


def test_adapter_sessions_callback_opens_the_state_db_read_only_without_a_runner_db(plugin, monkeypatch):
    adapter = plugin.adapter.VoiceLiveAdapter(_config())
    adapter.gateway_runner = SimpleNamespace(session_store=object())
    opened = []

    class FakeDB:
        def __init__(self, db_path=None, read_only=False):
            opened.append(read_only)
            self.closed = False

        def close(self):
            self.closed = True

    seen = {}

    def fake_rows(store_, db_, lane_key, limit=20):
        seen["db"] = db_
        return []

    monkeypatch.setattr(plugin.adapter, "SessionDB", FakeDB)
    monkeypatch.setattr(plugin.sessions, "session_rows", fake_rows)
    assert adapter._session_rows("tablet") == []
    assert opened == [True]
    assert isinstance(seen["db"], FakeDB) and seen["db"].closed


async def test_adapter_wires_the_sessions_callback_into_the_server(plugin, monkeypatch):
    adapter = plugin.adapter.VoiceLiveAdapter(_config())

    class FakeServer:
        def __init__(self, host, port, token, on_client, **kwargs):
            self.kwargs = kwargs

        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(plugin.adapter, "VoiceServer", FakeServer)
    assert await adapter.connect()
    try:
        assert adapter._server.kwargs["on_sessions"] == adapter._session_rows
    finally:
        await adapter.disconnect()


# The session query of a websocket connect: its value becomes the id of a /resume
# on the gateway, so a shape that is not a Hermes session id is refused before
# the client handler runs.


@pytest.fixture
async def join_server(plugin):
    """A voice server whose client handler records the lane and options it was given."""
    calls = []

    async def record(connection, client_id, **options):
        calls.append((client_id, options))
        async for _ in connection:
            pass

    server = plugin.server.VoiceServer("127.0.0.1", 0, TOKEN, record)
    server.client_calls = calls
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _join_url(server, session):
    return f"ws://127.0.0.1:{server.bound_port}/ws?token={TOKEN}&client=tablet&session={quote(session)}"


async def _refused_connect(server, session):
    """Connect with the session value; return the error frame and the close code."""
    async with connect(_join_url(server, session)) as ws:
        frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
        with pytest.raises(ConnectionClosed) as info:
            while True:
                await asyncio.wait_for(ws.recv(), 2)
    await asyncio.sleep(0.05)
    return frame, info.value.rcvd


async def test_an_empty_session_query_is_refused(join_server):
    frame, close = await _refused_connect(join_server, "")
    assert frame["type"] == "error" and "empty" in frame["message"]
    assert close.code == 1008
    assert join_server.client_calls == []


async def test_a_session_query_longer_than_256_characters_is_refused(join_server):
    frame, close = await _refused_connect(join_server, "a" * 257)
    assert frame["type"] == "error" and "256" in frame["message"]
    assert close.code == 1008
    assert join_server.client_calls == []


@pytest.mark.parametrize("session", ["20261107 101500", "20261107_'ab", 'id"x'])
async def test_a_session_query_with_a_character_outside_the_id_alphabet_is_refused(join_server, session):
    frame, close = await _refused_connect(join_server, session)
    assert frame["type"] == "error" and "character" in frame["message"]
    assert close.code == 1008
    assert join_server.client_calls == []


async def test_a_well_formed_session_query_of_256_characters_reaches_the_client_handler(join_server):
    session = ("20261107_101500_" + "Ab-9_" * 60)[:256]
    assert len(session) == 256
    async with connect(_join_url(join_server, session)):
        for _ in range(50):
            if join_server.client_calls:
                break
            await asyncio.sleep(0.01)
    assert join_server.client_calls == [("tablet", {"session": session})]


# --- the lane's old page when a new page connects ------------------------------------------


class SlowClose:
    """A page socket whose close handshake does not end until the test lets it: a phone
    that changed networks answers no close frame, and the server's close waits for it
    for its close timeout."""

    def __init__(self):
        self.frames, self.close_code, self.let_go = [], None, asyncio.Event()

    async def send(self, data):
        self.frames.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.close_code = code
        await self.let_go.wait()


def _lane_url(server, client="tablet", **query):
    extra = "".join(f"&{key}={quote(str(value))}" for key, value in query.items())
    return f"ws://127.0.0.1:{server.bound_port}/ws?token={TOKEN}&client={client}{extra}"


async def _until(predicate, timeout=1.0):
    """Wait until ``predicate`` holds; fail when it does not in ``timeout`` seconds."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_a_page_starts_at_once_when_the_lanes_old_page_does_not_answer_its_close(join_server, plugin):
    old = SlowClose()
    join_server._pages[("client", "tablet")] = old
    try:
        async with connect(_lane_url(join_server)):
            await _until(lambda: join_server.client_calls)  # under 1 s, not the close timeout
            await _until(lambda: old.close_code == plugin.server.REPLACED_CLOSE_CODE)
            assert old.frames == [{"type": "replaced"}]
            assert join_server._pages[("client", "tablet")] is not old
    finally:
        old.let_go.set()


async def test_the_lanes_old_page_that_answers_gets_the_replaced_frame_and_its_close_code(join_server, plugin):
    async with connect(_lane_url(join_server)) as first:
        await _until(lambda: len(join_server.client_calls) == 1)
        async with connect(_lane_url(join_server)):
            await _until(lambda: len(join_server.client_calls) == 2)
            assert json.loads(await asyncio.wait_for(first.recv(), 2)) == {"type": "replaced"}
            with pytest.raises(ConnectionClosed) as info:
                await asyncio.wait_for(first.recv(), 2)
            assert info.value.rcvd.code == plugin.server.REPLACED_CLOSE_CODE == 4001


# --- a page that connects by itself after its lane was taken -------------------------------
# The page names itself with a page id on every connect and marks a connect that it made
# by itself after a lost socket. A page that another one replaced while it was off the
# network never got its replaced frame: its own reconnect gets that frame and takes nothing.


async def _replaced_answer(ws):
    """The replaced frame and the close code of a connect that the server turned away."""
    frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
    with pytest.raises(ConnectionClosed) as info:
        await asyncio.wait_for(ws.recv(), 2)
    return frame, info.value.rcvd.code


async def _nothing_arrives(ws, wait=0.2):
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws.recv(), wait)


async def test_a_marked_connect_of_a_page_that_the_lane_replaced_gets_the_replaced_frame(join_server, plugin):
    first = await connect(_lane_url(join_server, page="page-a"))
    await _until(lambda: len(join_server.client_calls) == 1)
    async with connect(_lane_url(join_server, page="page-b")) as second:
        await _until(lambda: len(join_server.client_calls) == 2)
        # The first page comes back by itself; its old socket is of no interest here.
        async with connect(_lane_url(join_server, page="page-a", reconnect=1)) as again:
            assert await _replaced_answer(again) == ({"type": "replaced"}, plugin.server.REPLACED_CLOSE_CODE)
        await _nothing_arrives(second)
        assert second.close_code is None
        assert len(join_server.client_calls) == 2  # the handler did not run for the reconnect
    await first.close()


async def test_a_marked_connect_is_turned_away_when_the_page_was_gone_as_the_lane_was_taken(join_server, plugin):
    async with connect(_lane_url(join_server, page="page-a")):
        await _until(lambda: len(join_server.client_calls) == 1)
    await _until(lambda: ("client", "tablet") not in join_server._pages)  # the server saw the close
    async with connect(_lane_url(join_server, page="page-b")) as second:
        await _until(lambda: len(join_server.client_calls) == 2)
        async with connect(_lane_url(join_server, page="page-a", reconnect=1)) as again:
            assert await _replaced_answer(again) == ({"type": "replaced"}, plugin.server.REPLACED_CLOSE_CODE)
        await _nothing_arrives(second)
        assert second.close_code is None


async def test_the_page_that_holds_the_lane_keeps_it_over_its_own_reconnect(join_server):
    async with connect(_lane_url(join_server, page="page-a")):
        await _until(lambda: len(join_server.client_calls) == 1)
        async with connect(_lane_url(join_server, page="page-b")):
            await _until(lambda: len(join_server.client_calls) == 2)
        await _until(lambda: ("client", "tablet") not in join_server._pages)  # its socket was lost
        async with connect(_lane_url(join_server, page="page-b", reconnect=1)) as again:
            await _until(lambda: len(join_server.client_calls) == 3)
            await _nothing_arrives(again)
            assert again.close_code is None


async def test_a_connect_without_the_mark_takes_the_lane_back_and_the_page_is_no_longer_held_as_replaced(
        join_server, plugin):
    first = await connect(_lane_url(join_server, page="page-a"))
    await _until(lambda: len(join_server.client_calls) == 1)
    async with connect(_lane_url(join_server, page="page-b")) as second:
        await _until(lambda: len(join_server.client_calls) == 2)
        async with connect(_lane_url(join_server, page="page-a")):  # the user starts it again
            await _until(lambda: len(join_server.client_calls) == 3)
            assert await _replaced_answer(second) == ({"type": "replaced"}, plugin.server.REPLACED_CLOSE_CODE)
        await _until(lambda: ("client", "tablet") not in join_server._pages)
        async with connect(_lane_url(join_server, page="page-a", reconnect=1)) as again:
            await _until(lambda: len(join_server.client_calls) == 4)
            await _nothing_arrives(again)
            assert again.close_code is None
    await first.close()


async def test_a_marked_connect_of_a_page_the_server_does_not_know_takes_the_lane(join_server):
    """After a restart of the gateway the server knows no page: the page that comes back
    by itself goes on."""
    async with connect(_lane_url(join_server, page="page-a", reconnect=1)):
        await _until(lambda: len(join_server.client_calls) == 1)


async def test_a_mark_without_a_page_id_is_a_plain_connect(join_server, plugin):
    async with connect(_lane_url(join_server, page="page-a")) as first:
        await _until(lambda: len(join_server.client_calls) == 1)
        async with connect(_lane_url(join_server, reconnect=1)):
            await _until(lambda: len(join_server.client_calls) == 2)
            assert await _replaced_answer(first) == ({"type": "replaced"}, plugin.server.REPLACED_CLOSE_CODE)


@pytest.mark.parametrize("page_id", ["", "page a", "page/a", "p" * 257])
async def test_a_page_id_that_is_no_id_is_refused(join_server, page_id):
    async with connect(_lane_url(join_server, page=page_id)) as ws:
        frame = json.loads(await asyncio.wait_for(ws.recv(), 2))
        with pytest.raises(ConnectionClosed) as info:
            await asyncio.wait_for(ws.recv(), 2)
    assert frame["type"] == "error" and "page id" in frame["message"]
    assert info.value.rcvd.code == 1008
    assert join_server.client_calls == []


async def test_the_server_keeps_no_more_replaced_page_ids_than_its_limit(join_server, plugin, monkeypatch):
    monkeypatch.setattr(plugin.server, "MAX_REPLACED_PAGES", 3)
    sockets = []
    for number in range(6):
        sockets.append(await connect(_lane_url(join_server, page=f"page-{number}")))
        await _until(lambda: len(join_server.client_calls) == number + 1)
    assert len(join_server._replaced_pages) == 3
    # The newest of the replaced pages is still held, the oldest was dropped.
    assert "page-4" in join_server._replaced_pages and "page-0" not in join_server._replaced_pages
    for ws in sockets:
        await ws.close()
