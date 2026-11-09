"""Hermes makes no TTS call of its own for a reply on this platform."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType

CHAT = "voice"


def _adapter(plugin):
    return plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
        "token": "t", "voicemanager_url": "ws://127.0.0.1:1", "host": "127.0.0.1", "port": 0}))


def _switch_on(adapter, how):
    """Put the adapter in the state the gateway gives it for ``how``."""
    if how == "voice.auto_tts":
        adapter._auto_tts_default = True
    else:
        adapter._auto_tts_enabled_chats.add(CHAT)


@pytest.mark.parametrize("how", ["voice.auto_tts", "/voice on"])
def test_auto_tts_stays_off_for_the_chat(plugin, how):
    adapter = _adapter(plugin)
    _switch_on(adapter, how)
    assert adapter._should_auto_tts_for_chat(CHAT) is False


@pytest.mark.parametrize("how", ["voice.auto_tts", "/voice on"])
def test_a_reply_to_a_voice_message_asks_for_no_synthesis(plugin, how):
    adapter = _adapter(plugin)
    _switch_on(adapter, how)
    event = SimpleNamespace(source=SimpleNamespace(chat_id=CHAT), message_type=MessageType.VOICE)
    assert adapter._wants_auto_tts(event, "session", asyncio.Event(), "Hello there.", []) is False
