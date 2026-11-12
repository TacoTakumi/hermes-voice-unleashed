"""The VoiceManager client gives the frames of the user's turns in the server's order.

A final that the server sent before a turn end is a part of that turn, and a frame that
it sent after the turn end belongs to the next one. The order of arrival is the only
thing that tells them apart, so the client must keep it."""

from __future__ import annotations

import asyncio

from fake_vm import FakeVoiceManager


def _final(segment, text):
    return {"type": "transcription", "segment_index": segment, "text": text, "is_final": True}


def _partial(segment, text):
    return {"type": "transcription", "segment_index": segment, "text": text, "is_final": False}


def _turn_end(text):
    return {"type": "turn_end", "turn_id": 0, "text": text, "mic_sample": 1, "by": "model"}


TURN_EMPTY = {"type": "turn_empty", "mic_sample": 2, "by": "silence"}


async def _open(plugin, fake):
    client = plugin.session.client_from_url(fake.url)
    await client._connect()
    (conn,) = await fake.wait_connections(1)
    return client, client.duplex(), conn


async def _take(frames, count):
    async with asyncio.timeout(3):
        return [await anext(frames) for _ in range(count)]


def _kinds(frames):
    return [(f["type"], f.get("text")) for f in frames]


async def test_the_frames_of_a_turn_come_out_of_one_stream_in_the_servers_order(plugin):
    async with FakeVoiceManager() as fake:
        client, duplex, conn = await _open(plugin, fake)
        frames = duplex.turn_frames()
        sent = []
        for run in range(20):
            batch = [_final(3 * run, "end session"), _final(3 * run + 1, "no wait"),
                     _turn_end("end session no wait"), _partial(3 * run + 2, "I didn't"), TURN_EMPTY]
            for frame in batch:  # one after the other, nothing awaited from the client between them
                await conn.send(frame)
            sent += batch
        got = await _take(frames, len(sent))
        assert _kinds(got) == _kinds(sent)
        # A transcription frame carries a turn id, as on the stream of transcriptions.
        assert all("turn_id" in f for f in got if f["type"] == "transcription")
        await client._disconnect()


async def test_the_one_stream_ends_when_the_connection_closes(plugin):
    async with FakeVoiceManager() as fake:
        client, duplex, conn = await _open(plugin, fake)
        frames = duplex.turn_frames()
        await conn.send(_final(0, "hello"))
        assert _kinds(await _take(frames, 1)) == [("transcription", "hello")]
        await conn.ws.close()
        async with asyncio.timeout(3):
            assert [f async for f in frames] == []


async def test_a_session_that_does_not_ask_for_the_one_stream_keeps_its_two_streams(plugin):
    async with FakeVoiceManager() as fake:
        client, duplex, conn = await _open(plugin, fake)
        await conn.send(_final(0, "hello"))
        await conn.send(_turn_end("hello"))
        await conn.send(TURN_EMPTY)
        assert _kinds(await _take(duplex.transcripts(), 1)) == [("transcription", "hello")]
        assert _kinds(await _take(duplex.turn_ends(), 2)) == [("turn_end", "hello"), ("turn_empty", None)]
        await client._disconnect()
