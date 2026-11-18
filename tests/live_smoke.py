"""Run the laptop release checks with synthetic documents on actual model servers."""
import asyncio
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import httpx
from PIL import Image, ImageDraw, ImageFont
import pymupdf

from pageparse.config import Settings
from pageparse.models import Nemotron
from pageparse.tui_backend import BackendSession

TAG = "qwen2.5-coder:3b"


def fixtures(root):
    image = Image.new("RGB", (640, 480), "white")
    drawing = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=32)
    point = (54, 77)
    phrase = "Quarterly report"
    expected_box = drawing.textbbox(point, phrase, font=font)
    drawing.text(point, phrase, font=font, fill="black")
    image.save(root / "report.png")

    with pymupdf.open() as doc:
        first = doc.new_page(width=384, height=288)
        first.insert_text((36, 64), "Page one overview", fontsize=20)
        dense = doc.new_page(width=640, height=760)
        dense.insert_text((35, 39), "Quarterly operations report", fontsize=21)
        dense.insert_text((35, 61), "Small print reference A17", fontsize=8)
        for x in (35, 145, 240):
            dense.draw_line((x, 75), (x, 150), width=.8)
        for y in (75, 100, 125, 150):
            dense.draw_line((35, y), (240, y), width=.8)
        for x, y, text in (
            (44, 92, "Region"), (155, 92, "Units"),
            (44, 117, "North"), (155, 117, "42"),
            (44, 142, "South"), (155, 142, "86"),
        ):
            dense.insert_text((x, y), text, fontsize=12)
        for line in range(52):
            dense.insert_text(
                (35, 175 + line * 10.5),
                f"{line + 1:02d}. Reviewed inventory, dispatch schedules, and daily shipping targets across the region.",
                fontsize=8,
            )
        doc.save(root / "text.pdf")

    with pymupdf.open() as doc:
        raster = doc.new_page(width=307.2, height=230.4)
        png = BytesIO()
        image.save(png, "PNG")
        raster.insert_image(raster.rect, stream=png.getvalue())
        assert raster.get_text() == ""
        doc.save(root / "scanned.pdf")

    with pymupdf.open() as doc:
        doc.new_page(width=307.2, height=230.4)
        doc.save(root / "blank.pdf")
    return expected_box


class MeasuredNemotron(Nemotron):
    def __init__(self, settings):
        super().__init__(settings)
        self.completions = []

    def client(self, read=180):
        client = super().client(read)
        client.event_hooks["response"] = [self.measure]
        return client

    async def measure(self, response):
        if response.request.url.path.endswith("/chat/completions") and response.is_success:
            await response.aread()
            body = response.json()
            self.completions.append({
                "finish": body["choices"][0]["finish_reason"],
                "empty": body["choices"][0]["message"]["content"] == "",
                "usage": body.get("usage", {}),
            })


async def wait_server(probe, predicate, seconds=30):
    start = monotonic()
    async with asyncio.timeout(seconds):
        while True:
            sample = await probe()
            if predicate(sample):
                return monotonic() - start, sample
            await asyncio.sleep(.05)


