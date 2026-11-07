"""Gateway adapter for the voice page.

config.yaml ``gateway.platforms.voicemanager_live.extra`` keys: token,
voicemanager_url, voicemanager_token, host (127.0.0.1), port (8765).
The environment overrides them: VMLIVE_TOKEN, VMLIVE_VOICEMANAGER_URL,
VMLIVE_VOICEMANAGER_TOKEN, VMLIVE_HOST, VMLIVE_PORT.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms._shared import coerce_port, get_scoped_secret, seed_extra_from_env
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .server import VoiceServer
from .session import VoiceSession, client_from_url

logger = logging.getLogger(__name__)

PLATFORM = "voicemanager_live"
OWNER = "owner"  # the one gateway user: whoever holds the access token
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8765


def _setting(extra: dict, env: str, key: str, default: Any = "") -> Any:
    value = get_scoped_secret(env, "")
    if value:
        return value
    value = extra.get(key)
    return default if value in (None, "") else value


class VoiceLiveAdapter(BasePlatformAdapter):
    """Serves the voice page and relays each connected page to Hermes."""

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = getattr(config, "extra", {}) or {}
        self.token = str(_setting(extra, "VMLIVE_TOKEN", "token"))
        self.vm_url = str(_setting(extra, "VMLIVE_VOICEMANAGER_URL", "voicemanager_url"))
        self.vm_token = str(_setting(extra, "VMLIVE_VOICEMANAGER_TOKEN", "voicemanager_token"))
        self.host = str(_setting(extra, "VMLIVE_HOST", "host", DEFAULT_HOST))
        self.port = coerce_port(_setting(extra, "VMLIVE_PORT", "port", DEFAULT_PORT), DEFAULT_PORT)
        self._server: Optional[VoiceServer] = None
        self._clients: dict[str, Any] = {}  # chat_id -> the page's WebSocket connection

    @property
    def name(self) -> str:
        return "VoiceManager Live"

    @property
    def bound_port(self) -> int:
        return self._server.bound_port if self._server else self.port

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.token or not self.vm_url:
            self._set_fatal_error("config_missing", "VMLIVE_TOKEN and VMLIVE_VOICEMANAGER_URL must be set",
                                  retryable=False)
            return False
        try:
            self._server = VoiceServer(self.host, self.port, self.token, self._on_client)
            await self._server.start()
        except OSError as exc:
            logger.error("voice page: cannot listen on %s:%s: %s", self.host, self.port, exc)
            self._server = None
            self._set_fatal_error("listen_failed", str(exc), retryable=True)
            return False
        self._mark_connected()
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self._clients.clear()

    async def _on_client(self, connection, client_id: str) -> None:
        previous = self._clients.get(client_id)
        if previous is not None:  # a reload replaces the old page
            with contextlib.suppress(Exception):
                await previous.close()
        self._clients[client_id] = connection
        try:
            await self._run_client(connection, client_id)
        finally:
            if self._clients.get(client_id) is connection:
                del self._clients[client_id]

    def _new_session(self, connection, client_id: str) -> VoiceSession:
        return VoiceSession(connection, client_from_url(self.vm_url, self.vm_token), client_id)

    async def _run_client(self, connection, client_id: str) -> None:
        await self._new_session(connection, client_id).run()

    async def _send_json(self, chat_id: str, payload: dict) -> bool:
        connection = self._clients.get(chat_id)
        if connection is None:
            return False
        try:
            await connection.send(json.dumps(payload))
        except Exception:
            return False
        return True

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        if await self._send_json(chat_id, {"type": "reply", "text": content}):
            return SendResult(success=True, message_id=None)
        return SendResult(success=False, error="the voice page is not connected")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """The page shows its own state; no typing indicator."""

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}


def check_requirements() -> bool:
    """Passive check: websockets and httpx ship with Hermes, so only config matters."""
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(_setting(extra, "VMLIVE_TOKEN", "token") and
                _setting(extra, "VMLIVE_VOICEMANAGER_URL", "voicemanager_url"))


def _env_enablement() -> Optional[dict]:
    token = get_scoped_secret("VMLIVE_TOKEN", "").strip()
    url = get_scoped_secret("VMLIVE_VOICEMANAGER_URL", "").strip()
    if not (token and url):
        return None
    seed = seed_extra_from_env((
        ("VMLIVE_VOICEMANAGER_TOKEN", "voicemanager_token", None),
        ("VMLIVE_HOST", "host", None),
        ("VMLIVE_PORT", "port", int),
    ))
    return {"token": token, "voicemanager_url": url, **seed}


def register(ctx):
    """Plugin entry point."""
    ctx.register_platform(
        name=PLATFORM,
        label="VoiceManager Live",
        dashboard_description="Full-duplex voice from a phone browser through your VoiceManager server.",
        adapter_factory=VoiceLiveAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=validate_config,
        required_env=["VMLIVE_TOKEN", "VMLIVE_VOICEMANAGER_URL"],
        install_hint="No extra packages needed",
        env_enablement_fn=_env_enablement,
        allowed_users_env="VMLIVE_ALLOWED_USERS",
        emoji="🎙",
        platform_hint=(
            "You are talking with the user by voice. Your reply is spoken aloud as you write it. "
            "Answer in short, plain spoken sentences. Do not use markdown, tables, code blocks "
            "or lists unless the user asks for them."),
    )
