"""Load the plugin as a package named ``voicemanager_live``.

The repository root is the plugin directory, and its name has dashes, so
the tests import it through an explicit spec instead of sys.path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests import fake_vm directly
PACKAGE = "voicemanager_live"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_load_package()


def _register_platform():
    """Put the platform in Hermes's registry, as the plugin loader does, so Platform() knows it."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    adapter = sys.modules[PACKAGE].adapter
    if not platform_registry.is_registered(adapter.PLATFORM):
        platform_registry.register(PlatformEntry(
            name=adapter.PLATFORM, label="VoiceManager Live",
            adapter_factory=adapter.VoiceLiveAdapter, check_fn=adapter.check_requirements))


_register_platform()


@pytest.fixture
def plugin():
    return sys.modules[PACKAGE]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("VMLIVE_TOKEN", "VMLIVE_VOICEMANAGER_URL", "VMLIVE_VOICEMANAGER_TOKEN",
                 "VMLIVE_HOST", "VMLIVE_PORT", "VMLIVE_ALLOWED_USERS"):
        monkeypatch.delenv(name, raising=False)
