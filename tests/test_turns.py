"""End of turn: turn_end with the turn engine, the endpoint rule without it."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"


class Recorder:
    def __init__(self):
        self.events = []

    async def dispatch(self, text):
        self.events.append(("dispatch", text))

    async def cancel(self, reason):
        self.events.append(("cancel", reason))


def _frame(seg, text, *, endpoint=False, final=False, turn_id=1):
    return {"type": "transcription", "segment_index": seg, "text": text, "endpoint": endpoint,
            "is_final": final, "turn_id": turn_id}


@pytest.fixture
def classic(plugin):
    rec = Recorder()
    tracker = plugin.turns.TurnTracker(rec.dispatch, rec.cancel, turn_engine=None)
    tracker.open_turn(1)
    return tracker, rec


async def test_classic_dispatches_once_at_the_endpoint(classic):
    tracker, rec = classic
    await tracker.on_transcript(_frame(0, "what time"))
    await tracker.on_transcript(_frame(0, "what time is it"))
    assert rec.events == []
    await tracker.on_transcript(_frame(0, "what time is it", endpoint=True))
    await tracker.on_transcript(_frame(0, "What time is it?", final=True))
    assert rec.events == [("dispatch", "what time is it")]


async def test_classic_revision_supersedes_the_reply(classic):
    tracker, rec = classic
    await tracker.on_transcript(_frame(0, "book a flight to", endpoint=True))
    await tracker.on_transcript(_frame(0, "book a flight to boston"))
    await tracker.on_transcript(_frame(0, "book a flight to boston", final=True))
    assert rec.events == [("dispatch", "book a flight to"), ("cancel", "superseded"),
                          ("dispatch", "book a flight to boston")]


async def test_classic_new_words_during_a_reply_cancel_it(classic):
    tracker, rec = classic
    await tracker.on_transcript(_frame(0, "tell me a story", endpoint=True))
    await tracker.on_transcript(_frame(1, "actually stop"))
    assert rec.events == [("dispatch", "tell me a story"), ("cancel", "barge-in")]
    await tracker.on_transcript(_frame(0, "tell me a story", final=True))  # late confirmation
    await tracker.on_transcript(_frame(1, "actually stop", endpoint=True))
    assert rec.events[-1] == ("dispatch", "actually stop")
    assert len(rec.events) == 3


async def test_classic_ignores_frames_of_another_turn(classic):
    tracker, rec = classic
    await tracker.on_transcript(_frame(0, "old words", endpoint=True, turn_id=0))
    assert rec.events == []


async def test_turn_engine_dispatches_on_turn_end_only(plugin):
    rec = Recorder()
    tracker = plugin.turns.TurnTracker(rec.dispatch, rec.cancel, turn_engine="tyger")
    await tracker.on_transcript(_frame(0, "hello there", endpoint=True))
    await tracker.on_transcript(_frame(0, "hello there", final=True))
    assert rec.events == []
    await tracker.on_turn_end({"type": "turn_end", "text": "hello there", "turn_id": 0})
    assert rec.events == [("dispatch", "hello there")]
    tracker.reply_text()
    await tracker.on_turn_end({"type": "turn_end", "text": "wait", "turn_id": 0})
    assert rec.events[1:] == [("cancel", "barge-in"), ("dispatch", "wait")]


# --- through the adapter and a fake VoiceManager ---------------------------------------


async def _run_page(plugin, fake, script):
    adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
        "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
    events = []

    async def handle_message(event):
        events.append(event)

    adapter.handle_message = handle_message
    assert await adapter.connect()
    try:
        async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}") as page:
            while json.loads(await asyncio.wait_for(page.recv(), 3)).get("type") != "ready":
                pass
            await page.send(b"\x00\x00" * 160)
            (conn,) = await fake.wait_connections(1)
            await script(conn, events)
    finally:
        await adapter.disconnect()
    return events


async def test_turn_end_sends_one_voice_message(plugin):
    async def script(conn, events):
        await conn.send(_frame(0, "what is the weather", turn_id=0))
        await conn.send(_frame(0, "what is the weather", final=True, turn_id=0))
        await asyncio.sleep(0.2)
        assert events == []
        await conn.send({"type": "turn_end", "text": "what is the weather", "turn_id": 0,
                         "mic_sample": 1000, "by": "model"})
        await wait_for(lambda: events)
        await asyncio.sleep(0.1)

    async with FakeVoiceManager() as fake:
        events = await _run_page(plugin, fake, script)
    assert len(events) == 1
    event = events[0]
    assert event.text == "what is the weather"
    assert event.message_type == MessageType.VOICE
    assert event.media_urls == []
    assert event.source.chat_type == "dm"
    assert event.source.user_id == "owner"


async def test_classic_endpoint_sends_one_voice_message(plugin):
    async def script(conn, events):
        await wait_for(lambda: conn.json_frames("turn_start"))
        await conn.send(_frame(0, "hi", turn_id=1))
        await conn.send(_frame(0, "hi hermes", endpoint=True, turn_id=1))
        await conn.send(_frame(0, "hi hermes", final=True, turn_id=1))
        await wait_for(lambda: events)
        await asyncio.sleep(0.1)

    async with FakeVoiceManager(turn_engine_refusal="turn_engine_unsupported") as fake:
        events = await _run_page(plugin, fake, script)
    assert [e.text for e in events] == ["hi hermes"]


def test_the_gateway_does_not_transcribe_a_text_voice_message(plugin):
    from gateway.run_inbound_transcription import GatewayInboundTranscriptionMixin as Mixin

    event = SimpleNamespace(media_urls=[], media_types=[], text="hello")
    assert Mixin._pending_event_audio_paths(None, event) == []


# --- the voice-turn route keeps or turns off reasoning ---------------------------------


def _agent(reasoning):
    return SimpleNamespace(
        _voice_turn_pending=True, provider="custom", base_url="http://127.0.0.1:8080/v1",
        model="local-model", api_mode="chat_completions", reasoning_config=reasoning,
        _cached_system_prompt="sys")


@pytest.mark.parametrize("configured, expected", [
    ("none", {"enabled": False}),
    ("", {"enabled": True, "effort": "high"}),
])
def test_voice_turn_reasoning_follows_auxiliary_voice_chat(monkeypatch, configured, expected):
    import agent.auxiliary_task_config as task_config
    from agent.voice_turn_route import begin_voice_turn_route

    monkeypatch.setattr(task_config, "_get_auxiliary_task_config",
                        lambda task: {"provider": "auto", "model": "", "reasoning_effort": configured})
    agent = _agent({"enabled": True, "effort": "high"})
    begin_voice_turn_route(agent, [], "sys")
    assert agent.reasoning_config == expected
