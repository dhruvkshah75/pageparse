"""Document selection, parse progress, preview, and explicit export."""
import asyncio
from pathlib import Path

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Markdown, ProgressBar, Select, Static, TextArea

from .clipboard import read_clipboard_image
from .config import endpoint_label
from .documents import SUPPORTED_SUFFIXES
from .exports import FORMATS, ExportError
from .jobs import markdown_result, pretty_json
from .models import ServiceError, run_sync
from .preview import render_preview

SOURCE_HINT = "Path or Ctrl+V image"


class SourceTree(DirectoryTree):
    def filter_paths(self, paths):
        return [path for path in paths if not path.name.startswith(".")
                and (path.is_dir() or path.suffix.lower() in SUPPORTED_SUFFIXES)]


class ChooseSource(ModalScreen[Path | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Back")]

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static("Choose a document", classes="dialog-title")
            yield Static(str(Path.cwd()), id="browse-path", markup=False)
            yield SourceTree(Path.cwd(), id="source-tree")
            with Horizontal(classes="buttons"):
                yield Button("Parent folder", id="parent-folder")
                yield Button("Back", id="close-picker")

    def on_mount(self):
        self.query_one(SourceTree).focus()

    @on(DirectoryTree.FileSelected)
    def selected(self, event):
        self.dismiss(event.path)

    @on(Button.Pressed)
    def move(self, event):
        if event.button.id == "close-picker":
            self.dismiss(None)
        else:
            tree = self.query_one(SourceTree)
            tree.path = Path(tree.path).parent
            self.query_one("#browse-path", Static).update(str(tree.path))
            tree.focus()


class ParseView(Vertical):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.status = None
        self.discovery = None
        self.job = None
        self.parse_task = None
        self.result = None
        self.export_task = None
        self.default_destination = Path.cwd() / "outputs"
        self.clipboard_image = None
        self.clipboard_task = None
        self.source_revision = 0
        self.preview_revision = 0

    def compose(self):
        yield Static("Checking vLLM…", id="parse-connection", classes="connection", markup=False)
        with Horizontal(id="parse-layout"):
            with Vertical(id="parse-controls"):
                yield Static("Document", classes="section-title")
                with Horizontal(classes="field-row"):
                    yield Input(placeholder=SOURCE_HINT, id="source")
                    yield Button("Browse", id="browse")
                yield Input(placeholder="Pages · all, or e.g. 1,3-5", id="pages")
                with Horizontal(classes="field-row"):
                    yield Input(str(self.default_destination), placeholder="Output directory", id="destination")
                    yield Select(
                        [("Markdown + JSON", "both"), ("Markdown (.md)", "md"), ("JSON (.json)", "json")],
                        value="both", allow_blank=False, compact=True, id="export-format",
                        tooltip="Choose which files to save",
                    )
                with Horizontal(classes="buttons"):
                    yield Button("Start", id="start", variant="primary", disabled=True)
                    yield Button("Cancel", id="cancel", disabled=True)
                    yield Button("Export", id="save-export", disabled=True)
                yield ProgressBar(total=1, show_eta=False, show_percentage=False, id="parse-bar")
                yield Static("Idle · select a PDF or image", id="parse-progress", markup=False)
                yield Static("", id="parse-feedback", markup=False)
            with Vertical(id="preview-panel"):
                with Horizontal(id="preview-toolbar"):
                    yield Static("Preview", classes="section-title")
                    yield Select([("Rendered", "rendered"), ("Markdown", "markdown"), ("JSON", "json")],
                                 value="rendered", allow_blank=False, id="preview-mode")
                yield Static("Markdown uses pipe tables. JSON keeps the original HTML.", id="preview-hint", markup=False)
                with VerticalScroll(id="render-scroll"):
                    yield Markdown("Parsed pages will appear here.", open_links=False, id="rendered")
                yield TextArea("", read_only=True, soft_wrap=True, id="raw-preview")

    def on_mount(self):
        self.query_one("#raw-preview").display = False
        self.set_interval(.5, self.update_progress)

    def refresh_connection(self):
        if self.discovery is None or self.discovery.done():
            self.discovery = asyncio.create_task(self.discover())

    async def discover(self):
        settings = self.app.settings
        endpoint = endpoint_label(settings.vllm_url)
        self.query_one("#parse-connection", Static).update(f"{settings.nemotron_model} · {endpoint} · checking")
        self.update_connection(await self.app.nemotron.status())

    def update_connection(self, status):
        self.status = status
        settings = self.app.settings
        detail = f" · {status.message}" if status.state != "ready" else ""
        self.query_one("#parse-connection", Static).update(
            f"{settings.nemotron_model} · {status.endpoint} · {status.state}{detail}",
        )
        self.sync_actions()

    def sync_actions(self):
        source = self.query_one("#source", Input)
        ready = self.status is not None and self.status.state == "ready"
        self.query_one("#start", Button).disabled = (
            not ready or self.app.slot is not None or self.clipboard_task is not None
            or not (source.value.strip() or self.clipboard_image)
        )
        running = self.job is not None and self.app.slot == "parse"
        self.query_one("#cancel", Button).disabled = not running or self.job.state not in ("validating", "running")
        self.query_one("#save-export", Button).disabled = (
            not self.result or not self.result["pages"] or self.export_task is not None
            or not self.app.exports_view.available
        )

    @on(Input.Changed, "#source")
    def source_changed(self, event):
        self.source_revision += 1
        if event.value:
            self.clipboard_image = None
            event.input.placeholder = SOURCE_HINT
        if hasattr(self.app, "exports_view"):
            self.sync_actions()

    def paste_image(self, text_input=None):
        if self.clipboard_task is None:
            self.clipboard_task = asyncio.create_task(self.load_clipboard(self.source_revision, text_input))
            self.sync_actions()

    async def load_clipboard(self, revision, text_input):
        source = self.query_one("#source", Input)
        original_value = source.value
        try:
            image = await read_clipboard_image()
            if self.source_revision != revision or source.value != original_value:
                return
            self.clipboard_image = image
            with self.prevent(Input.Changed):
                source.value = ""
            source.placeholder = f"Clipboard image · {image.width}×{image.height}"
            self.query_one("#pages", Input).value = ""
            self.feedback("Clipboard image ready. Press F5 to parse.")
        except ServiceError as error:
            if self.source_revision != revision or source.value != original_value:
                return
            if error.code == "clipboard_unavailable" and text_input is source and self.app.clipboard:
                source.action_paste()
            else:
                self.feedback(str(error))
        finally:
            self.clipboard_task = None
            self.sync_actions()

    @staticmethod
    def path(value):
        value = value.strip()
        if not value:
            return None
        if len(value) > 1 and value[0] == value[-1] and value[0] in ("\"", "'"):
            value = value[1:-1]
        try:
            return Path(value).expanduser().absolute()
        except (OSError, RuntimeError, ValueError):
            return None

    @on(Button.Pressed, "#browse")
    def browse(self):
        def chosen(path):
            if path is not None:
                self.query_one("#source", Input).value = str(path)

        self.app.push_screen(ChooseSource(), chosen)

    @on(Button.Pressed, "#start")
    def start(self):
        self.sync_actions()
        if self.query_one("#start", Button).disabled:
            return
        value = self.query_one("#source", Input).value
        source = self.path(value) if value.strip() else self.clipboard_image
        if source is None:
            self.feedback("Enter a readable local file path.")
            return
        self.result = None
        self.app.sequence += 1
        self.job = self.app.job_factory(
            self.app.sequence, source, self.query_one("#pages", Input).value,
            self.app.settings.nemotron_model, self.app.nemotron, report=self.progress_event,
        )
        self.app.set_slot("parse")
        self.feedback("")
        self.refresh_preview()
        self.parse_task = asyncio.create_task(self.run_job(self.job))

    async def run_job(self, job):
        try:
            self.result = await job.run()
            if failure := self.result["failure"]:
                prefix = f"Page {failure['page_number']} · " if failure["page_number"] is not None else ""
                self.feedback(prefix + failure["message"])
            elif self.result["status"] == "cancelled":
                self.feedback("Cancelled locally. Completed pages remain in preview.")
            self.refresh_preview()
        finally:
            self.parse_task = None
            self.app.set_slot(None)

    @on(Button.Pressed, "#cancel")
    def cancel(self):
        if self.job and self.app.slot == "parse":
            self.job.cancel()
            self.sync_actions()

    def progress_event(self, progress):
        if self.job and progress.job_id == self.job.job_id:
            self.update_progress(progress)
            if progress.state == "running" and progress.active_page is None:
                self.refresh_preview()

    def update_progress(self, progress=None):
        if not self.job:
            return
        progress = progress or self.job.progress()
        page = f" · Page {progress.active_page}" if progress.active_page is not None else ""
        self.query_one("#parse-bar", ProgressBar).update(total=progress.total or 1, progress=progress.completed)
        self.query_one("#parse-progress", Static).update(
            f"{progress.state.capitalize()} · {progress.completed}/{progress.total} pages{page} · {progress.elapsed:.1f}s",
        )

    @on(Select.Changed, "#preview-mode")
    def change_mode(self):
        self.refresh_preview()

    def refresh_preview(self):
        mode = self.query_one("#preview-mode", Select).value
        raw = self.query_one("#raw-preview", TextArea)
        rendered = self.query_one("#render-scroll")
        rendered.display = mode == "rendered"
        raw.display = mode != "rendered"
        self.preview_revision += 1
        result = self.result if self.result is not None else self.job.result if self.job else None
        if not result or not result["pages"]:
            raw.load_text("")
            self.query_one("#rendered", Markdown).update("Parsed pages will appear here.")
            return
        snapshot = {**result, "pages": list(result["pages"]), "status": result["status"] or "complete"}
        asyncio.create_task(self.show_preview(mode, snapshot, self.preview_revision))

    async def show_preview(self, mode, snapshot, revision):
        if mode == "json":
            content = await run_sync(pretty_json, snapshot)
        elif mode == "rendered":
            content = await run_sync(render_preview, snapshot["pages"])
        else:
            content = await run_sync(markdown_result, snapshot)
        if revision != self.preview_revision:
            return
        if mode == "rendered":
            await self.query_one("#rendered", Markdown).update(content)
        else:
            self.query_one("#raw-preview", TextArea).load_text(content)

    def feedback(self, text):
        self.query_one("#parse-feedback", Static).update(text)

    @on(Button.Pressed, "#save-export")
    def export(self):
        if self.query_one("#save-export", Button).disabled:
            return
        value = self.query_one("#destination", Input).value
        destination = self.path(value) if value.strip() else self.default_destination
        if destination is None:
            self.feedback("Enter a valid output directory.")
            return
        choice = self.query_one("#export-format", Select).value
        formats = FORMATS if choice == "both" else (choice,)
        self.export_task = asyncio.create_task(self.publish(self.result, destination, formats))
        self.sync_actions()

    async def publish(self, result, destination, formats):
        try:
            if destination == self.default_destination:
                try:
                    await run_sync(lambda: destination.mkdir(mode=0o700, exist_ok=True))
                except OSError:
                    raise ExportError("Could not create the default output folder. Choose a writable directory.") from None
            proposal = await run_sync(self.app.store.plan, result, destination, formats)
            needs_confirmation = result["status"] != "complete"
            while True:
                if needs_confirmation or not proposal.trusted:
                    warning = ("\n\nThis directory is outside the cleanup boundary. "
                               "Staging files may require manual removal." if not proposal.trusted else "")
                    title = "Export partial result?" if result["status"] != "complete" else "Confirm output paths"
                    if not await self.app.confirm(title, "\n".join(map(str, proposal.paths)) + warning, "Export"):
                        self.feedback("Export cancelled.")
                        return
                publication = await run_sync(self.app.store.publish, result, proposal)
                if publication.status != "collision":
                    break
                self.feedback(publication.message)
                proposal = publication.proposal
                needs_confirmation = True
            details = [publication.message] if publication.message else []
            details.extend(str(path) for path in publication.paths)
            details.extend(f"Staging · {path}" for path in publication.staging)
            summary = "\n".join([publication.status.capitalize(), *details])
            self.feedback(summary)
            self.app.notify(summary, title="Export", timeout=8, markup=False)
            self.app.exports_view.refresh_index()
        except ExportError as error:
            self.feedback(str(error))
        finally:
            self.export_task = None
            self.sync_actions()