async def main(root):
    settings = replace(Settings.from_env(), index=root / ".state/exports.sqlite3")
    nemotron = MeasuredNemotron(settings)
    events = []
    session = BackendSession(settings, "manual", lambda event, **fields: events.append((event, fields)))
    session.nemotron = nemotron
    session.default_destination = root
    sequence = 0
    expected_box = fixtures(root)
    vllm = urlsplit(settings.vllm_url)
    metrics_url = f"{vllm.scheme}://{vllm.netloc}/metrics"
    failures = []

    async with httpx.AsyncClient(trust_env=False, timeout=5) as monitor:
        models = (await monitor.get(settings.vllm_url + "/models")).raise_for_status().json()
        model, = [item for item in models["data"] if item["id"] == settings.nemotron_model]
        versions = (await monitor.get(settings.ollama_url + "/api/version")).raise_for_status().json()
        tags = (await monitor.get(settings.ollama_url + "/api/tags")).raise_for_status().json()
        assert TAG in [tag["name"] for tag in tags["models"]]
        print("PASS preflight", model["id"], "context", model.get("max_model_len"),
              "Ollama", versions["version"], TAG, "generation ceiling", settings.max_tokens, flush=True)

        async def running():
            response = (await monitor.get(metrics_url)).raise_for_status()
            return sum(float(value) for value in re.findall(
                r"^vllm:num_requests_(?:running|waiting)(?:\{[^}]*\})? ([0-9.e+-]+)",
                response.text, re.MULTILINE,
            ))

        async def loaded():
            response = (await monitor.get(settings.ollama_url + "/api/ps")).raise_for_status().json()
            return [(item["name"], item.get("size_vram")) for item in response["models"]]

        try:
            session.begin()
            await asyncio.gather(*session.tasks.values())
            assert {fields["name"]: fields["state"] for event, fields in events if event == "connection"} == {
                "Nemotron": "ready", "Ollama": "ready",
            }

            async def start_parse(filename, pages=""):
                nonlocal sequence
                sequence += 1
                session.dispatch({"op": "parse", "id": sequence, "clipboard": False,
                                  "source": str(root / filename), "pages": pages})
                return session.tasks["parse"]

            async def parse(filename, pages=""):
                previous = len(nemotron.completions)
                await asyncio.wait_for(await start_parse(filename, pages), 180)
                result = session.result
                assert result and result["status"] == "complete", result and result["failure"]
                for page, completion in zip(result["pages"], nemotron.completions[previous:], strict=True):
                    usage = completion["usage"]
                    print(filename, page["page_number"], f"{page['width_px']}×{page['height_px']}",
                          completion["finish"], "empty", completion["empty"],
                          "prompt tokens", usage.get("prompt_tokens"), "completion tokens", usage.get("completion_tokens"),
                          flush=True)
                return result

            async def direct():
                result = await parse("report.png")
                page, = result["pages"]
                block = next(block for block in page["blocks"] if "Quarterly report" in block["text"])
                assert all(abs(actual - expected) <= 12 for actual, expected in zip(block["bbox_pixels"], expected_box))
                print("Region", block["bbox_pixels"], "reference", expected_box, flush=True)
                session.dispatch({"op": "export", "destination": str(root), "format": "both"})
                await session.tasks["export"]
                assert (root / "report.md").is_file() and (root / "report.json").is_file()

            async def text_pdf():
                result = await parse("text.pdf", "all")
                first, dense = result["pages"]
                text = dense["markdown"]
                assert "Page one overview" in first["markdown"]
                assert "Small print reference A17" in text
                cells = [cell.get_text(strip=True) for cell in BeautifulSoup(text, "html.parser").find_all(["td", "th"])]
                assert {"Region", "Units", "North", "42", "South", "86"}.issubset(cells), cells
                assert "52." in text

            async def scanned():
                result = await parse("scanned.pdf")
                assert "Quarterly report" in result["pages"][0]["markdown"]

            async def blank():
                result = await parse("blank.pdf")
                assert result["pages"][0]["blocks"] == []
                assert result["pages"][0]["markdown"] == ""

            def send(history):
                start = len(events)
                session.dispatch({"op": "chat", "model": TAG, "messages": history,
                                  "retained": sum(len(message["content"].encode()) for message in history)})
                return session.tasks["chat"], start

            def chat_result(start):
                reply = "".join(fields["content"] for event, fields in events[start:] if event == "chunk")
                state = next(fields["status"] for event, fields in events[start:] if event == "chat_done")
                return state, reply

            async def switches():
                parsing = await start_parse("text.pdf", "2")
                await wait_server(running, lambda number: number > 0, 12)
                start = monotonic()
                session.dispatch({"op": "stop_parse"})
                await asyncio.wait_for(parsing, 5)
                assert session.result["status"] == "cancelled"
                assert session.job.renderer.process is None
                print(f"Nemotron local cancellation and renderer reap {monotonic() - start:.2f}s", flush=True)
                idle = asyncio.create_task(wait_server(running, lambda number: number == 0))

                history = [{"role": "user", "content": "Reply with exactly PARSE-27. Remember it for the next question."}]
                pending, offset = send(history)
                await asyncio.wait_for(pending, 180)
                state, reply = chat_result(offset)
                assert state == "complete" and "PARSE-27" in reply, state
                print(f"vLLM running/waiting fell to zero {((await idle)[0]):.2f}s after local cleanup", flush=True)
                history.extend([{"role": "assistant", "content": reply},
                                {"role": "user", "content": "Repeat the code from your previous answer. Give only the code."}])
                pending, offset = send(history)
                await asyncio.wait_for(pending, 180)
                state, reply = chat_result(offset)
                assert state == "complete" and "PARSE-27" in reply, state

                pending, offset = send([{"role": "user", "content":
                    "Output every number from 1 to 1000 separated by commas, without abbreviating."}])
                async with asyncio.timeout(180):
                    while not any(event == "chunk" for event, _ in events[offset:]):
                        await asyncio.sleep(.03)
                print("Ollama running model and VRAM before Stop", await loaded(), flush=True)
                start = monotonic()
                session.dispatch({"op": "stop_chat"})
                await asyncio.wait_for(pending, 5)
                assert chat_result(offset)[0] == "stopped"
                print(f"Ollama local cancellation {monotonic() - start:.2f}s", flush=True)
                unloaded = asyncio.create_task(wait_server(loaded, lambda models: not models))
                await scanned()
                print(f"Ollama /api/ps became empty {((await unloaded)[0]):.2f}s after local cleanup", flush=True)

            for name, check in (
                ("direct image, coordinates, export", direct),
                ("multi-page text PDF, dense page, small print, table cells", text_pdf),
                ("scanned PDF", scanned),
                ("verified blank response", blank),
                ("live bidirectional switching, Ollama history and cancellation", switches),
            ):
                try:
                    await check()
                except (AssertionError, TimeoutError) as error:
                    failures.append(name)
                    print("FAIL", name, str(error), flush=True)
                else:
                    print("PASS", name, flush=True)
        finally:
            await session.close()
    if failures:
        raise SystemExit("Failed checks: " + ", ".join(failures))


if __name__ == "__main__":
    with TemporaryDirectory(prefix="pageparse-live-") as folder:
        asyncio.run(main(Path(folder)))
