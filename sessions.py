"""The profile's sessions and a session's history, read from Hermes's state.

Hermes keeps every session of a profile, whatever platform it came from, in
the profile's state DB (``SessionDB``), and the gateway's session store
knows which session each conversation (lane) is on. The page's session list
comes from the first; which row is the current one comes from the second.

``session_rows`` lists the most recently active sessions the way Hermes's own
session list does: a session that ended by compaction is shown as its live
continuation, so the ancestor itself never appears. ``history_lines`` turns
the end of a session's stored transcript into spoken lines: what the user
said and what Hermes answered, nothing else.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SESSION_ROWS = 20  # the most recently active sessions the list shows
HISTORY_LINES = 40  # the spoken lines the history frame carries
_HISTORY_PAGE = 64  # stored rows read per step while walking back through a transcript
# Stored roles that become spoken lines, and what the page calls them.
_LINE_ROLES = {"user": "user", "assistant": "hermes"}


def session_rows(store, db, lane_key: str, limit: int = SESSION_ROWS) -> list[dict[str, Any]]:
    """The profile's ``limit`` most recently active sessions, most recent first.

    Each row: id, title (None when the session has none), preview (the first
    user message, short), last_active (epoch seconds), message_count, source
    (the platform the session came from) and current (whether the lane
    ``lane_key`` is on it). A compaction ancestor is listed as its live tip.
    """
    current_id = store.peek_session_id(lane_key) if lane_key else None
    rows = []
    for row in db.list_sessions_rich(limit=limit, order_by_last_active=True):
        # A compacted conversation is one row for its whole chain; the lane may
        # point at any segment of it.
        chain = set(row.get("_lineage_ids") or ())
        chain.add(row.get("id"))
        rows.append({
            "id": row.get("id"),
            "title": row.get("title") or None,
            "preview": row.get("preview") or "",
            "last_active": row.get("last_active"),
            "message_count": int(row.get("message_count") or 0),
            "source": row.get("source") or "",
            "current": current_id is not None and current_id in chain,
        })
    return rows


def history_lines(db, session_id: str, limit: int = HISTORY_LINES) -> list[dict[str, Any]]:
    """The last ``limit`` spoken lines of the session, oldest first.

    A line is ``{"role": "user" | "hermes", "text": ...}`` from a stored user
    or assistant message with text. Tool rows, empty rows, compaction
    summaries and rows that cannot be read are left out one at a time; the
    rest still come through. A new or unknown session gives ``[]``.
    """
    if not session_id or limit <= 0:
        return []
    lines: list[dict[str, Any]] = []  # newest first while collecting
    offset = 0
    while len(lines) < limit:
        page = db.get_messages(session_id, limit=_HISTORY_PAGE, offset=offset, latest=True)
        if not page:
            break
        for message in reversed(page):  # the page is in order; walk it from the newest
            try:
                line = _line(message)
            except Exception:
                logger.debug("history: skipping an unreadable row of session %s", session_id, exc_info=True)
                continue
            if line is not None:
                lines.append(line)
                if len(lines) == limit:
                    break
        if len(page) < _HISTORY_PAGE:
            break
        offset += len(page)
    lines.reverse()
    return lines


def _line(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    role = _LINE_ROLES.get(message.get("role"))
    if role is None or message.get("_compressed_summary"):
        return None
    text = _text(message.get("content")).strip()
    return {"role": role, "text": text} if text else None


def _text(content: Any) -> str:
    """The text of a stored message: a string as is, the text parts of a
    multimodal list joined; anything else (an image alone, None) is empty."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(p for p in parts if p)
    return ""
