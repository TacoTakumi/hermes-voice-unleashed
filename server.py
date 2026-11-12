"""HTTP and WebSocket server for the voice page.

One websockets server serves both: ``GET /`` returns the page and
``/ws`` upgrades to the audio WebSocket. Every request must carry the
access token as the ``token`` query parameter; a request without the
right token gets 401 before any handshake, so it never reaches a
VoiceManager session.

``/ws?attach=<Hermes session id>`` attaches a UI client to a Hermes session
instead of a page conversation; it is handed to ``on_attach``.
``/ws?client=<id>&session=<Hermes session id>`` asks for the page's
conversation to join that session; the id goes to ``on_client`` as ``session``.
Both ids must be 1 to MAX_ATTACH_ID characters from letters, digits, underscore
and dash (``20261107_101500_ab12cd``): the session id becomes the text of a
``/resume`` on the gateway, so any other shape is refused with an error frame
and close code 1008 before the handler runs.

``/ws?page=<page id>`` names the page itself: one id for the life of a page load, of
the same shape as a session id. ``&reconnect=1`` marks a connect that the page made
by itself after a lost socket. A page that another device replaced while it was off
the network never got its replaced frame; its marked connect gets that frame and
close code 4001, and takes nothing. A connect without the mark is a start by the
user and takes the conversation as before. A page that sends no page id is never
turned away.

``GET /sessions?client=<page client>`` answers the session list for that
client's conversation as JSON, from ``on_sessions``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"

ClientHandler = Callable[..., Awaitable[None]]  # (connection, id, **options)
SessionsHandler = Callable[[str], list[dict[str, Any]]]  # page client id -> its session rows
REPLACED_CLOSE_CODE = 4001  # another page took this conversation over
JOIN_FAILED_CLOSE_CODE = 4002  # the session the page asked to join could not be joined
MAX_ATTACH_ID = 256  # a longer Hermes session id is refused, never cut
SESSION_ID_CHARS = re.compile(r"[A-Za-z0-9_-]+\Z")  # the alphabet of a Hermes session id
DEFAULT_CLIENT = "voice"
MAX_CLIENT_ID = 64  # a longer page client id is cut to this
MAX_REPLACED_PAGES = 64  # the ids of replaced pages that are kept; the oldest goes first


def _query(path: str) -> tuple[str, dict[str, list[str]]]:
    parts = urlsplit(path)
    return parts.path, parse_qs(parts.query, keep_blank_values=True)


def _client_id(query: dict[str, list[str]]) -> str:
    """The page client the query names, the default one when it names none."""
    return (query.get("client") or [DEFAULT_CLIENT])[0][:MAX_CLIENT_ID] or DEFAULT_CLIENT


def _bad_session_id(kind: str, session_id: str) -> Optional[str]:
    """Why ``session_id`` is not a Hermes session id, None when it is one.

    ``kind`` names the query key in the reason ("attach" or "session")."""
    if not session_id:
        return f"empty {kind} id"
    if len(session_id) > MAX_ATTACH_ID:
        return f"{kind} id longer than {MAX_ATTACH_ID} characters"
    if not SESSION_ID_CHARS.match(session_id):
        return f"{kind} id has a character outside letters, digits, underscore and dash"
    return None


async def _refuse(connection: ServerConnection, reason: str) -> None:
    """Tell the client why and close with 1008 (policy violation)."""
    with contextlib.suppress(Exception):
        await connection.send(json.dumps({"type": "error", "message": reason}))
    await connection.close(code=1008, reason=reason)


def _response(status: HTTPStatus, body: bytes, content_type: str) -> Response:
    headers = Headers([
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("Referrer-Policy", "no-referrer"),
    ])
    return Response(status.value, status.phrase, headers, body)


class VoiceServer:
    """Serves the page and accepts audio WebSockets for a token holder."""

    def __init__(self, host: str, port: int, token: str, on_client: ClientHandler,
                 web_dir: Path = WEB_DIR, on_attach: Optional[ClientHandler] = None,
                 on_sessions: Optional[SessionsHandler] = None) -> None:
        if not token:
            raise ValueError("an access token is required")
        self.host, self.port = host, port
        self._token = token.encode()
        self._on_client = on_client
        self._on_attach = on_attach
        self._on_sessions = on_sessions
        self._web_dir = web_dir
        self._server: Optional[Server] = None
        # (kind, id) -> the connection that owns it: ("client", page client id) or
        # ("attach", Hermes session id), so a page and an attached client never collide.
        self._pages: dict[tuple[str, str], ServerConnection] = {}
        self._closing: set[asyncio.Task] = set()  # the closes of replaced pages, kept from collection
        # ("client", page client id) -> the id of the page that last took that conversation.
        # It stays after the page's socket closed: a page that is off the network when
        # another device takes its place is replaced all the same.
        self._page_ids: dict[tuple[str, str], str] = {}
        # The ids of the pages that another device replaced, oldest first. A page leaves
        # it when the user starts that page again.
        self._replaced_pages: dict[str, None] = {}

    @property
    def bound_port(self) -> int:
        """The port actually bound (differs from ``port`` when it was 0)."""
        if self._server is None:
            return self.port
        return self._server.sockets[0].getsockname()[1]

    def token_ok(self, token: str) -> bool:
        return hmac.compare_digest(token.encode(), self._token)

    async def start(self) -> None:
        self._server = await serve(self._handle, self.host, self.port,
                                   process_request=self._process_request, max_size=2**20)
        logger.info("voice page listening on %s:%s", self.host, self.bound_port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _page(self) -> bytes:
        return (self._web_dir / "index.html").read_bytes()

    def _process_request(self, connection: ServerConnection, request: Request):
        path, query = _query(request.path)
        token = (query.get("token") or [""])[0]
        if not self.token_ok(token):
            return _response(HTTPStatus.UNAUTHORIZED, b"unauthorized\n", "text/plain; charset=utf-8")
        if path == "/ws":
            return None  # continue with the WebSocket handshake
        if path in ("/", "/index.html"):
            return _response(HTTPStatus.OK, self._page(), "text/html; charset=utf-8")
        if path == "/sessions" and self._on_sessions is not None:
            return self._sessions_response(_client_id(query))  # awaited by websockets
        return _response(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    async def _sessions_response(self, client_id: str) -> Response:
        """The session list for the page client's conversation, as a JSON array.

        The callback reads Hermes's state DB, so it runs off the event loop."""
        try:
            rows = await asyncio.to_thread(self._on_sessions, client_id)
        except Exception:
            logger.exception("voice page: the session list for %s failed", client_id)
            return _response(HTTPStatus.INTERNAL_SERVER_ERROR, b"session list unavailable\n",
                             "text/plain; charset=utf-8")
        return _response(HTTPStatus.OK, json.dumps(rows).encode(), "application/json")

    async def _handle(self, connection: ServerConnection) -> None:
        _, query = _query(connection.request.path)
        if "attach" in query and self._on_attach is not None:
            session_id = query["attach"][0]
            reason = _bad_session_id("attach", session_id)
            if reason is not None:
                await _refuse(connection, reason)
                return
            await self._own(connection, ("attach", session_id), self._on_attach)
            return
        client_id = _client_id(query)
        page_id = None
        if "page" in query:
            page_id = query["page"][0]
            reason = _bad_session_id("page", page_id)
            if reason is not None:
                await _refuse(connection, reason)
                return
            if "reconnect" in query and page_id in self._replaced_pages:
                # The page comes back by itself and does not know that it was replaced.
                await self._close_replaced(connection)
                return
        options = {}
        if "session" in query:
            session = query["session"][0]
            reason = _bad_session_id("session", session)
            if reason is not None:
                await _refuse(connection, reason)
                return
            options["session"] = session
        self._page_takes(("client", client_id), page_id)
        await self._own(connection, ("client", client_id), self._on_client, **options)

    def _page_takes(self, key: tuple[str, str], page_id: Optional[str]) -> None:
        """The page ``page_id`` (None: a page that names no id) takes the conversation
        ``key``. The page that had it before is held as replaced, also when its socket
        is gone, and the page that takes it is not held any more."""
        last = self._page_ids.pop(key, None)
        if last is not None and last != page_id:
            self._hold_replaced(last)
        if page_id is not None:
            self._page_ids[key] = page_id
            self._replaced_pages.pop(page_id, None)

    def _hold_replaced(self, page_id: str) -> None:
        self._replaced_pages.pop(page_id, None)
        self._replaced_pages[page_id] = None  # the newest is last
        while len(self._replaced_pages) > MAX_REPLACED_PAGES:
            del self._replaced_pages[next(iter(self._replaced_pages))]

    @staticmethod
    async def _close_replaced(connection: ServerConnection) -> None:
        with contextlib.suppress(Exception):
            await connection.send(json.dumps({"type": "replaced"}))
        with contextlib.suppress(Exception):
            await connection.close(code=REPLACED_CLOSE_CODE, reason="replaced")

    async def _own(self, connection: ServerConnection, key: tuple[str, str],
                   handler: ClientHandler, **options) -> None:
        """Run ``handler`` for the connection that now owns ``key``."""
        previous = self._pages.get(key)
        if previous is not None:
            # One page owns a conversation's mic and speaker. The new page takes it over;
            # the old one is told so, and REPLACED_CLOSE_CODE keeps it from reconnecting
            # (two pages that both reconnected would push each other off for ever).
            # The close runs on a task of its own and nothing waits for it: an old page
            # that answers no close frame (a phone that changed networks) holds its
            # close for the close timeout, and the new page must not wait that long.
            task = asyncio.create_task(self._close_replaced(previous))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        self._pages[key] = connection
        try:
            await handler(connection, key[1], **options)
        finally:
            if self._pages.get(key) is connection:
                del self._pages[key]
