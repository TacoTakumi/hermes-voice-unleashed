"""Session rows and history lines read from a real Hermes state DB.

The session list the page shows comes from the profile's ``SessionDB`` and
the gateway's session store; the history frame comes from a session's
stored messages. Both are read here against a real DB in a scratch
HERMES_HOME, not a fake.
"""

from __future__ import annotations

import time

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB

from voicemanager_live import sessions

PLATFORM = "voicemanager_live"
OTHER = "telegram"
BASE = 1_700_000_000.0  # a fixed epoch so recency is under the test's control


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A session store and a SessionDB on the same scratch HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    db = SessionDB()
    try:
        yield store, db
    finally:
        db.close()
        store.close_all_db_handles()


def _lane(store, db, chat_id="voice"):
    """Put the page's lane on a session, as the gateway does for its first message."""
    source = SessionSource(platform=Platform(PLATFORM), chat_id=chat_id, chat_type="dm",
                           user_id="owner", user_name="Owner")
    entry = store.get_or_create_session(source)
    db.ensure_session(entry.session_id, source=PLATFORM)
    return entry.session_key, entry.session_id


def _session(db, session_id, source, at, text="hello there"):
    """A session whose recency is ``at``: one user message stamped with that time."""
    db.create_session(session_id, source=source)
    db.append_message(session_id, "user", text, timestamp=at)
    return session_id


def _populate(store, db):
    """25 sessions over two sources, one of them a compaction ancestor. Returns the lane
    key, the lane's session id, the ancestor's id, its tip's id and the ids by recency
    (most recent first) as the list should show them."""
    lane_key, lane_id = _lane(store, db)
    db.append_message(lane_id, "user", "lane question", timestamp=BASE + 24)
    by_recency = [(BASE + 24, lane_id)]
    for i in range(22):  # 22 plain sessions, the lane's, the ancestor and its tip make 25
        source = PLATFORM if i % 2 else OTHER
        sid = _session(db, f"s{i:02d}", source, BASE + i, text=f"question {i}")
        by_recency.append((BASE + i, sid))
    # The ancestor ended by compression; its continuation is the row the list should show.
    ancestor = _session(db, "old-root", OTHER, BASE + 23.0, text="before compaction")
    db.end_session(ancestor, "compression")
    tip = "old-tip"
    db.create_session(tip, source=OTHER, parent_session_id=ancestor)
    db.append_message(tip, "user", "after compaction", timestamp=BASE + 23.5)
    db.set_session_title(tip, "Old conversation")
    by_recency.append((BASE + 23.5, tip))
    by_recency.sort(reverse=True)
    return lane_key, lane_id, ancestor, tip, [sid for _, sid in by_recency]


def test_session_rows_lists_the_twenty_most_recent_with_the_lane_current(state):
    store, db = state
    lane_key, lane_id, ancestor, tip, expected = _populate(store, db)
    assert db.get_session(ancestor)["end_reason"] == "compression"  # the fixture holds

    rows = sessions.session_rows(store, db, lane_key)

    assert len(rows) == 20
    assert [r["id"] for r in rows] == expected[:20]
    ids = {r["id"] for r in rows}
    assert ancestor not in ids
    assert tip in ids
    assert {r["source"] for r in rows} == {PLATFORM, OTHER}
    for row in rows:
        assert set(row) == {"id", "title", "preview", "last_active", "message_count", "source", "current"}
        assert isinstance(row["last_active"], float)
        assert row["message_count"] >= 1
        assert row["preview"]
    assert [r["id"] for r in rows if r["current"]] == [lane_id]
    tip_row = next(r for r in rows if r["id"] == tip)
    assert tip_row["title"] == "Old conversation"
    assert tip_row["preview"] == "after compaction"
    assert tip_row["last_active"] == pytest.approx(BASE + 23.5)
    lane_row = rows[0]
    assert lane_row["title"] is None
    assert lane_row["preview"] == "lane question"


