"""Joining a Hermes session from the page: the owner is the platform's slash admin."""

from __future__ import annotations

from types import SimpleNamespace

from gateway.config import PlatformConfig
from gateway.slash_access import policy_for_source


def _config(**extra):
    base = {"token": "tok", "voicemanager_url": "http://127.0.0.1:1", "host": "127.0.0.1", "port": 0}
    base.update(extra)
    return PlatformConfig(enabled=True, extra=base)


def test_the_owner_is_the_slash_admin_without_configuration(plugin):
    config = _config()
    adapter = plugin.adapter.VoiceLiveAdapter(config)
    assert config.extra["allow_admin_from"] == [plugin.adapter.OWNER]
    gateway_config = SimpleNamespace(platforms={adapter.platform: config})
    policy = policy_for_source(gateway_config, adapter._voice_source("kitchen"))
    assert policy.enabled
    assert policy.is_admin(plugin.adapter.OWNER)


def test_a_configured_admin_list_is_kept(plugin):
    config = _config(allow_admin_from=["alice"])
    plugin.adapter.VoiceLiveAdapter(config)
    assert config.extra["allow_admin_from"] == ["alice"]
