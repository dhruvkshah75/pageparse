"""Local source decoding and an isolated PDF rendering process."""
import asyncio
from dataclasses import dataclass
from io import BytesIO
import math
import multiprocessing
from pathlib import Path
import re
import stat

from PIL import Image, ImageOps

from .models import ServiceError

MAX_WIDTH = 1664
MAX_HEIGHT = 2048
MAX_SOURCE_PIXELS = 40_000_000
IMAGE_FORMATS = {"PNG", "JPEG", "WEBP"}
SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
_PAGE_ITEM = re.compile(r"([0-9]+)(?:\s*-\s*([0-9]+))?")


@dataclass(frozen=True)
class PageImage:
    png: bytes
    width: int
    height: int


def source_type(path: Path):
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise OSError
        with path.open("rb"):
            pass
    except (OSError, ValueError):
        raise ServiceError("invalid_source", "Select a readable local file.") from None
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ServiceError("invalid_source", "Select a PDF, PNG, JPEG, or WebP file.")
    return "pdf" if suffix == ".pdf" else "image"


def select_pages(spec: str, count: int):
    if not spec.strip():
        return list(range(1, count + 1))
    pages = set()
    for item in spec.split(","):
        match = _PAGE_ITEM.fullmatch(item.strip())
        try:
            if not match:
                raise ValueError
            start = int(match[1])
            end = int(match[2]) if match[2] else start
            if not 1 <= start <= end <= count:
                raise ValueError
        except ValueError:
            raise ServiceError(
                "invalid_page_range", f"Enter pages between 1 and {count}, using numbers and ranges such as 1,3-5.",
            ) from None
        pages.update(range(start, end + 1))
    return sorted(pages)


def normalize_image(path: Path):
    try:
        with Image.open(path, formats=sorted(IMAGE_FORMATS)) as source:
            expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
            if expected.get(path.suffix.lower()) != source.format:
                raise ValueError
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ServiceError("source_limit", "This image exceeds 40 million pixels. Resize it and retry.")
            if getattr(source, "is_animated", False):
                raise ServiceError("invalid_source", "Select a still image or extract a single animation frame.")
            image = ImageOps.exif_transpose(source)
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, "white")
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                image = flattened
            else:
                image = image.convert("RGB")
            image.thumbnail((MAX_WIDTH, MAX_HEIGHT), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            image.save(buffer, "PNG")
            return PageImage(buffer.getvalue(), image.width, image.height)
    except Image.DecompressionBombError:
        raise ServiceError("source_limit", "This image exceeds the safe pixel limit. Resize it and retry.") from None
    except (OSError, ValueError, SyntaxError):
        raise ServiceError("invalid_source", "The image could not be decoded. Check its file type or export a fresh copy.") from None


def pdf_worker(connection):
    """The child owns the document, request handling, and all PyMuPDF objects."""
    import pymupdf

    with connection:
        try:
            command, path = connection.recv()
            if command != "open":
                return
        except (EOFError, OSError):
            return
        try:
            document = pymupdf.open(path)
        except Exception:
            connection.send(("error", "invalid_source", "The PDF could not be opened. Check the file or export a fresh copy.", None))
            return
        with document:
            if not document.is_pdf or document.needs_pass or not document.page_count:
                if not document.is_pdf:
                    message = "The selected file is not a valid PDF."
                elif document.needs_pass:
                    message = "Select an unlocked copy of this password-protected PDF."
                else:
                    message = "This PDF has no pages. Select a PDF with readable pages."
                connection.send(("error", "invalid_source", message, None))
                return
            connection.send(("metadata", document.page_count))
            while True:
                try:
                    command, number = connection.recv()
                    if command == "shutdown":
                        return
                    if command != "render":
                        return
                    try:
                        page = document[number - 1]
                        width, height = page.rect.width, page.rect.height
                        if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                            raise ValueError
                        scale = min(150 / 72, MAX_WIDTH / width, MAX_HEIGHT / height)
                        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                                 colorspace=pymupdf.csRGB, alpha=False)
                        if not (0 < pixmap.width <= MAX_WIDTH and 0 < pixmap.height <= MAX_HEIGHT):
                            raise ValueError
                        image = PageImage(pixmap.tobytes("png"), pixmap.width, pixmap.height)
                    except Exception:
                        connection.send(("error", "render_failed", "The PDF page could not be rendered.", number))
                        return
                    connection.send(("page", number, image))
                    del image, pixmap, page
                except (EOFError, OSError):
                    return


class PdfRenderer:
    """One request at a time over a private, two-way multiprocessing pipe."""

    def __init__(self, target=pdf_worker):
        self.context = multiprocessing.get_context("spawn")
        self.target = target
        self.process = None
        self.connection = None
        self.exchange = None

    async def open(self, path: Path):
        parent, child = self.context.Pipe()
        process = self.context.Process(target=self.target, args=(child,), daemon=True)
        try:
            process.start()
        except (OSError, RuntimeError):
            parent.close()
            raise ServiceError("render_failed", "Could not start the PDF renderer.") from None
        finally:
            child.close()
        self.process = process
        self.connection = parent
        reply = await self.request(("open", str(path)), "metadata")
        return reply[1]

    async def render(self, number: int):
        reply = await self.request(("render", number), "page")
        if reply[1] != number:
            raise ServiceError("render_failed", "The PDF renderer returned the wrong page.")
        return reply[2]

    async def request(self, message, expected):
        def exchange():
            self.connection.send(message)
            return self.connection.recv()

        self.exchange = asyncio.create_task(asyncio.to_thread(exchange))
        try:
            reply = await asyncio.shield(self.exchange)
        except (EOFError, OSError):
            raise ServiceError("render_failed", "The PDF renderer exited unexpectedly.") from None
        finally:
            if self.exchange.done():
                self.exchange = None
        if reply[0] == "error":
            raise ServiceError(reply[1], reply[2])
        if reply[0] != expected:
            raise ServiceError("render_failed", "The PDF renderer returned an unexpected reply.")
        return reply

    async def close(self):
        if self.process is None:
            return
        await asyncio.to_thread(self.shutdown)
        if self.exchange is not None:
            await asyncio.gather(self.exchange, return_exceptions=True)
            self.exchange = None
        self.connection.close()
        self.process.close()
        self.process = None

    def shutdown(self):
        try:
            self.connection.send(("shutdown", None))
            # Keep draining a cancelled render as it exits so its PNG cannot fill the pipe.
        except (EOFError, OSError):
            pass
        self.process.join(2)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(1)
        if self.process.is_alive():
            self.process.kill()
            self.process.join()
