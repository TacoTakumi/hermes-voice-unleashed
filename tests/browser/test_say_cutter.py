"""The Desktop plugin's say cutter, run in headless Chrome.

The model starts each reply with a spoken summary in <say>...</say>. The
cutter gets the reply as streamed text deltas and gives back only the text
of the first say block, as it streams. The harness page loads
desktop/plugin.js as an ES module with stub SDK imports.
"""

from __future__ import annotations

import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.async_api")

ROOT = Path(__file__).resolve().parents[2]
OPEN, CLOSE = "<say>", "</say>"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    handler = functools.partial(_QuietHandler, directory=str(ROOT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
async def page(site):
    async with playwright_api.async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=os.environ.get("CHROME") or None, headless=True)
        page = await browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        await page.goto(f"{site}/tests/browser/cutter_harness.html")
        await page.wait_for_function("window.harnessReady || window.harnessError", timeout=10000)
        harness_error = await page.evaluate("window.harnessError || ''")
        assert not harness_error, harness_error
        assert not errors, errors
        try:
            yield page
        finally:
            await browser.close()


async def run(page, deltas):
    return await page.evaluate("deltas => window.runCutter(deltas)", deltas)


async def run_many(page, cases):
    return await page.evaluate("cases => cases.map(deltas => window.runCutter(deltas))", cases)


def said(result):
    return "".join(result["pushes"]) + result["end"]


async def test_plugin_still_has_its_default_export(page):
    assert await page.evaluate("typeof window.pluginDefault.register") == "function"
    assert await page.evaluate("typeof window.createSayCutter") == "function"


async def test_a_block_in_one_delta(page):
    result = await run(page, ["<say>Sunny and warm.</say>"])
    assert result["pushes"] == ["Sunny and warm."]
    assert result["end"] == ""


async def test_text_before_and_after_the_block_is_dropped(page):
    result = await run(page, ["Well, ", "<say>Sunny.</say>", " The full forecast follows."])
    assert result["pushes"] == ["", "Sunny.", ""]
    assert result["end"] == ""


async def test_say_text_streams_as_it_arrives(page):
    result = await run(page, ["<say>", "Sunny ", "and warm", ".</say>", " More."])
    assert result["pushes"] == ["", "Sunny ", "and warm", ".", ""]


async def test_tags_split_at_every_position(page):
    reply = "Hi <say>Sunny and warm.</say> The full forecast."
    cases = []
    for tag in (OPEN, CLOSE):
        start = reply.index(tag)
        for cut in range(start, start + len(tag) + 1):
            cases.append([reply[:cut], reply[cut:]])
        for cut in range(start + 1, start + len(tag)):  # tag split in three pieces
            cases.append([reply[:start + 1], reply[start + 1:cut], reply[cut:]])
    cases.append(list(reply))  # one character per delta
    results = await run_many(page, cases)
    for deltas, result in zip(cases, results):
        assert said(result) == "Sunny and warm.", deltas
        assert result["end"] == "", deltas


async def test_only_a_possible_partial_tag_is_held(page):
    result = await run(page, ["<say>a<", "b", " c</s", "x", " d</sa", "y> e"])
    assert result["pushes"] == ["a", "<b", " c", "</sx", " d", ""]
    result = await run(page, ["x<s", "o<say>ok", "</say>"])
    assert result["pushes"] == ["", "ok", ""]


async def test_end_flushes_an_unclosed_block(page):
    result = await run(page, ["<say>Sunny and", " warm"])
    assert result["pushes"] == ["Sunny and", " warm"]
    assert result["end"] == ""
    result = await run(page, ["<say>Sunny</s"])
    assert result["pushes"] == ["Sunny"]
    assert result["end"] == ""  # a partial closing tag is not text
    result = await run(page, ["<say>Sunny <"])
    assert said(result) == "Sunny "


async def test_end_closes_the_cutter(page):
    result = await run(page, ["<say>Sunny"])
    assert result["after"] == ""
    assert result["endAgain"] == ""


async def test_a_reply_without_a_block_says_nothing(page):
    result = await run(page, ["The full forecast: ", "sunny, <b>warm</b> and <s", "ay"])
    assert result["pushes"] == ["", "", ""]
    assert result["end"] == ""
    result = await run(page, [])
    assert result["end"] == ""


async def test_a_second_block_is_ignored(page):
    result = await run(page, ["<say>First.</say> text ", "<say>Second.</say>", " end"])
    assert said(result) == "First."
    assert result["pushes"][1:] == ["", ""]
