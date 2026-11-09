"""The state DB fallback when the profile has no state DB yet.

Without a runner the adapter opens the profile's state DB read-only for each
read. A profile that has not stored a session yet has no state.db file, which
is normal on first use: the readers then see an empty DB (no sessions, no
history, zero messages) and nothing is logged at warning or above. The test
runner points HERMES_HOME at an empty scratch directory, so the real open
fails here the way it does on a fresh profile.

The message count read before /new runs off the event loop, like the other
reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import httpx
import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"


def _config(**extra):
    base = {"token": TOKEN, "voicemanager_url": "http://127.0.0.1:1", "host": "127.0.0.1", "port": 0}
    base.update(extra)
    return PlatformConfig(enabled=True, extra=base)


class FakeStore:
    """The gateway's session store, as far as the connect needs it: the session each
    lane key points at."""

    def __init__(self, session_id):
        self.session_id = session_id

    def peek_session_id(self, session_key):
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
    """An adapter without a gateway runner, so every read goes through the fallback.
    Its gateway is a recording handle_message that answers a command at once."""
    adapter = plugin.adapter.VoiceLiveAdapter(_config(voicemanager_url=fake_vm.url))
    messages = []

    async def handle_message(event):
        messages.append(event)
        if event.text.startswith("/") and adapter.gateway_reply is not None:
            await adapter.send(event.source.chat_id, adapter.gateway_reply)

    adapter.handle_message = handle_message
    adapter.messages = messages
    adapter.gateway_reply = "New session started."
    assert await adapter.connect()
    yield adapter
    await adapter.disconnect()


def _url(adapter, query):
    return f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&{query}"


def _loud(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_state_db_file_is_missing_here(plugin):
    """The rig's premise: the scratch HERMES_HOME holds no state.db, so the real
    read-only open fails."""
    with pytest.raises(Exception):
        plugin.adapter.SessionDB(read_only=True)


async def test_a_connect_without_a_state_db_gets_an_empty_history_and_no_warning(adapter, fake_vm, caplog):
    adapter._session_store = FakeStore("abc")
    caplog.set_level(logging.DEBUG)
    async with connect(_url(adapter, "client=tablet")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert page.of("history") == [{"type": "history", "session_id": "abc", "lines": []}]
    assert _loud(caplog) == []


async def test_the_session_list_without_a_state_db_is_empty_and_ok(adapter, fake_vm, caplog):
    adapter._session_store = FakeStore("abc")
    caplog.set_level(logging.DEBUG)
    url = f"http://127.0.0.1:{adapter.bound_port}/sessions"
    async with httpx.AsyncClient() as http:
        answer = await http.get(url, params={"token": TOKEN, "client": "phone"})
    assert answer.status_code == 200
    assert answer.json() == []
    assert _loud(caplog) == []


async def test_a_session_new_connect_without_a_state_db_counts_zero_and_does_not_warn(adapter, fake_vm, caplog):
    # A lane whose session has no stored message is as good as new: no /new is sent.
    adapter._session_store = FakeStore("abc")
    caplog.set_level(logging.DEBUG)
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert adapter.messages == []
    assert page.of("history") == [{"type": "history", "session_id": "abc", "lines": []}]
    assert _loud(caplog) == []


class ThreadNotingDB:
    """A state DB that notes the thread each message count ran on."""

    def __init__(self, count):
        self.count = count
        self.threads = []

    def message_count(self, session_id):
        self.threads.append(threading.current_thread())
        return self.count

    def get_messages(self, session_id, limit=None, offset=0, latest=True):
        return []


async def test_the_message_count_before_new_runs_off_the_event_loop(adapter, fake_vm):
    adapter._session_store = FakeStore("abc")
    db = ThreadNotingDB(3)
    adapter.gateway_runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))
    loop_thread = threading.current_thread()
    async with connect(_url(adapter, "client=tablet&session=new")) as ws:
        page = Page(ws)
        await wait_for(lambda: page.of("ready"))
        page.task.cancel()
    assert [m.text for m in adapter.messages] == ["/new"]
    assert len(db.threads) == 1
    assert db.threads[0] is not loop_thread
