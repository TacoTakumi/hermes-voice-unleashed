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

``GET /sessions?client=<page client>`` answers the session list for that
client's conversation as JSON, from ``on_sessions``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
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
DEFAULT_CLIENT = "phone"
MAX_CLIENT_ID = 64  # a longer page client id is cut to this


def _query(path: str) -> tuple[str, dict[str, list[str]]]:
    parts = urlsplit(path)
    return parts.path, parse_qs(parts.query, keep_blank_values=True)


def _client_id(query: dict[str, list[str]]) -> str:
    """The page client the query names, the default one when it names none."""
    return (query.get("client") or [DEFAULT_CLIENT])[0][:MAX_CLIENT_ID] or DEFAULT_CLIENT


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
            if not session_id or len(session_id) > MAX_ATTACH_ID:
                reason = ("empty attach id" if not session_id
                          else f"attach id longer than {MAX_ATTACH_ID} characters")
                with contextlib.suppress(Exception):
                    await connection.send(json.dumps({"type": "error", "message": reason}))
                await connection.close(code=1008, reason=reason)  # policy violation
                return
            await self._own(connection, ("attach", session_id), self._on_attach)
            return
        client_id = _client_id(query)
        session = (query.get("session") or [""])[0]
        options = {"session": session} if session else {}
        await self._own(connection, ("client", client_id), self._on_client, **options)

    async def _own(self, connection: ServerConnection, key: tuple[str, str],
                   handler: ClientHandler, **options) -> None:
        """Run ``handler`` for the connection that now owns ``key``."""
        previous = self._pages.get(key)
        if previous is not None:
            # One page owns a conversation's mic and speaker. The new page takes it over;
            # the old one is told so, and REPLACED_CLOSE_CODE keeps it from reconnecting
            # (two pages that both reconnected would push each other off for ever).
            with contextlib.suppress(Exception):
                await previous.send(json.dumps({"type": "replaced"}))
            with contextlib.suppress(Exception):
                await previous.close(code=REPLACED_CLOSE_CODE, reason="replaced")
        self._pages[key] = connection
        try:
            await handler(connection, key[1], **options)
        finally:
            if self._pages.get(key) is connection:
                del self._pages[key]
