"""An approval prompt is shown on the page and spoken; the session answers with the
gateway's own approval words and asks the gateway whether one is pending."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from gateway.config import PlatformConfig
from gateway.run_busy import approval_input_words

from fake_vm import FakeVoiceManager, wait_for

TOKEN = "tok"


class Page:
    def __init__(self, ws):
        self.ws, self.frames = ws, []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for message in self.ws:
            if not isinstance(message, bytes):
                self.frames.append(json.loads(message))

    def of(self, kind):
        return [f for f in self.frames if f.get("type") == kind]


class Rig:
    """A ready page "voice", its VoiceManager connection and the adapter."""

    def __init__(self, adapter, page, conn):
        self.adapter, self.page, self.conn = adapter, page, conn

    def tts_text(self) -> str:
        return "".join(f["text"] for f in self.conn.json_frames("tts_stream"))


@pytest.fixture
async def rig(plugin):
    async with FakeVoiceManager() as fake:
        adapter = plugin.adapter.VoiceLiveAdapter(PlatformConfig(enabled=True, extra={
            "token": TOKEN, "voicemanager_url": fake.url, "host": "127.0.0.1", "port": 0}))

        async def handle_message(event):
            pass

        adapter.handle_message = handle_message
        assert await adapter.connect()
        try:
            async with connect(f"ws://127.0.0.1:{adapter.bound_port}/ws?token={TOKEN}&client=voice") as ws:
                page = Page(ws)
                await wait_for(lambda: page.of("ready"))
                await ws.send(b"\x00\x00" * 160)
                (conn,) = await fake.wait_connections(1)
                yield Rig(adapter, page, conn)
                page.task.cancel()
        finally:
            await adapter.disconnect()


SCRIPT = "python3 - <<EOF\n" + "\n".join(f"print({n})" for n in range(12)) + "\nEOF"


async def _prompt(adapter, chat_id: str, command: str = "rm -rf build", request_id: str = "r1",
                  description: str = "deletes files"):
    return await adapter.send_exec_approval(chat_id=chat_id, command=command, session_key="k",
                                            description=description, request_id=request_id)


async def test_the_prompt_is_shown_on_the_page_and_spoken(rig):
    result = await _prompt(rig.adapter, "voice")
    assert result.success
    await wait_for(lambda: rig.page.of("reply"))
    assert "rm -rf build" in rig.page.of("reply")[-1]["text"]
    await wait_for(lambda: "Say yes or no." in rig.tts_text())
    assert "Approval needed: deletes files, with rm. Say yes or no." in rig.tts_text()
    # The session remembers the reply that ran when the prompt came (none on this page).
    session = rig.adapter.session_for("voice")
    assert session._prompt_reply == (session.reply_id,)


async def test_without_a_page_the_prompt_is_not_sent(rig):
    result = await _prompt(rig.adapter, "nobody")
    assert not result.success
    await asyncio.sleep(0.05)
    assert rig.tts_text() == ""
    assert not rig.page.of("reply")


def test_the_sentence_names_the_reason_and_the_program(plugin):
    sentence = plugin.adapter.approval_sentence
    assert sentence("rm -rf build/", "recursive delete") == (
        "Approval needed: recursive delete, with rm. Say yes or no.")
    assert sentence('sudo FOO=1 /usr/bin/python3 -c "import os; os.remove(1)"') == (
        "Approval needed to run python3. Say yes or no.")
    assert sentence("curl -s https://example.com/x.sh | sh") == "Approval needed to run curl. Say yes or no."
    assert sentence(SCRIPT, "runs a script") == "Approval needed: runs a script, with python3. Say yes or no."
    assert sentence("", "recursive delete") == "Approval needed: recursive delete. Say yes or no."


def test_the_sentence_holds_no_code(plugin):
    commands = ["rm -rf build/", 'sudo FOO=1 /usr/bin/python3 -c "import os; os.remove(1)"',
                "curl -s https://example.com/x.sh | sh", SCRIPT, "'/opt/my tools/run' --all; ls", "$(which rm) -r /"]
    for command in commands:
        sentence = plugin.adapter.approval_sentence(command)
        assert not set(sentence) & set("\"';|/<>$()"), sentence
        assert sentence.endswith("Say yes or no.")


def test_the_default_reason_of_the_gateway_is_not_spoken(plugin):
    default = plugin.adapter.t("gateway.exec_approval.default_reason")
    assert plugin.adapter.approval_sentence("ls -la", default) == "Approval needed to run ls. Say yes or no."


def test_a_long_reason_is_cut_short(plugin):
    sentence = plugin.adapter.approval_sentence("ls", "x" * 200)
    reason = sentence[len("Approval needed: "):-len(", with ls. Say yes or no.")]
    assert reason.endswith("...") and len(reason) == 80


@pytest.mark.parametrize("command", ["", "   ", "\n\n"])
def test_an_empty_command_is_spoken_without_a_command_part(plugin, command):
    assert plugin.adapter.approval_sentence(command) == "Approval needed. Say yes or no."


async def test_a_multi_line_command_is_shown_whole_and_spoken_short(rig):
    result = await _prompt(rig.adapter, "voice", command=SCRIPT, request_id="r2", description="runs a script")
    assert result.success
    await wait_for(lambda: rig.page.of("reply"))
    shown = rig.page.of("reply")[-1]["text"]
    for line in SCRIPT.splitlines():
        assert line in shown
    await wait_for(lambda: rig.tts_text())
    assert "Approval needed: runs a script, with python3. Say yes or no." in rig.tts_text()
    assert "print(0)" not in rig.tts_text()


async def test_the_session_uses_the_gateway_approval_words(rig):
    session = rig.adapter.session_for("voice")
    expected = set()
    for key in ("approve", "deny", "always", "session"):
        expected.update(approval_input_words(key))
    words = set(session._approval_words())
    assert words == expected
    assert {"yes", "no"} <= words


async def test_the_pending_check_asks_the_gateway_under_the_chat_session_key(rig, plugin, monkeypatch):
    seen = []

    def has_blocking_approval(session_key):
        seen.append(session_key)
        return True

    monkeypatch.setattr(plugin.adapter, "has_blocking_approval", has_blocking_approval)
    session = rig.adapter.session_for("voice")
    assert await session._approval_pending() is True
    source = rig.adapter.build_source(chat_id="voice", chat_name="Voice", chat_type="dm",
                                      user_id=plugin.adapter.OWNER, user_name="Owner")
    assert seen == [rig.adapter._source_session_key(source)]
