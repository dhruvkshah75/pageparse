import asyncio
import base64
from io import BytesIO
import json

import httpx
from PIL import Image
import pymupdf

from pageparse.config import Settings
from pageparse.models import Nemotron, Ollama
from pageparse.documents import normalize_image
from pageparse.tui_backend import BackendSession

RAW = r"<x_0.1><y_0.2>Table<x_0.7><y_0.3><class_Table>"
TABLE = (
    '<table><tr><th rowspan="2">Block accessed</th><th rowspan="2">Hit or miss</th>'
    '<th colspan="2">Set 0</th><th colspan="2">Set 1</th></tr>'
    '<tr><th>Cache line 0</th><th>Cache line 1</th><th>Cache line 0</th><th>Cache line 1</th></tr>'
    '<tr><td>8</td><td>Miss</td><td>Block 0</td><td>Block 8</td><td>???</td><td>???</td></tr></table>'
)


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

    def session(self, tmp_path):
        settings = Settings(index=tmp_path / ".state/exports.sqlite3")
        transport = httpx.MockTransport(self.route)
        self.events = []
        session = BackendSession(settings, "manual", self.record)
        session.nemotron = Nemotron(settings, transport, helper)
        session.ollama = Ollama(settings, transport)
        session.default_destination = tmp_path / "outputs"
        return session

    def record(self, event, **fields):
        self.events.append({"event": event, **fields})

    def last(self, event):
        return next(frame for frame in reversed(self.events) if frame["event"] == event)

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


async def request(session, operation, **fields):
    session.dispatch({"op": operation, **fields})
    scope = "export" if operation.startswith("export") else operation
    if task := session.tasks.get(scope):
        await task


def pdf_fixture(path):
    with pymupdf.open() as doc:
        for width in (72, 144, 216):
            page = doc.new_page(width=width, height=144)
            page.insert_text((15, 30), f"Page {doc.page_count}")
        doc.save(path)


def test_all_pdf_pages_preview_publish_and_delete(tmp_path):
    path = tmp_path / "incoming.pdf"
    pdf_fixture(path)

    async def run():
        fixture = FixtureServices()
        session = fixture.session(tmp_path)
        try:
            await request(session, "parse", id=1, clipboard=False, source=str(path), pages=" ALL ")
            assert fixture.last("parse_done")["status"] == "complete"
            assert fixture.sizes == [(150, 300), (300, 300), (450, 300)]

            await request(session, "preview", mode="rendered", revision=1)
            assert "### Page 3" in fixture.last("preview")["content"]
            assert "| Block accessed" in fixture.last("preview")["content"]
            await request(session, "preview", mode="json", revision=2)
            result = json.loads(fixture.last("preview")["content"])
            assert result["selected_pages"] == [1, 2, 3]
            assert result["pages"][1]["blocks"][0]["bbox_pixels"] == [30, 60, 210, 90]
            assert 'colspan="2"' in result["pages"][0]["markdown"]

            await request(session, "export", destination="", format="both")
            assert (session.default_destination / "incoming.md").is_file()
            await request(session, "index")
            group, = fixture.last("index")["groups"]
            await request(session, "delete", targets=[[group["id"], "md"]])
            assert not (session.default_destination / "incoming.md").exists()
            assert (session.default_destination / "incoming.json").exists()
            (session.default_destination / "incoming.json").unlink()
            await request(session, "index")
            assert fixture.last("index")["groups"] == []
        finally:
            await session.close()

    asyncio.run(run())


def test_cancel_parse_and_confirm_partial_export(tmp_path):
    path = tmp_path / "incoming.pdf"
    pdf_fixture(path)

    async def run():
        fixture = FixtureServices()
        fixture.block_page = 2
        session = fixture.session(tmp_path)
        try:
            session.dispatch({"op": "parse", "id": 1, "clipboard": False, "source": str(path), "pages": "all"})
            await asyncio.wait_for(fixture.parse_waiting.wait(), 5)
            session.dispatch({"op": "stop_parse"})
            await session.tasks["parse"]
            assert fixture.parse_closed
            assert session.job.renderer.process is None
            assert fixture.last("parse_done")["status"] == "cancelled"

            await request(session, "export", destination="", format="md")
            assert fixture.last("export_confirm")["title"] == "Export partial result?"
            await request(session, "export_cancel")
            assert not list(session.default_destination.iterdir())
            await request(session, "export", destination="", format="md")
            await request(session, "export_accept")
            assert (session.default_destination / "incoming.partial.md").is_file()
        finally:
            await session.close()

        reopened = fixture.session(tmp_path)
        try:
            await request(reopened, "index")
            group, = fixture.last("index")["groups"]
            assert len(group["files"]) == 1
        finally:
            await reopened.close()

    asyncio.run(run())


def test_streaming_chat_stop_and_utf8_limit(tmp_path, monkeypatch):
    async def run():
        fixture = FixtureServices()
        fixture.block_turn = 2
        session = fixture.session(tmp_path)
        try:
            history = [{"role": "user", "content": "First"}]
            await request(session, "chat", model="qwen2.5-coder:3b", messages=history, retained=5)
            assert fixture.last("chat_done")["status"] == "complete"
            history = [*history, {"role": "assistant", "content": "answer"}, {"role": "user", "content": "Next"}]
            session.dispatch({"op": "chat", "model": "qwen2.5-coder:3b", "messages": history, "retained": 15})
            await asyncio.wait_for(fixture.chat_waiting.wait(), 5)
            assert fixture.last("chunk")["content"] == "visible partial"
            session.dispatch({"op": "stop_chat"})
            await session.tasks["chat"]
            assert fixture.last("chat_done")["status"] == "stopped"
            assert fixture.chat_closed
            assert fixture.chat_bodies[1]["messages"] == history

            monkeypatch.setattr("pageparse.tui_backend.CHAT_LIMIT", 9)

            async def stream(*_):
                yield "🙂🙂🙂"

            monkeypatch.setattr(session.ollama, "chat", stream)
            await request(session, "chat", model="qwen2.5-coder:3b", messages=history, retained=3)
            assert fixture.last("chunk")["content"] == "🙂"
            assert fixture.last("chat_done")["status"] == "failed"
        finally:
            await session.close()

    asyncio.run(run())


def test_paste_image_and_discovery(tmp_path, monkeypatch):
    path = tmp_path / "test.png"
    Image.new("RGB", (80, 60)).save(path)

    async def clipboard():
        return normalize_image(path)

    monkeypatch.setattr("pageparse.tui_backend.read_clipboard_image", clipboard)

    async def run():
        fixture = FixtureServices()
        session = fixture.session(tmp_path)
        try:
            fixture.vllm_state = "offline"
            session.refresh()
            await asyncio.gather(*session.tasks.values())
            assert any(e.get("name") == "Nemotron" and e["state"] == "offline" for e in fixture.events)
            fixture.vllm_state = "ready"
            await request(session, "paste", revision=3)
            assert fixture.last("clipboard") == {"event": "clipboard", "revision": 3, "width": 80, "height": 60}
            await request(session, "parse", id=2, clipboard=True, source="", pages="")
            assert fixture.last("parse_done")["status"] == "complete"
            assert session.result["source_name"] == "clipboard-image.png"
        finally:
            await session.close()

    asyncio.run(run())
