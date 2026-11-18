import asyncio
from io import BytesIO
import struct
import zlib

from PIL import Image
import pymupdf
import pytest

from pageparse.documents import PdfRenderer, normalize_image, select_pages, source_type
from pageparse.models import ServiceError


def sample_pdf(path):
    with pymupdf.open() as doc:
        text = doc.new_page(width=144, height=216)
        text.insert_text((15, 30), "Report")
        text.set_cropbox(pymupdf.Rect(0, 0, 100, 196))
        text.set_rotation(90)
        scanned = doc.new_page(width=100, height=150)
        raw = BytesIO()
        Image.new("RGB", (20, 20), "red").save(raw, "PNG")
        scanned.insert_image(pymupdf.Rect(0, 0, 100, 100), stream=raw.getvalue())
        doc.new_page(width=2200, height=200)
        doc.save(path)


def test_pdf_worker_rotated_cropped_scanned_text_wide(tmp_path):
    path = tmp_path / "sample.pdf"
    sample_pdf(path)
    assert source_type(path) == "pdf"

    async def render():
        renderer = PdfRenderer()
        try:
            assert await renderer.open(path) == 3
            results = [await renderer.render(number) for number in (1, 2, 3)]
        finally:
            await renderer.close()
        assert renderer.process is None
        return results

    rotated, scan, wide = asyncio.run(render())
    assert (rotated.width, rotated.height) == (409, 209)
    assert (scan.width, scan.height) == (209, 313)
    assert (wide.width, wide.height) == (1664, 152)
    with Image.open(BytesIO(scan.png)) as image:
        assert image.mode == "RGB"
        assert image.size == (scan.width, scan.height)
        assert image.getpixel((10, 10)) == (255, 0, 0)


@pytest.mark.parametrize("value", ["0", "2-1", "7", "1,,3", "a", "1-2-3", "-1", "1,"])
def test_invalid_page_ranges(value):
    with pytest.raises(ServiceError) as raised:
        select_pages(value, 6)
    assert raised.value.code == "invalid_page_range"


def test_page_range_sort_and_deduplication():
    assert select_pages("5, 2 - 4, 2, 1", 6) == [1, 2, 3, 4, 5]
    assert select_pages(" ", 3) == [1, 2, 3]
    assert select_pages("all", 3) == [1, 2, 3]
    assert select_pages(" ALL ", 3) == [1, 2, 3]


def test_exif_transparency_and_envelope(tmp_path):
    path = tmp_path / "image.jpeg"
    image = Image.new("RGB", (65, 30), "red")
    exif = image.getexif()
    exif[274] = 6
    image.save(path, exif=exif)
    assert (normalize_image(path).width, normalize_image(path).height) == (30, 65)

    path = tmp_path / "image.png"
    Image.new("RGBA", (2000, 1000), (255, 0, 0, 0)).save(path)
    result = normalize_image(path)
    with Image.open(BytesIO(result.png)) as image:
        assert image.size == (1664, 832)
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_invalid_images_and_header_only_limit(tmp_path):
    path = tmp_path / "image.png"
    frames = [Image.new("RGB", (5, 5), color) for color in ("red", "green")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=50, loop=0)
    with pytest.raises(ServiceError, match="still image"):
        normalize_image(path)
    path.write_bytes(b"invalid")
    with pytest.raises(ServiceError, match="could not be decoded"):
        normalize_image(path)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 7000, 6000, 8, 2, 0, 0, 0))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + header + chunk(b"IDAT", b"") + chunk(b"IEND", b""))
    with pytest.raises(ServiceError) as raised:
        normalize_image(path)
    assert raised.value.code == "source_limit"


def test_password_required_and_invalid_pdf(tmp_path):
    path = tmp_path / "locked.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")

    async def inspect(message):
        renderer = PdfRenderer()
        try:
            with pytest.raises(ServiceError, match=message):
                await renderer.open(path)
        finally:
            await renderer.close()

    asyncio.run(inspect("password"))
    path.write_bytes(b"invalid")
    asyncio.run(inspect("could not be opened"))
