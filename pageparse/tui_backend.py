"""Private JSON-lines bridge between the Go TUI and the Python document engine."""

import asyncio
from contextlib import aclosing, suppress
from dataclasses import asdict
import json
from pathlib import Path
import sys
import traceback

from .clipboard import read_clipboard_image
from .config import Settings, endpoint_label
from .exports import ExportError, ExportStore, FORMATS
from .jobs import DocumentJob, markdown_result, pretty_json
from .launcher import Server, start_backends, stop_owned, stop_reused_nemotron
from .models import Nemotron, Ollama, ServiceError, run_sync
from .preview import render_preview

CHAT_LIMIT = 256 * 1024


def local_path(value):
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return Path(value).expanduser().absolute()


class BackendSession:
    def __init__(self, settings, mode, output):
        self.settings = settings
        self.emit = output
        self.nemotron = Nemotron(settings)
        self.ollama = Ollama(settings)
        self.store = ExportStore(settings.index)
        self.mode = mode
        self.owned = []
        self.reused = []
        self.starting = set()
        self.tasks = {}
        self.job = None
        self.result = None
        self.clipboard = None
        self.proposal = None
        self.default_destination = Path.cwd() / "outputs"

    def launch_task(self, group, action, request):
        if group in self.tasks:
            return

        async def run():
            try:
                await action(request)
            except (ServiceError, ExportError, OSError, ValueError, RuntimeError) as error:
                self.emit("error", scope=group, message=str(error))
            except asyncio.CancelledError:
                raise
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self.emit("error", scope=group, message="Unexpected backend error. Check PageParse's diagnostic log.")
            finally:
                self.tasks.pop(group, None)

        self.tasks[group] = asyncio.create_task(run())

    def begin(self):
        self.emit("session", model=self.settings.nemotron_model, destination=str(self.default_destination))
        servers = []
        if self.mode in ("image", "both"):
            s = self.settings
            servers.append(Server("Nemotron", s.vllm_url, "/v1", "/models",
                                  s.nemotron_model, s.api_key, s.nemotron_root))
        if self.mode in ("chat", "both"):
            servers.append(Server("Ollama", self.settings.ollama_url, "", "/api/tags"))

        self.starting = {server.name for server in servers}
        for server in servers:
            self.emit("connection", name=server.name, state="starting",
                      endpoint=endpoint_label(server.api_url), message="Warming up in the background.")

        async def start(_):
            def report(server, error):
                self.starting.discard(server.name)
                if error:
                    self.emit("connection", name=server.name, state="startup failed",
                              endpoint=endpoint_label(server.api_url), message=error)
                else:
                    self.launch_task(server.name, self.discover, {"name": server.name})

            await start_backends(servers, self.owned, self.reused, self.settings.index.parent / "logs", report)

        if servers:
            self.launch_task("startup", start, {})
        self.refresh()
        self.launch_task("index", self.index, {"recover": True})

    def refresh(self):
        for name in ("Nemotron", "Ollama"):
            if name not in self.starting:
                self.launch_task(name, self.discover, {"name": name})

    async def discover(self, request):
        name = request["name"]
        adapter = self.nemotron if name == "Nemotron" else self.ollama
        self.emit("connection", name=name, **asdict(await adapter.status()))

    async def paste(self, request):
        try:
            self.clipboard = await read_clipboard_image()
        except ServiceError as error:
            if error.code != "clipboard_unavailable" or not request.get("text_fallback"):
                raise
            self.emit("paste_text", revision=request["revision"])
            return
        self.emit("clipboard", revision=request["revision"],
                  width=self.clipboard.width, height=self.clipboard.height)

    async def parse(self, request):
        self.result = None
        self.job = None
        if request["clipboard"]:
            source = self.clipboard
            if source is None:
                raise ServiceError("invalid_source", "Copy an image before parsing.")
        else:
            source = local_path(request["source"])
        self.job = DocumentJob(
            request["id"], source, request["pages"], self.settings.nemotron_model, self.nemotron,
            mode=request.get("pass_mode", "single"),
            report=lambda progress: self.emit("progress", **asdict(progress)),
        )
        self.result = await self.job.run()
        self.emit("parse_done", status=self.result["status"], failure=self.result["failure"],
                  completed=len(self.result["pages"]))

    async def preview(self, request):
        result = self.result if self.result is not None else self.job.result if self.job else None
        content = ""
        if result and result["pages"]:
            snapshot = {**result, "pages": list(result["pages"]), "status": result["status"] or "complete"}
            mode = request["mode"]
            if mode == "rendered":
                content = await run_sync(render_preview, snapshot["pages"])
            else:
                content = await run_sync(pretty_json if mode == "json" else markdown_result, snapshot)
        self.emit("preview", revision=request["revision"], content=content)

    async def chat(self, request):
        room = CHAT_LIMIT - request["retained"]
        try:
            async with aclosing(self.ollama.chat(request["model"], request["messages"])) as stream:
                async for chunk in stream:
                    data = chunk.encode("utf-8")
                    if len(data) > room:
                        piece = data[:room].decode("utf-8", errors="ignore")
                        if piece:
                            self.emit("chunk", content=piece)
                        raise ServiceError("result_limit", "Transcript limit reached. Start a new chat.")
                    room -= len(data)
                    self.emit("chunk", content=chunk)
        except asyncio.CancelledError:
            self.emit("chat_done", status="stopped", message="Stopped locally.")
        except ServiceError as error:
            self.emit("chat_done", status="failed", message=str(error))
        else:
            self.emit("chat_done", status="complete")

    async def index(self, request):
        if request.get("recover"):
            await run_sync(self.store.recover)
        self.emit("index", groups=await run_sync(self.store.list))

    async def export(self, request):
        action = request["op"]
        if action == "export_cancel":
            self.proposal = None
            self.emit("export_done", message="Export cancelled.")
            return
        if action == "export":
            if not self.result:
                raise ExportError("Generate at least one page before exporting.")
            result = self.result
            destination = local_path(request["destination"]) if request["destination"].strip() else self.default_destination
            if destination == self.default_destination:
                await run_sync(lambda: destination.mkdir(mode=0o700, exist_ok=True))
            formats = FORMATS if request["format"] == "both" else (request["format"],)
            proposal = await run_sync(self.store.plan, result, destination, formats)
            if result["status"] != "complete" or not proposal.trusted:
                self.confirm_export(result, proposal)
                return
        elif self.proposal:
            result, proposal = self.proposal
        else:
            raise ExportError("This export proposal has expired. Try exporting again.")

        self.proposal = None
        publication = await run_sync(self.store.publish, result, proposal)
        if publication.status == "collision":
            self.confirm_export(result, publication.proposal, publication.message)
            return
        details = [publication.status.capitalize(), publication.message, *map(str, publication.paths),
                   *(f"Staging · {path}" for path in publication.staging)]
        self.emit("export_done", message="\n".join(filter(None, details)))
        self.launch_task("index", self.index, {})

    def confirm_export(self, result, proposal, message=""):
        self.proposal = result, proposal
        title = "Export partial result?" if result["status"] != "complete" else "Confirm output paths"
        details = [message, *map(str, proposal.paths)]
        if not proposal.trusted:
            details.append("This directory is outside the cleanup boundary. Staging files may require manual removal.")
        self.emit("export_confirm", title=title, details=list(filter(None, details)))

    async def delete(self, request):
        outcomes = await run_sync(self.store.delete, request["targets"])
        deleted = sum(outcome.status == "deleted" for outcome in outcomes)
        if outcomes:
            summary = f"Deleted {deleted} of {len(outcomes)} files."
            message = "\n".join([summary, *(f"{outcome.path} · {outcome.status}" for outcome in outcomes)])
        else:
            message = "The selected records are already gone."
        self.emit("delete_done", message=message)
        self.launch_task("index", self.index, {})

    def dispatch(self, request):
        operation = request["op"]
        if operation == "refresh":
            self.refresh()
            self.launch_task("index", self.index, {"recover": True})
        elif operation == "stop_parse" and self.job is not None:
            self.job.cancel()
        elif operation == "stop_chat" and (task := self.tasks.get("chat")):
            task.cancel()
        elif operation in ("parse", "chat"):
            if "parse" in self.tasks or "chat" in self.tasks:
                self.emit("error", scope=operation, message="Another inference request is already active.")
            else:
                self.launch_task(operation, getattr(self, operation), request)
        elif operation in ("export", "export_accept", "export_cancel"):
            self.launch_task("export", self.export, request)
        elif operation in ("paste", "preview", "index", "delete"):
            self.launch_task(operation, getattr(self, operation), request)

    async def close(self):
        pending = list(self.tasks.items())
        for name, task in pending:
            if name not in ("export", "delete", "index"):
                task.cancel()
        await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        await asyncio.to_thread(stop_owned, self.owned)
        for fd in self.reused:
            stop_reused_nemotron(fd)


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=True, allow_nan=False),
          file=sys.__stdout__, flush=True)


async def serve(mode):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2 * 1024 * 1024)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    session = BackendSession(Settings.from_env(), mode, emit)
    try:
        session.begin()
        while line := await reader.readline():
            session.dispatch(json.loads(line))
            # Let newly created commands enter their cleanup blocks before reading more.
            await asyncio.sleep(0)
    finally:
        transport.close()
        await session.close()


if __name__ == "__main__":
    sys.stdout = sys.stderr
    try:
        asyncio.run(serve(sys.argv[1]))
    except (OSError, ValueError) as error:
        with suppress(BrokenPipeError):
            emit("fatal", message=str(error))
