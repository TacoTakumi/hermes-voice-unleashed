"""The reads on a core that keeps sessions behind the storage interface.

On such a core the gateway runner holds a store handle with repositories, not
a ``SessionDB``. The session list, the history, the message count and the
lineage walk read a real store through such a handle here, and give what the
same reads give through the core's ``SessionDB``. A profile whose store exists
is also read through the read-only open the adapter makes without a runner.

The whole file is skipped on a core without the storage interface.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("persistence_assembly")

from gateway.config import PlatformConfig  # noqa: E402
from hermes_state import SessionDB  # noqa: E402
from persistence_assembly import open_persistence, profile_scope  # noqa: E402

from voice_unleashed import sessions, state_reads  # noqa: E402

PLATFORM = "voice_unleashed"
BASE = 1_700_000_000.0


class FakeStore:
    """The gateway's session store: the session the lane is on."""

    def __init__(self, session_id):
        self.session_id = session_id

    def peek_session_id(self, session_key):
        return self.session_id


@pytest.fixture
def stored(tmp_path, monkeypatch):
    """A store on a scratch HERMES_HOME with a compacted conversation, a plain session
    and an empty one. Gives the ``SessionDB`` that wrote it and a store handle on it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB()
    db.create_session("root", source="telegram")
    db.append_message("root", "user", "before compaction", timestamp=BASE)
    db.end_session("root", "compression")
    db.create_session("tip", source="telegram", parent_session_id="root")
    db.append_message("tip", "user", "after compaction", timestamp=BASE + 1)
    db.append_message("tip", "assistant", "an answer", timestamp=BASE + 2)
    db.set_session_title("tip", "Old conversation")
    db.create_session("plain", source=PLATFORM)
    db.append_message("plain", "user", "a question", timestamp=BASE + 3)
    db.create_session("empty", source=PLATFORM)
    handle = open_persistence(profile_scope())
    try:
        yield db, handle
    finally:
        handle.close()
        db.close()


def test_a_store_handle_is_wrapped_and_a_session_db_is_not(stored):
    db, handle = stored
    assert isinstance(state_reads.reader_for(handle), state_reads.StoreReader)
    assert state_reads.reader_for(db) is db
    assert state_reads.reader_for(SimpleNamespace(_db=db)) is db


def test_the_session_list_through_a_store_handle_matches_the_session_db(stored):
    db, handle = stored
    store = FakeStore("root")  # the lane points at the compacted ancestor
    rows = sessions.session_rows(store, state_reads.reader_for(handle), "lane")
    assert rows == sessions.session_rows(store, db, "lane")
    # The empty session has no message, so its recency is its start: now.
    assert [row["id"] for row in rows] == ["empty", "plain", "tip"]
    tip = rows[2]
    assert tip["title"] == "Old conversation" and tip["current"] is True
    assert tip["message_count"] >= 2 and tip["source"] == "telegram"


def test_the_history_through_a_store_handle_matches_the_session_db(stored):
    db, handle = stored
    lines = sessions.history_lines(state_reads.reader_for(handle), "tip")
    assert lines == sessions.history_lines(db, "tip")
    assert lines[-2:] == [{"role": "user", "text": "after compaction"}, {"role": "hermes", "text": "an answer"}]


def test_the_message_count_through_a_store_handle(stored):
    _, handle = stored
    reader = state_reads.reader_for(handle)
    assert reader.message_count("tip") == 2
    assert reader.message_count("empty") == 0
    assert reader.message_count("no-such-session") == 0


def test_the_lineage_walk_through_a_store_handle(plugin, stored):
    _, handle = stored
    reader = state_reads.reader_for(handle)
    assert plugin.adapter._same_conversation(reader, "tip", "root") is True
    assert plugin.adapter._same_conversation(reader, "plain", "root") is False


def _adapter(plugin):
    extra = {"token": "tok", "voicemanager_url": "http://127.0.0.1:1", "host": "127.0.0.1", "port": 0}
    return plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra=extra))


def test_the_adapter_reads_the_runner_s_store_handle(plugin, stored):
    """The runner of such a core holds the handle itself under ``_session_db``."""
    _, handle = stored
    adapter = _adapter(plugin)
    adapter.gateway_runner = SimpleNamespace(_session_db=handle, session_store=FakeStore("plain"))
    rows = adapter._session_rows("voice")
    assert [row["id"] for row in rows] == ["empty", "plain", "tip"]
    assert rows[1]["current"] is True and rows[1]["voice_attached"] is False
    assert adapter._history_lines("plain") == [{"role": "user", "text": "a question"}]
    with adapter._session_db() as reader:
        assert reader.message_count("plain") == 1


def test_the_adapter_without_a_runner_reads_the_profile_s_store(plugin, stored):
    adapter = _adapter(plugin)
    assert adapter._history_lines("plain") == [{"role": "user", "text": "a question"}]
    with adapter._session_db() as reader:
        assert isinstance(reader, state_reads.StoreReader)
        assert reader.message_count("tip") == 2