def test_session_rows_marks_nothing_current_for_an_unknown_lane(state):
    store, db = state
    _populate(store, db)
    rows = sessions.session_rows(store, db, "voicemanager_live:nobody")
    assert len(rows) == 20
    assert not any(r["current"] for r in rows)


def _transcript(db, session_id):
    """50 rows: 44 spoken turns, 3 tool rows, 2 empty assistant rows, 1 null-content row.
    Returns the (role, text) pairs the history should show, oldest first."""
    db.create_session(session_id, source=PLATFORM)
    expected = []
    turn = 0
    skipped_at = {5: "tool", 17: "empty", 23: "null", 30: "tool", 38: "empty", 45: "tool"}
    for n in range(50):
        kind = skipped_at.get(n)
        if kind == "tool":
            db.append_message(session_id, "tool", f"tool output {n}", tool_name="terminal",
                              tool_call_id=f"call{n}")
        elif kind == "empty":
            db.append_message(session_id, "assistant", "")
        elif kind == "null":
            db.append_message(session_id, "assistant", None,
                              tool_calls=[{"id": f"call{n}", "type": "function",
                                           "function": {"name": "terminal", "arguments": "{}"}}])
        else:
            role = "user" if turn % 2 == 0 else "assistant"
            text = f"{role} line {turn}"
            db.append_message(session_id, role, text)
            expected.append(("user" if role == "user" else "hermes", text))
            turn += 1
    assert len(expected) == 44
    return expected


def test_history_lines_gives_the_last_forty_spoken_turns_oldest_first(state):
    _, db = state
    expected = _transcript(db, "talk")

    lines = sessions.history_lines(db, "talk")

    assert len(lines) == 40
    assert [(l["role"], l["text"]) for l in lines] == expected[-40:]
    for line in lines:
        assert set(line) == {"role", "text"}
        assert line["role"] in ("user", "hermes")
        assert line["text"].strip()
        assert "tool output" not in line["text"]


def test_history_lines_honours_the_limit_and_a_short_session(state):
    _, db = state
    expected = _transcript(db, "talk")
    assert [(l["role"], l["text"]) for l in sessions.history_lines(db, "talk", limit=5)] == expected[-5:]
    assert [(l["role"], l["text"]) for l in sessions.history_lines(db, "talk", limit=100)] == expected


def test_history_lines_of_a_new_session_is_empty(state):
    _, db = state
    db.create_session("fresh", source=PLATFORM)
    assert sessions.history_lines(db, "fresh") == []
    assert sessions.history_lines(db, "never-created") == []


def test_history_lines_reads_text_parts_and_skips_what_has_none(state):
    _, db = state
    db.create_session("parts", source=PLATFORM)
    db.append_message("parts", "user", [{"type": "text", "text": "look at this"},
                                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}])
    db.append_message("parts", "assistant", [{"type": "image_url", "image_url": {"url": "data:x"}}])
    db.append_message("parts", "assistant", "a picture")
    db.append_message("parts", "system", "not shown")
    assert sessions.history_lines(db, "parts") == [
        {"role": "user", "text": "look at this"},
        {"role": "hermes", "text": "a picture"},
    ]


def test_history_lines_strip_say_tags_from_a_reply(state):
    """A Desktop voice chat's replies start with a spoken summary in a say block."""
    _, db = state
    db.create_session("said", source=PLATFORM)
    db.append_message("said", "user", "what is the plan")
    db.append_message("said", "assistant", "<say>Two steps.</say> First build, then test.")
    db.append_message("said", "assistant", [{"type": "text", "text": "<say>Done.</say>"}])
    assert sessions.history_lines(db, "said") == [
        {"role": "user", "text": "what is the plan"},
        {"role": "hermes", "text": "Two steps. First build, then test."},
        {"role": "hermes", "text": "Done."},
    ]
