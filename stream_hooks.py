"""Plugin hooks that carry a reply's text to the page's VoiceManager session.

Registering a stream observer makes the agent stream on every surface, so
replies reach TTS as they are written with no gateway streaming setting.
The hooks run off the event loop (each stream hook on its own host worker
thread, ``on_session_end`` on the agent's thread), so each one only reads its
routing and hands the event to the adapter's loop. Nothing orders the two:
the session closes a reply only after its text has gone quiet.

Routing: a hook keeps an event only when the session's platform is this
plugin's, and finds the page by the session's chat id, read through
``get_session_env`` (the hooks run inside the turn's session context).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from gateway.session_context import get_session_env

logger = logging.getLogger(__name__)

PLATFORM = "voicemanager_live"

_adapters: list[Any] = []  # the running adapters, newest last


def attach(adapter) -> None:
    if adapter not in _adapters:
        _adapters.append(adapter)


def detach(adapter) -> None:
    if adapter in _adapters:
        _adapters.remove(adapter)


def _route(surface: Optional[str]) -> Optional[tuple[Any, Any]]:
    """The (adapter, session) a hook event belongs to, or None when it is not ours."""
    platform = get_session_env("HERMES_SESSION_PLATFORM", "") or surface or ""
    if platform != PLATFORM or (surface and surface != PLATFORM):
        return None
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
    for adapter in reversed(_adapters):
        session = adapter.session_for(chat_id)
        if session is not None:
            return adapter, session
    return None


def _post(surface: Optional[str], method: str, *args: Any) -> None:
    routed = _route(surface)
    if routed is None:
        return
    adapter, session = routed
    loop = adapter.loop
    if loop is None or loop.is_closed():
        return
    # The message id of the utterance this turn answers: the session drops text of an
    # older reply it cancelled.
    reply_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "")
    asyncio.run_coroutine_threadsafe(getattr(session, method)(reply_id, *args), loop)


def on_stream_delta(delta: str = "", kind: str = "text", turn_id: str = "", iteration: int = 0,
                    surface: str = "", **_: Any) -> None:
    if kind == "text" and delta:
        _post(surface, "on_reply_delta", turn_id, iteration, delta)


def on_session_end(turn_id: str = "", interrupted: bool = False, platform: str = "", **_: Any) -> None:
    _post(platform, "on_turn_done", turn_id, bool(interrupted))


HOOKS = {
    "on_stream_delta": on_stream_delta,
    "on_session_end": on_session_end,
}


def register(ctx) -> None:
    for name, callback in HOOKS.items():
        ctx.register_hook(name, callback)
