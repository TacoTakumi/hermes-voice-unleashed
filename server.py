"""HTTP and WebSocket server for the voice page.

One websockets server serves both: ``GET /`` returns the page and
``/ws`` upgrades to the audio WebSocket. Every request must carry the
access token as the ``token`` query parameter; a request without the
right token gets 401 before any handshake, so it never reaches a
VoiceManager session.
"""

from __future__ import annotations

import hmac
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"

ClientHandler = Callable[[ServerConnection, str], Awaitable[None]]


def _query(path: str) -> tuple[str, dict[str, list[str]]]:
    parts = urlsplit(path)
    return parts.path, parse_qs(parts.query)


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
                 web_dir: Path = WEB_DIR) -> None:
        if not token:
            raise ValueError("an access token is required")
        self.host, self.port = host, port
        self._token = token.encode()
        self._on_client = on_client
        self._web_dir = web_dir
        self._server: Optional[Server] = None

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

    def _process_request(self, connection: ServerConnection, request: Request) -> Optional[Response]:
        path, query = _query(request.path)
        token = (query.get("token") or [""])[0]
        if not self.token_ok(token):
            return _response(HTTPStatus.UNAUTHORIZED, b"unauthorized\n", "text/plain; charset=utf-8")
        if path == "/ws":
            return None  # continue with the WebSocket handshake
        if path in ("/", "/index.html"):
            return _response(HTTPStatus.OK, self._page(), "text/html; charset=utf-8")
        return _response(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    async def _handle(self, connection: ServerConnection) -> None:
        _, query = _query(connection.request.path)
        client_id = (query.get("client") or ["phone"])[0][:64] or "phone"
        await self._on_client(connection, client_id)
