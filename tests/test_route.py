"""A stream hook event reaches the adapter of the turn's profile, not just any adapter
with a session for the chat id."""

from __future__ import annotations

import contextvars
import threading

import pytest

from gateway.session_context import set_session_vars

PLATFORM = "voicemanager_live"


class FakeAdapter:
    """Only what the routing reads: the owner profile, the sessions by chat id, a loop."""

    def __init__(self, owner, chat_ids=("voice",)):
        self._owner_profile = owner
        self.loop = None
        self.sessions = {chat_id: object() for chat_id in chat_ids}

    def session_for(self, chat_id):
        return self.sessions.get(chat_id)


@pytest.fixture
def hooks(plugin):
    attached = []

    def attach(*adapters):
        for adapter in adapters:
            plugin.stream_hooks.attach(adapter)
            attached.append(adapter)

    yield plugin.stream_hooks, attach
    for adapter in attached:
        plugin.stream_hooks.detach(adapter)


def route(hooks, profile, chat_id="voice"):
    """Run the routing inside a turn's session context, off the loop, as Hermes does."""
    result = []

    def run():
        set_session_vars(platform=PLATFORM, chat_id=chat_id, profile=profile)
        result.append(hooks._route(None))

    thread = threading.Thread(target=contextvars.Context().run, args=(run,))
    thread.start()
    thread.join()
    return result[0]


def test_profile_a_reaches_adapter_a_only(hooks):
    stream_hooks, attach = hooks
    a, b = FakeAdapter("a"), FakeAdapter("b")
    attach(a, b)
    assert route(stream_hooks, "a") == (a, a.sessions["voice"])


def test_profile_b_reaches_adapter_b_only(hooks):
    stream_hooks, attach = hooks
    a, b = FakeAdapter("a"), FakeAdapter("b")
    attach(b, a)  # a is the newest: the profile, not the order, picks b
    assert route(stream_hooks, "b") == (b, b.sessions["voice"])


def test_empty_profile_matches_the_adapter_without_owner(hooks):
    stream_hooks, attach = hooks
    plain, a = FakeAdapter(None), FakeAdapter("a")
    attach(plain, a)
    assert route(stream_hooks, "") == (plain, plain.sessions["voice"])


def test_default_profile_matches_the_adapter_without_owner(hooks):
    stream_hooks, attach = hooks
    plain, a = FakeAdapter(None), FakeAdapter("a")
    attach(plain, a)
    assert route(stream_hooks, "default") == (plain, plain.sessions["voice"])


def test_unknown_profile_falls_back_to_the_newest_adapter_with_a_session(hooks):
    stream_hooks, attach = hooks
    a, b = FakeAdapter("a"), FakeAdapter("b")
    attach(a, b)
    assert route(stream_hooks, "c") == (b, b.sessions["voice"])


def test_matching_profile_without_a_session_falls_back(hooks):
    stream_hooks, attach = hooks
    a, b = FakeAdapter("a"), FakeAdapter("b", chat_ids=("tablet",))
    attach(a, b)
    assert route(stream_hooks, "b") == (a, a.sessions["voice"])
