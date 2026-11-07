"""The pre_llm_call hook asks for a spoken summary on turns of a session with voice attached."""

from __future__ import annotations

import pytest

from gateway.config import PlatformConfig

SESSION_ID = "20261107_101500_ab12cd"


@pytest.fixture
def adapter(plugin):
    """An adapter the hooks can see, with no server: the test marks sessions attached itself."""
    adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
        "token": "tok", "voicemanager_url": "ws://127.0.0.1:1", "host": "127.0.0.1", "port": 0}))
    plugin.stream_hooks.attach(adapter)
    yield adapter
    plugin.stream_hooks.detach(adapter)


def _hook(plugin, **kwargs):
    return plugin.stream_hooks.HOOKS["pre_llm_call"](**kwargs)


def test_attached_session_gets_the_instruction(plugin, adapter):
    adapter._attached[SESSION_ID] = object()
    result = _hook(plugin, session_id=SESSION_ID, platform="tui", user_message="hi",
                   conversation_history=[], is_first_turn=True, model="m", task_id="t",
                   turn_id="u", sender_id="", parent_session_id="")
    assert isinstance(result, str) and "<say>" in result and "</say>" in result


def test_unknown_session_gets_none(plugin, adapter):
    adapter._attached[SESSION_ID] = object()
    assert _hook(plugin, session_id="20261107_101501_ffffff", user_message="hi") is None


def test_session_after_detach_gets_none(plugin, adapter):
    adapter._attached[SESSION_ID] = object()
    assert _hook(plugin, session_id=SESSION_ID) is not None
    del adapter._attached[SESSION_ID]  # what the attach handler does when the connection closes
    assert _hook(plugin, session_id=SESSION_ID) is None


def test_no_session_id_gets_none(plugin, adapter):
    adapter._attached[""] = object()
    assert _hook(plugin) is None
    assert _hook(plugin, session_id=None) is None


def test_hook_never_raises(plugin, adapter):
    class Broken:
        def is_attached(self, session_id):
            raise RuntimeError("boom")

    plugin.stream_hooks.attach(Broken())
    try:
        assert _hook(plugin, session_id=SESSION_ID) is None
    finally:
        plugin.stream_hooks._adapters.pop()


def test_hook_is_registered(plugin):
    names = []

    class Ctx:
        def register_hook(self, name, callback):
            names.append(name)

    plugin.stream_hooks.register(Ctx())
    assert "pre_llm_call" in names
