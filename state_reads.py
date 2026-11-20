"""The profile's stored sessions, read the same way on every Hermes core.

A core up to 0.23 keeps sessions in a ``SessionDB``. A later core keeps them
behind a storage interface and hands out a store handle with repositories
(``sessions``, ``messages``) in its place. The readers in this plugin want four
reads: one session row, a page of a transcript, a session's message count and
the session list. ``reader_for`` gives an object with those four reads over
whichever of the two the gateway holds, and ``open_profile_reader`` opens the
profile's store read-only when the gateway holds neither.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# What sqlite says when a read-only open finds no database file.
_MISSING_DB_FILE = "unable to open database file"


class EmptyState:
    """A profile with nothing stored: what the readers get when the profile has no
    store yet. It answers the reads the session list, the history frame, the message
    count and the lineage walk make."""

    def get_session(self, session_id: str) -> None:
        return None

    def get_messages(self, session_id: str, *args: Any, **kwargs: Any) -> list:
        return []

    def message_count(self, session_id: Optional[str] = None) -> int:
        return 0

    def list_sessions_rich(self, *args: Any, **kwargs: Any) -> list:
        return []


class StoreReader:
    """The four reads over a storage interface handle, in the old dict shapes."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        record = self._store.sessions.get(session_id)
        return None if record is None else record.as_session_row()

    def get_messages(self, session_id: str, limit: Optional[int] = None, offset: int = 0,
                     latest: bool = False) -> list[dict[str, Any]]:
        from persistence.conversation.messages import TranscriptRead

        request = TranscriptRead(limit=limit, offset=offset, latest=latest)
        return [row.as_wire_row() for row in self._store.messages.read_transcript(session_id, request)]

    def message_count(self, session_id: str) -> int:
        """The count the session's own row carries; a session without a row has none."""
        row = self.get_session(session_id)
        return int((row or {}).get("message_count") or 0)

    def list_sessions_rich(self, limit: int = 20, order_by_last_active: bool = False) -> list[dict[str, Any]]:
        from persistence.conversation.sessions import SessionOrder, SessionQuery, SessionWindow

        order = SessionOrder.LAST_ACTIVE if order_by_last_active else SessionOrder.STARTED_AT
        entries = self._store.sessions.list_sessions(SessionQuery(order=order), SessionWindow(limit=limit))
        return [entry.as_list_row() for entry in entries]


def _is_store_handle(db: Any) -> bool:
    """Whether ``db`` is a storage interface handle: it has the two repositories the
    reads use. A ``SessionDB`` has neither."""
    try:
        return getattr(db, "sessions", None) is not None and getattr(db, "messages", None) is not None
    except Exception:
        return False


def reader_for(db: Any) -> Any:
    """``db`` as an object with the four reads: a store handle is wrapped, a
    ``SessionDB`` (behind its async door or not) is used as it is."""
    if _is_store_handle(db):
        return StoreReader(db)
    return getattr(db, "_db", db)


@contextlib.contextmanager
def open_profile_reader() -> Iterator[Any]:
    """The profile's store opened read-only for the block. A profile that has not
    stored a session yet has no store, which is normal on first use: a read-only
    open cannot create it, so the readers see an empty one instead."""
    try:
        from persistence_assembly import open_persistence_reader, profile_scope, store_exists
    except ImportError:
        with _open_session_db() as db:
            yield db
        return
    scope = profile_scope()
    if not store_exists(scope):
        logger.debug("voice page: no session store yet, reading it as empty")
        yield EmptyState()
        return
    with open_persistence_reader(scope.profile_dir) as store:
        yield StoreReader(store)


@contextlib.contextmanager
def _open_session_db() -> Iterator[Any]:
    """The read-only open on a core that still has ``SessionDB``."""
    from hermes_state import SessionDB

    try:
        db = SessionDB(read_only=True)
    except sqlite3.OperationalError as exc:
        if _MISSING_DB_FILE not in str(exc):
            raise
        logger.debug("voice page: no state DB yet, reading it as empty")
        yield EmptyState()
        return
    try:
        yield db
    finally:
        db.close()
