"""A reply that starts with a say block (a Desktop voice chat the page joined) is spoken
by its first block only, and say tags never reach the page."""

from __future__ import annotations

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig

from fake_vm import FakeVoiceManager, wait_for
from test_stream import PLATFORM, TOKEN, Page, ended, fire, tts_frames, voicemanager_join

SAY_REPLY = "<say>Hi there.</say> Full text with details."
# The reply's deltas, cut so that both tags are split across deltas.
SAY_DELTAS = ["<sa", "y>Hi th", "ere.</s", "ay> Full text", " with details."]
assert "".join(SAY_DELTAS) == SAY_REPLY


@pytest.fixture
async def rig(plugin):
    """A page with an utterance under way, as in the stream tests, plus the adapter."""
    async with FakeVoiceManager() as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))
        messages = []

        async def handle_message(event):
            messages.append(event)

        adapter.handle_message = handle_message
        assert await adapter.connect()
        try:
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=voice") as ws:
                page = Page(ws)
                await wait_for(lambda: page.of("ready"))
                await ws.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                await conn.send({"type": "turn_end", "text": "tell me something", "turn_id": 0})
                await wait_for(lambda: messages)
                yield plugin.stream_hooks, conn, page, adapter
                page.task.cancel()
        finally:
            await adapter.disconnect()


def stream(hooks, deltas):
    for delta in deltas:
        fire(hooks.on_stream_delta, delta=delta, kind="text", turn_id="t1", iteration=1, surface=PLATFORM)
    fire(hooks.on_session_end, turn_id="t1", interrupted=False, platform=PLATFORM)


async def test_a_leading_say_block_is_spoken_and_the_rest_only_shown(rig):
    hooks, conn, page, adapter = rig
    stream(hooks, SAY_DELTAS)
    frames = await wait_for(lambda: ended(conn))
    spoken = voicemanager_join(frames)
    assert spoken == "Hi there."
    assert "Full" not in spoken and "<" not in spoken
    await wait_for(lambda: "details." in "".join(f["text"] for f in page.of("reply_delta")))
    shown = "".join(f["text"] for f in page.of("reply_delta"))
    assert "<" not in shown and ">" not in shown
    assert "Hi there." in shown and "Full text with details." in shown


async def test_the_reply_frame_carries_no_say_tags(rig):
    hooks, conn, page, adapter = rig
    result = await adapter.send("voice", "<say>Hi there.</say> Full text")
    assert result.success
    await wait_for(lambda: page.of("reply"))
    assert page.of("reply")[-1]["text"] == "Hi there. Full text"


async def test_a_reply_without_a_say_block_is_spoken_whole(rig):
    hooks, conn, page, adapter = rig
    stream(hooks, ["Once", " upon", " a ti", "me"])
    frames = await wait_for(lambda: ended(conn))
    assert "".join(f["text"] for f in frames) == "Once upon a time"
    assert voicemanager_join(frames) == "Once upon a time"
    await wait_for(lambda: "".join(f["text"] for f in page.of("reply_delta")) == "Once upon a time")


async def test_text_that_only_looked_like_a_tag_is_spoken(rig):
    """Text is held back only while it could still become an opening say tag."""
    hooks, conn, page, adapter = rig
    stream(hooks, ["<s", "ample> is", " not a tag."])
    frames = await wait_for(lambda: ended(conn))
    assert "".join(f["text"] for f in frames) == "<sample> is not a tag."
    await wait_for(lambda: "".join(f["text"] for f in page.of("reply_delta")) == "<sample> is not a tag.")


def test_the_say_cutter_holds_a_split_tag_and_releases_what_cannot_be_one(plugin):
    cutter = plugin.session.SayCutter()
    assert cutter.push("  <sa") == ("", "")
    assert cutter.push("y>Hi th") == ("  Hi th", "Hi th")
    assert cutter.push("ere.</s") == ("ere.", "ere.")
    assert cutter.push("ay> Full text") == (" Full text", "")
    assert cutter.push(" with <say>more</say>.") == (" with more.", "")
    assert cutter.end() == ("", "")

    plain = plugin.session.SayCutter()
    assert plain.push("Once") == ("Once", "Once")
    assert plain.push(" upon <") == (" upon ", " upon ")
    assert plain.end() == ("<", "<")

    unclosed = plugin.session.SayCutter()
    assert unclosed.push("<say>Hi") == ("Hi", "Hi")
    assert unclosed.push(" there</sa") == (" there", " there")
    assert unclosed.end() == ("", "")
