import asyncio
import base64
from io import BytesIO
import json

import httpx
from PIL import Image
import pymupdf
import pytest
from textual.widgets import Button, Input, Select, Static, TextArea

from pageparse.app import Confirm, PageParse
from pageparse.config import Settings
from pageparse.models import Nemotron, Ollama
from pageparse.parse_view import ChooseSource, SourceTree

RAW = r"<x_0.1><y_0.2>Table<x_0.7><y_0.3><class_Table>"
TABLE = "<table><tr><td>Revenue</td><td>42</td></tr></table>"


def helper(raw, *, image_size):
    assert raw == RAW
    width, height = image_size
    return {
        "markdown": TABLE,
        "blocks": [{"class": "Table", "bbox": [.1, .2, .7, .3], "text": "Table", "content": TABLE,
                    "bbox_pixels": [round(.1 * width), round(.2 * height),
                                    round(.7 * width), round(.3 * height)]}],
        "warnings": [],
    }


class FixtureServices:
    def __init__(self):
        self.vllm_state = "ready"
        self.ollama_state = "ready"
        self.sizes = []
        self.block_page = None
        self.parse_waiting = asyncio.Event()
        self.parse_closed = False
        self.chat_bodies = []
        self.block_turn = None
        self.chat_waiting = asyncio.Event()
        self.chat_closed = False

    def app(self, tmp_path):
        self.settings = Settings(index=tmp_path / ".state/exports.sqlite3")
        transport = httpx.MockTransport(self.route)
        return PageParse(self.settings, nemotron=Nemotron(self.settings, transport, helper),
                         ollama=Ollama(self.settings, transport))

    async def route(self, request):
        match request.url.path:
            case "/v1/models":
                if self.vllm_state == "offline":
                    raise httpx.ConnectError("offline")
                name = "nemotron-parse" if self.vllm_state == "ready" else "other-model"
                return httpx.Response(200, json={"data": [{"id": name}]})
            case "/api/tags":
                if self.ollama_state == "offline":
                    raise httpx.ConnectError("offline")
                names = ["alternate:latest", "qwen2.5-coder:3b"] if self.ollama_state == "ready" else []
                return httpx.Response(200, json={"models": [{"name": name} for name in names]})
            case "/v1/chat/completions":
                parts = json.loads(request.content)["messages"][0]["content"]
                png = parts[1]["image_url"]["url"].split(",", 1)[1]
                with Image.open(BytesIO(base64.b64decode(png))) as image:
                    self.sizes.append(image.size)
                if len(self.sizes) == self.block_page:
                    self.parse_waiting.set()
                    try:
                        await asyncio.Future()
                    finally:
                        self.parse_closed = True
                return httpx.Response(200, json={"choices": [
                    {"finish_reason": "stop", "message": {"content": RAW}},
                ]})
            case "/api/chat":
                self.chat_bodies.append(json.loads(request.content))
                if len(self.chat_bodies) == self.block_turn:
                    return httpx.Response(200, stream=SlowStream(self))
                return httpx.Response(200, content=frame("answer") + frame("", done=True))
        raise AssertionError(request.url.path)


def frame(text, done=False):
    return (json.dumps({"message": {"content": text}, "done": done}) + "\n").encode()


class SlowStream(httpx.AsyncByteStream):
    def __init__(self, services):
        self.services = services

    async def __aiter__(self):
        yield frame("visible partial")
        self.services.chat_waiting.set()
        await asyncio.Future()

    async def aclose(self):
        self.services.chat_closed = True


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.02)


async def ready(app):
    await until(lambda: app.parse_view.status and app.chat_view.status and app.exports_view.available)


def pdf_fixture(path):
    with pymupdf.open() as doc:
        for width in (72, 144, 216):
            page = doc.new_page(width=width, height=144)
            page.insert_text((15, 30), f"Page {doc.page_count}")
        doc.save(path)


