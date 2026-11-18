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
from textual.widgets import Input, TextArea

from pageparse.app import PageParse
from pageparse.config import Settings
from pageparse.models import Nemotron, Ollama

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


async def until(predicate, seconds=180):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.03)


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
    app = PageParse(settings, nemotron=nemotron, ollama=Ollama(settings))
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

        async with app.run_test(size=(120, 36)) as pilot:
            await until(lambda: app.parse_view.status and app.chat_view.status and app.exports_view.available, 10)
            assert app.parse_view.status.state == app.chat_view.status.state == "ready"

            async def start_parse(filename, pages=""):
                await pilot.press("f1")
                app.query_one("#source", Input).value = str(root / filename)
                app.query_one("#pages", Input).value = pages
                await pilot.press("f5")
                assert app.parse_view.job is not None

            async def parse(filename, pages=""):
                previous = len(nemotron.completions)
                await start_parse(filename, pages)
                await until(lambda: app.slot is None)
                result = app.parse_view.result
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
                await pilot.press("f7")
                await until(lambda: (root / "report.md").is_file() and (root / "report.json").is_file())

            async def text_pdf():
                result = await parse("text.pdf", "1-2")
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

            async def send(prompt):
                await pilot.press("f2")
                app.query_one("#prompt", TextArea).load_text(prompt)
                await pilot.press("f5")
                await until(lambda: bool(app.chat_view.turns) and app.chat_view.turns[-1].prompt == prompt, 10)
                return app.chat_view.turns[-1]

            async def switches():
                await start_parse("text.pdf", "2")
                await wait_server(running, lambda number: number > 0, 12)
                start = monotonic()
                await pilot.press("f6")
                await until(lambda: app.slot is None, 5)
                assert app.parse_view.result["status"] == "cancelled"
                assert app.parse_view.job.renderer.process is None
                print(f"Nemotron local cancellation and renderer reap {monotonic() - start:.2f}s", flush=True)
                idle = asyncio.create_task(wait_server(running, lambda number: number == 0))
                first = await send("Reply with exactly PARSE-27. Remember it for the next question.")
                await until(lambda: app.slot is None)
                assert first.state == "complete" and "PARSE-27" in first.reply, first.state
                print(f"vLLM running/waiting fell to zero {((await idle)[0]):.2f}s after local cleanup", flush=True)
                second = await send("Repeat the code from your previous answer. Give only the code.")
                await until(lambda: app.slot is None)
                assert second.state == "complete" and "PARSE-27" in second.reply, second.state
                print("Qwen prompt and context-dependent follow-up passed", flush=True)

                pending = await send("Output every number from 1 to 1000 separated by commas, without abbreviating.")
                await until(lambda: bool(pending.reply), 180)
                print("Ollama running model and VRAM before Stop", await loaded(), flush=True)
                start = monotonic()
                await pilot.press("f6")
                await until(lambda: app.slot is None, 5)
                assert pending.state == "stopped · incomplete"
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
    if failures:
        raise SystemExit("Failed checks: " + ", ".join(failures))


if __name__ == "__main__":
    with TemporaryDirectory(prefix="pageparse-live-") as folder:
        asyncio.run(main(Path(folder)))
