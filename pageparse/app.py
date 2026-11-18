"""Textual entry point, shared inference slot, and connection discovery."""
import asyncio

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Static, TabbedContent, TabPane

from .chat_view import ChatView
from .config import Settings
from .exports import ExportStore
from .exports_view import ExportsView
from .jobs import DocumentJob
from .models import Nemotron, Ollama
from .parse_view import ParseView


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Back")]

    def __init__(self, title, text, action):
        super().__init__()
        self.heading = title
        self.text = text
        self.verb = action

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(self.heading, classes="dialog-title", markup=False)
            with VerticalScroll(classes="dialog-body"):
                yield Static(self.text, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button(self.verb, id="confirm", variant="error" if self.verb == "Delete" else "primary")

    @on(Button.Pressed)
    def choose(self, event):
        self.dismiss(event.button.id == "confirm")


class PageParse(App):
    TITLE = "PageParse"
    CSS_PATH = "pageparse.tcss"
    BINDINGS = [
        Binding("f1", "workspace('parse')", "Parse", priority=True),
        Binding("f2", "workspace('chat')", "Chat", priority=True),
        Binding("f3", "workspace('exports')", "Exports", priority=True),
        Binding("f4", "refresh_services", "Refresh", priority=True),
        Binding("f5", "primary", "Start / Send", priority=True),
        Binding("f6", "stop", "Cancel / Stop", priority=True),
        Binding("f7", "export", "Export", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, settings=None, *, nemotron=None, ollama=None, store=None, job_factory=DocumentJob):
        super().__init__()
        self.settings = settings or Settings.from_env()
        self.nemotron = nemotron or Nemotron(self.settings)
        self.ollama = ollama or Ollama(self.settings)
        self.store = store or ExportStore(self.settings.index)
        self.job_factory = job_factory
        self.slot = None
        self.sequence = 0

    def compose(self) -> ComposeResult:
        yield Static(" ◆  PAGEPARSE", id="masthead", markup=False)
        with TabbedContent(initial="parse", id="workspaces"):
            with TabPane("Parse", id="parse"):
                yield ParseView(id="parse-view")
            with TabPane("Chat", id="chat"):
                yield ChatView(id="chat-view")
            with TabPane("Exports", id="exports"):
                yield ExportsView(id="exports-view")
        yield Footer()

    def on_mount(self):
        self.theme = "textual-dark"
        self.screen.set_class(self.size.width < 100, "narrow")
        self.parse_view = self.query_one(ParseView)
        self.chat_view = self.query_one(ChatView)
        self.exports_view = self.query_one(ExportsView)
        self.action_refresh_services()

    def on_resize(self, event):
        self.screen_stack[0].set_class(event.size.width < 100, "narrow")

    def set_slot(self, owner):
        self.slot = owner
        self.screen_stack[0].query_one("#masthead", Static).update(
            " ◆  PAGEPARSE" + (f"   /   {owner.upper()} ACTIVE" if owner else ""),
        )
        self.parse_view.sync_actions()
        self.chat_view.sync_actions()

    def action_workspace(self, name):
        if not isinstance(self.screen, ModalScreen):
            workspaces = self.query_one("#workspaces", TabbedContent)
            workspaces.active = name
            target = {"parse": "#source", "chat": "#prompt", "exports": "#export-list"}[name]
            self.screen.set_focus(self.query_one(target))

    def active_workspace(self):
        return self.query_one("#workspaces", TabbedContent).active

    def action_refresh_services(self):
        if isinstance(self.screen, ModalScreen):
            return
        self.parse_view.refresh_connection()
        self.chat_view.refresh_connection()
        self.exports_view.refresh_index()

    def action_primary(self):
        if isinstance(self.screen, ModalScreen):
            return
        if self.active_workspace() == "parse":
            self.parse_view.start()
        elif self.active_workspace() == "chat":
            self.chat_view.send()

    def action_stop(self):
        if not isinstance(self.screen, ModalScreen):
            if self.slot == "parse":
                self.parse_view.cancel()
            elif self.slot == "chat":
                self.chat_view.stop()

    def action_export(self):
        if not isinstance(self.screen, ModalScreen) and self.active_workspace() == "parse":
            self.parse_view.export()

    async def confirm(self, title, text, action):
        future = asyncio.get_running_loop().create_future()

        def finish(value):
            if not future.done():
                future.set_result(bool(value))

        self.push_screen(Confirm(title, text, action), finish)
        return await future

    async def action_quit(self):
        if hasattr(self, "parse_view"):
            while isinstance(self.screen, ModalScreen):
                self.screen.dismiss(None)
                await asyncio.sleep(0)
            self.parse_view.cancel()
            self.chat_view.stop()
            tasks = (self.parse_view.parse_task, self.chat_view.turn_task, self.parse_view.export_task,
                     self.exports_view.index_task, self.parse_view.discovery, self.chat_view.discovery)
            await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
        self.exit()