@pytest.mark.parametrize("viewport", [(80, 24), (120, 36)])
def test_file_picker_ordered_preview_export_and_delete(tmp_path, monkeypatch, viewport):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "incoming.pdf"
    pdf_fixture(source)
    app = FixtureServices().app(tmp_path)

    async def verify():
        async with app.run_test(size=viewport) as pilot:
            await ready(app)
            assert app.active_workspace() == "parse"
            controls = app.query_one("#parse-controls").region
            preview = app.query_one("#preview-panel").region
            if viewport[0] == 80:
                assert controls.y < preview.y and controls.width == preview.width
            else:
                assert controls.x < preview.x and controls.y == preview.y
            assert preview.height >= 8

            await pilot.click("#browse")
            assert isinstance(app.screen, ChooseSource)
            tree = app.screen.query_one(SourceTree)
            await until(lambda: len(tree.root.children) == 1)
            await pilot.pause()
            await pilot.press("down", "enter")
            await until(lambda: app.parse_view.query_one("#source", Input).value == str(source))
            app.query_one("#pages", Input).value = "3,2,2"
            await pilot.press("f5")
            await until(lambda: app.parse_view.result is not None)
            assert app.parse_view.result["selected_pages"] == [2, 3]
            assert app.nemotron.transport is app.ollama.transport
            assert app.parse_view.result["pages"][0]["blocks"][0]["bbox_pixels"] == [30, 60, 210, 90]
            assert [p["width_px"] for p in app.parse_view.result["pages"]] == [300, 450]
            assert list(tmp_path.glob("*.md")) == []
            assert list(tmp_path.glob("*.json")) == []
            assert list(tmp_path.glob("*.png")) == []
            app.query_one("#preview-mode", Select).value = "markdown"
            await until(lambda: TABLE in app.query_one("#raw-preview", TextArea).text)
            app.query_one("#preview-mode", Select).value = "json"
            await until(lambda: '"schema_version": 1' in app.query_one("#raw-preview", TextArea).text)

            await pilot.press("f7")
            await until(lambda: (tmp_path / "incoming.json").exists() and bool(app.exports_view.groups))
            export = json.loads((tmp_path / "incoming.json").read_text())
            assert export["status"] == "complete"
            assert [page["page_number"] for page in export["pages"]] == [2, 3]
            assert TABLE in (tmp_path / "incoming.md").read_text()

            await pilot.press("f3")
            await until(lambda: app.active_workspace() == "exports")
            assert app.focused.id == "export-list"
            await pilot.press("space")
            await until(lambda: not pilot.app.query_one("#delete-md", Button).disabled)
            await pilot.click("#delete-md")
            await until(lambda: isinstance(app.screen, Confirm))
            assert str(tmp_path / "incoming.md") in app.screen.text
            assert "known bytes" in app.screen.text
            await pilot.press("escape")
            assert (tmp_path / "incoming.md").exists()
            await until(lambda: app.exports_view.index_task is None)
            await pilot.click("#delete-md")
            await until(lambda: isinstance(app.screen, Confirm))
            await pilot.click("#confirm")
            await until(lambda: not (tmp_path / "incoming.md").exists())
            assert (tmp_path / "incoming.json").exists() and source.exists()
            await until(lambda: app.exports_view.index_task is None)

    asyncio.run(verify())


@pytest.mark.parametrize("viewport", [(80, 24), (120, 36)])
def test_cancel_parse_confirm_partial_and_reopen_offline(tmp_path, viewport):
    source = tmp_path / "incoming.pdf"
    pdf_fixture(source)
    services = FixtureServices()
    services.block_page = 2
    app = services.app(tmp_path)

    async def verify():
        async with app.run_test(size=viewport) as pilot:
            await ready(app)
            app.query_one("#source", Input).value = str(source)
            await pilot.press("f5")
            await asyncio.wait_for(services.parse_waiting.wait(), 5)
            assert services.sizes == [(150, 300), (300, 300)]
            assert app.query_one("#start", Button).disabled
            assert app.query_one("#save-export", Button).disabled
            await pilot.press("f2")
            assert app.active_workspace() == "chat"
            assert app.focused.id == "prompt"
            app.query_one("#prompt", TextArea).load_text("What is 2 + 2?")
            assert app.query_one("#send", Button).disabled
            await pilot.press("f6")
            await until(lambda: app.slot is None)
            assert services.parse_closed
            await pilot.press("f1", "f7")
            await until(lambda: isinstance(app.screen, Confirm))
            assert "partial" in app.screen.heading
            assert "incoming.partial.md" in app.screen.text and "incoming.partial.json" in app.screen.text
            await pilot.press("escape")
            await until(lambda: app.parse_view.export_task is None)
            assert list(tmp_path.glob("*.partial.*")) == []
            await pilot.press("f7")
            await until(lambda: isinstance(app.screen, Confirm))
            occupied = tmp_path / "incoming.partial.md"
            occupied.write_text("Unrelated file")
            await pilot.click("#confirm")
            await until(lambda: isinstance(app.screen, Confirm) and "incoming.partial-2.md" in app.screen.text)
            assert "incoming.partial-2.json" in app.screen.text
            await pilot.click("#confirm")
            await until(lambda: app.parse_view.export_task is None)
            export = json.loads((tmp_path / "incoming.partial-2.json").read_text())
            assert export["status"] == "cancelled"
            assert export["failure"] is None
            assert export["selected_pages"] == [1, 2, 3]
            assert [page["page_number"] for page in export["pages"]] == [1]
            await until(lambda: app.exports_view.index_task is None)

        services.vllm_state = services.ollama_state = "offline"
        reopened = services.app(tmp_path)
        async with reopened.run_test(size=viewport) as pilot:
            await ready(reopened)
            assert reopened.parse_view.status.state == reopened.chat_view.status.state == "offline"
            await pilot.press("f3")
            await until(lambda: reopened.active_workspace() == "exports")
            await pilot.press("space")
            await until(lambda: not pilot.app.query_one("#delete-md", Button).disabled)
            await pilot.click("#delete-md")
            await until(lambda: isinstance(reopened.screen, Confirm))
            await pilot.click("#confirm")
            await until(lambda: not (tmp_path / "incoming.partial-2.md").exists())
            await until(lambda: reopened.exports_view.index_task is None)
            assert (tmp_path / "incoming.partial-2.json").exists() and source.exists()
            assert occupied.read_text() == "Unrelated file"

    asyncio.run(verify())


