"""Sequential document inference and serializable results."""
import asyncio
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from time import monotonic

from .documents import PageImage, PdfRenderer, normalize_image, select_pages, source_type
from .models import PASS_MODES, ServiceError, run_sync
from .preview import render_tables

MAX_RESULT_BYTES = 32 * 1024 * 1024
MARKDOWN_FILTERS = ("Page-header", "Page-footer", "Footnote")


@dataclass(frozen=True)
class Progress:
    job_id: int
    state: str
    total: int
    completed: int
    active_page: int | None
    elapsed: float


def pretty_json(result):
    return json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def page_markdown(page, hidden):
    blocks = page["blocks"] if hidden else ()
    if not any(block["class"] in hidden for block in blocks):
        return page["markdown"]
    try:
        from nemotron_parse import render_markdown
    except ImportError:
        raise ServiceError("postprocess_failed", "Update the NVIDIA helper to use Markdown filters.") from None

    raw = []
    for block in blocks:
        if block["class"] not in hidden:
            x1, y1, x2, y2 = (format(Decimal(str(value)), "f") for value in block["bbox"])
            raw.append(f"<x_{x1}><y_{y1}>{block['text']}<x_{x2}><y_{y2}><class_{block['class']}>")
    return render_markdown("\n".join(raw))


def markdown_result(result, hidden=()):
    sections = [f"<!-- Page {page['page_number']} -->\n\n{render_tables(page_markdown(page, hidden)).rstrip()}"
                for page in result["pages"]]
    markdown = "\n\n---\n\n".join(sections)
    if result["status"] != "complete":
        status = result["status"]
        markdown = f"> Partial result · {status}.\n\n{markdown}"
    return markdown.rstrip() + "\n"


class DocumentJob:
    def __init__(self, job_id, source: Path | PageImage, pages, model, adapter, *,
                 report=lambda progress: None, renderer_factory=PdfRenderer,
                 result_budget=MAX_RESULT_BYTES, mode="single"):
        if mode not in PASS_MODES:
            raise ServiceError("invalid_mode", "Choose single, double, or triple pass.")
        self.job_id = job_id
        self.source = source
        self.range = pages
        self.adapter = adapter
        self.mode = mode
        self.report = report
        self.renderer_factory = renderer_factory
        self.result_budget = result_budget
        self.renderer = None
        self.request = None
        self.task = None
        self.state = "validating"
        self.active_page = None
        self.started = monotonic()
        self.ended = None
        is_path = isinstance(source, Path)
        self.result = {
            "schema_version": 1, "source_name": source.name if is_path else "clipboard-image.png",
            "source_type": "pdf" if is_path and source.suffix.lower() == ".pdf" else "image",
            "model": model, "pass_mode": mode, "status": None, "selected_pages": [],
            "pages": [], "failure": None,
        }

    def progress(self):
        return Progress(self.job_id, self.state, len(self.result["selected_pages"]),
                        len(self.result["pages"]), self.active_page, (self.ended or monotonic()) - self.started)

    def cancel(self):
        if self.state not in ("validating", "running"):
            return
        self.state = "cancelled"
        self.report(self.progress())
        if self.task:
            self.task.cancel()

    async def run(self):
        self.report(self.progress())
        if self.state != "cancelled":
            self.task = asyncio.create_task(self.process())
        try:
            if self.task:
                await self.task
                if self.state != "cancelled":
                    self.state = "complete"
        except asyncio.CancelledError:
            self.state = "cancelled"
        except ServiceError as error:
            if self.state != "cancelled":
                self.state = "failed"
                self.result["failure"] = {
                    "page_number": self.active_page, "code": error.code, "message": str(error),
                }
        finally:
            if self.renderer:
                await self.renderer.close()
            self.task = None
        self.ended = monotonic()
        self.result["status"] = self.state
        self.active_page = None
        self.report(self.progress())
        return self.result

    async def process(self):
        if isinstance(self.source, PageImage):
            direct_image = self.source
            self.source = None
            count = 1
        elif await run_sync(source_type, self.source) == "pdf":
            self.renderer = self.renderer_factory()
            count = await self.renderer.open(self.source)
            direct_image = None
        else:
            direct_image = await run_sync(normalize_image, self.source)
            count = 1
        pages = select_pages(self.range, count)
        self.result["selected_pages"] = pages
        self.state = "running"
        retained = 0
        for number in pages:
            if self.state == "cancelled":
                return
            self.active_page = number
            self.report(self.progress())
            image = await self.renderer.render(number) if self.renderer else direct_image
            if self.state == "cancelled":
                return
            self.request = asyncio.create_task(
                self.adapter.parse(image.png, image.width, image.height, mode=self.mode)
            )
            width, height = image.width, image.height
            del image, direct_image
            direct_image = None
            try:
                payload = await self.request
            finally:
                self.request = None
            if self.state == "cancelled":
                return
            page = {"page_number": number, "width_px": width, "height_px": height, **payload}
            size = await run_sync(lambda: len(json.dumps(
                page, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
            ).encode("utf-8")))
            if retained + size > self.result_budget:
                raise ServiceError("result_limit", "The result exceeds the 32 MiB retention limit.")
            retained += size
            self.result["pages"].append(page)
            self.active_page = None
            self.report(self.progress())
