import asyncio
import os
from multiprocessing.connection import Connection
from pathlib import Path
import signal
import sys
import time

import pytest

from pageparse.documents import PageImage, PdfRenderer
from pageparse.jobs import DocumentJob, markdown_result
from pageparse.models import ServiceError


class FixtureRenderer:
    async def open(self, path):
        self.closed = False
        return 4

    async def render(self, number):
        return PageImage(bytes([number]), 100 + number, 200)

    async def close(self):
        self.closed = True


class FixtureModel:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def parse(self, png, width, height):
        number = png[0]
        self.calls.append(number)
        assert width == 100 + number and height == 200
        if self.error and len(self.calls) == 2:
            raise ServiceError(self.error, "Second page failed.")
        return {"markdown": f"Page {number}", "coordinate_frame": "content_normalized",
                "blocks": [], "warnings": []}


def make_job(tmp_path, *, adapter=None, pages="3, 2, 2", renderer=FixtureRenderer, **kwargs):
    source = tmp_path / "source.pdf"
    source.touch()
    return DocumentJob(17, source, pages, "fixture", adapter or FixtureModel(),
                       renderer_factory=renderer, **kwargs)


@pytest.mark.parametrize("error", [None, "truncated", "timeout", "invalid_completion"])
def test_sequential_requests_and_partial_failure(tmp_path, error):
    model = FixtureModel(error)
    events = []
    job = make_job(tmp_path, adapter=model, report=events.append)
    result = asyncio.run(job.run())

    assert model.calls == [2, 3]
    assert job.renderer.closed and job.task is None
    assert all(event.job_id == 17 for event in events)
    assert result["selected_pages"] == [2, 3]
    assert result["status"] == ("failed" if error else "complete")
    assert [page["page_number"] for page in result["pages"]] == ([2] if error else [2, 3])
    if error:
        assert result["failure"] == {"page_number": 3, "code": error, "message": "Second page failed."}
        assert markdown_result(result).startswith("> Partial result")
    else:
        assert result["failure"] is None
        assert "<!-- Page 2 -->\n\nPage 2\n\n---\n\n<!-- Page 3 -->" in markdown_result(result)


def test_result_budget_and_invalid_range(tmp_path):
    job = make_job(tmp_path, result_budget=160)
    result = asyncio.run(job.run())
    assert result["failure"]["code"] == "result_limit"
    assert [page["page_number"] for page in result["pages"]] == [2]
    job = make_job(tmp_path, pages="7")
    result = asyncio.run(job.run())
    assert result["failure"]["code"] == "invalid_page_range"
    assert result["failure"]["page_number"] is None
    assert job.adapter.calls == []


def test_cancel_inference_ignores_late_results(tmp_path):
    async def verify():
        entered = asyncio.Event()
        request_closed = asyncio.Event()

        class LateModel(FixtureModel):
            async def parse(self, png, width, height):
                if png[0] == 3:
                    entered.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        await asyncio.sleep(0)
                        request_closed.set()
                return await super().parse(png, width, height)

        job = make_job(tmp_path, adapter=LateModel(), pages="2-4")
        task = asyncio.create_task(job.run())
        await asyncio.wait_for(entered.wait(), 3)
        job.cancel()
        result = await asyncio.wait_for(task, 3)
        assert request_closed.is_set() and job.renderer.closed
        assert result["status"] == "cancelled"
        assert result["failure"] is None
        assert result["selected_pages"] == [2, 3, 4]
        assert [page["page_number"] for page in result["pages"]] == [2]
        assert job.adapter.calls == [2, 3]

    asyncio.run(verify())


def child_exits(connection):
    connection.recv()
    connection.send(("metadata", 1))
    connection.recv()
    os._exit(8)


def child_ignores_termination(connection):
    connection.recv()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    connection.send(("metadata", 2))
    _, number = connection.recv()
    connection.send(("page", number, bytes([number]), 100 + number, 200))
    connection.recv()
    while True:
        time.sleep(1)


def test_unexpected_renderer_exit(tmp_path):
    job = make_job(tmp_path, pages="", renderer=lambda: worker_process(child_exits))
    result = asyncio.run(job.run())
    assert result["failure"]["code"] == "render_failed"
    assert result["failure"]["page_number"] == 1
    assert job.renderer.process is None


def test_cancel_render_reaps_unresponsive_child_before_return(tmp_path):
    async def verify():
        job = make_job(tmp_path, pages="", renderer=lambda: worker_process(child_ignores_termination))
        task = asyncio.create_task(job.run())
        async with asyncio.timeout(6):
            while job.active_page != 2:
                await asyncio.sleep(.01)
            pid = job.renderer.process.pid
            start = time.monotonic()
            job.cancel()
            result = await task
        assert 3 <= time.monotonic() - start < 5
        assert result["status"] == "cancelled"
        assert [page["page_number"] for page in result["pages"]] == [1]
        assert job.renderer.process is None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(verify())

def worker_process(target):
    return PdfRenderer((sys.executable, str(Path(__file__).resolve()), target.__name__))


if __name__ == "__main__":
    globals()[sys.argv[1]](Connection(int(sys.argv[2])))
