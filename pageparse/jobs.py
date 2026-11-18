"""Sequential document inference and serializable results."""
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from time import monotonic

from .documents import PdfRenderer, normalize_image, select_pages, source_type
from .models import ServiceError, run_sync

MAX_RESULT_BYTES = 32 * 1024 * 1024


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


def markdown_result(result):
    sections = [f"<!-- Page {page['page_number']} -->\n\n{page['markdown'].rstrip()}"
                for page in result["pages"]]
    markdown = "\n\n---\n\n".join(sections)
    if result["status"] != "complete":
        status = result["status"]
        markdown = f"> Partial result · {status}.\n\n{markdown}"
    return markdown.rstrip() + "\n"


class DocumentJob:
    def __init__(self, job_id, source: Path, pages, model, adapter, *,
                 report=lambda progress: None, renderer_factory=PdfRenderer,
                 result_budget=MAX_RESULT_BYTES):
        self.job_id = job_id
        self.source = source
        self.range = pages
        self.adapter = adapter
        self.report = report
        self.renderer_factory = renderer_factory
        self.result_budget = result_budget
        self.renderer = None
        self.request = None
        self.task = None
        self.state = "validating"
        self.active_page = None
        self.started = monotonic()
        self.result = {
            "schema_version": 1, "source_name": source.name,
            "source_type": "pdf" if source.suffix.lower() == ".pdf" else "image",
            "model": model, "status": None, "selected_pages": [],
            "pages": [], "failure": None,
        }

    def progress(self):
        return Progress(self.job_id, self.state, len(self.result["selected_pages"]),
                        len(self.result["pages"]), self.active_page, monotonic() - self.started)

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
        self.result["status"] = self.state
        self.active_page = None
        self.report(self.progress())
        return self.result

    async def process(self):
        kind = await run_sync(source_type, self.source)
        if kind == "pdf":
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
            self.request = asyncio.create_task(self.adapter.parse(image.png, image.width, image.height))
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