@pytest.mark.parametrize("viewport", [(80, 24), (120, 36)])
def test_chat_history_stop_and_model_switch(tmp_path, viewport):
    services = FixtureServices()
    services.block_turn = 3
    app = services.app(tmp_path)

    async def send(pilot, prompt):
        app.query_one("#prompt", TextArea).load_text(prompt)
        await pilot.press("f5")
        await until(lambda: app.chat_view.turns[-1].prompt == prompt)

    async def verify():
        async with app.run_test(size=viewport) as pilot:
            await ready(app)
            await pilot.press("f2")
            assert app.query_one("#chat-model", Select).value == "qwen2.5-coder:3b"
            await send(pilot, "First")
            await until(lambda: app.slot is None)
            await send(pilot, "Follow-up")
            await until(lambda: app.slot is None)
            assert services.chat_bodies[1]["messages"] == [
                {"role": "user", "content": "First"}, {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "Follow-up"},
            ]

            await send(pilot, "Drop this turn")
            await asyncio.wait_for(services.chat_waiting.wait(), 5)
            assert app.query_one("#new-chat", Button).disabled
            assert app.query_one("#chat-model", Select).disabled
            await pilot.press("f6")
            await until(lambda: app.slot is None)
            assert services.chat_closed
            assert "stopped · incomplete" in str(app.query_one("#transcript", Static).render())
            assert "visible partial" in str(app.query_one("#transcript", Static).render())

            await send(pilot, "Continue")
            await until(lambda: app.slot is None)
            messages = services.chat_bodies[3]["messages"]
            assert [message["content"] for message in messages] == ["First", "answer", "Follow-up", "answer", "Continue"]
            app.query_one("#chat-model", Select).value = "alternate:latest"
            await until(lambda: app.chat_view.model == "alternate:latest")
            assert app.chat_view.turns == []
            await send(pilot, "Different model")
            await until(lambda: app.slot is None)
            assert services.chat_bodies[4]["messages"] == [{"role": "user", "content": "Different model"}]
            await pilot.click("#new-chat")
            assert app.chat_view.turns == []

    asyncio.run(verify())


def test_discovery_refresh(tmp_path):
    services = FixtureServices()
    services.vllm_state = services.ollama_state = "offline"
    app = services.app(tmp_path)

    async def verify():
        async with app.run_test() as pilot:
            await ready(app)
            assert app.parse_view.status.state == app.chat_view.status.state == "offline"
            services.vllm_state = services.ollama_state = "missing"
            await pilot.press("f4")
            await until(lambda: app.parse_view.status.state == app.chat_view.status.state == "model missing")
            services.vllm_state = services.ollama_state = "ready"
            await pilot.press("f4")
            await until(lambda: app.parse_view.status.state == app.chat_view.status.state == "ready")

    asyncio.run(verify())


def test_chat_cap_keeps_only_complete_utf8(tmp_path, monkeypatch):
    monkeypatch.setattr("pageparse.chat_view.CHAT_LIMIT", 9)
    services = FixtureServices()
    services.block_turn = 1
    app = services.app(tmp_path)

    async def verify():
        async with app.run_test() as pilot:
            await ready(app)
            await pilot.press("f2")
            composer = app.query_one("#prompt", TextArea)
            composer.load_text("1234567890")
            await pilot.press("f5")
            assert not services.chat_bodies
            assert "Transcript limit" in str(app.query_one("#chat-feedback", Static).render())
            composer.load_text("€")
            await pilot.press("f5")
            await until(lambda: bool(app.chat_view.turns) and app.slot is None)
            turn, = app.chat_view.turns
            assert turn.state == "failed · incomplete"
            assert turn.reply == "visibl"
            assert app.chat_view.retained == 9

    asyncio.run(verify())
