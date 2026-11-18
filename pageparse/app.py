"""Textual entry point, shared inference slot, and connection discovery."""
import asyncio

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Footer, Input, Static, TabbedContent, TabPane

from .chat_view import ChatView
from .config import Settings, endpoint_label
from .exports import ExportStore
from .exports_view import ExportsView
from .jobs import DocumentJob
from .models import Discovery, Nemotron, Ollama
from .parse_view import ParseView


CHARCOAL = Theme(
    name="pageparse-charcoal",
    background="#232221",
    surface="#2C2A28",
    panel="#37332F",
    foreground="#F0E9DE",
    primary="#E3A58D",
    accent="#E3A58D",
    secondary="#C6BAAA",
    success="#B5CBA2",
    warning="#EBC18D",
    error="#E99590",
    variables={
        "text": "#F0E9DE",
        "text-muted": "#C6BAAA",
        "border-blurred": "#675E55",
        "block-cursor-foreground": "#232221",
        "button-color-foreground": "#232221",
        "input-selection-background": "#E3A58D 30%",
        "link-color": "#E3A58D",
    },
)


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
        Binding("ctrl+v", "paste_image(True)", "Paste image", show=False, priority=True),
        Binding("f8", "paste_image(False)", "Paste image", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, settings=None, *, nemotron=None, ollama=None, store=None,
                 job_factory=DocumentJob, initial_workspace="parse", starting_backends=()):
        super().__init__()
        self.register_theme(CHARCOAL)
        self.theme = CHARCOAL.name
        self.settings = settings or Settings.from_env()
        self.nemotron = nemotron or Nemotron(self.settings)
        self.ollama = ollama or Ollama(self.settings)
        self.store = store or ExportStore(self.settings.index)
        self.job_factory = job_factory
        self.initial_workspace = initial_workspace
        self.starting_backends = set(starting_backends)
        self.startup_task = None
        self.slot = None
        self.sequence = 0

    def compose(self) -> ComposeResult:
        yield Static(" ◆  PAGEPARSE", id="masthead", markup=False)
        with TabbedContent(initial=self.initial_workspace, id="workspaces"):
            with TabPane("Parse", id="parse"):
                yield ParseView(id="parse-view")
            with TabPane("Chat", id="chat"):
                yield ChatView(id="chat-view")
            with TabPane("Exports", id="exports"):
                yield ExportsView(id="exports-view")
        yield Footer()

    def on_mount(self):
        self.screen.set_class(self.size.width < 100, "narrow")
        self.parse_view = self.query_one(ParseView)
        self.chat_view = self.query_one(ChatView)
        self.exports_view = self.query_one(ExportsView)
        for name, view, url in (
            ("Nemotron", self.parse_view, self.settings.vllm_url),
            ("Ollama", self.chat_view, self.settings.ollama_url),
        ):
            if name in self.starting_backends:
                view.update_connection(Discovery("starting", endpoint_label(url), "Warming up in the background."))
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

    @on(TabbedContent.TabActivated, "#workspaces", pane="#exports")
    def refresh_exports_on_open(self):
        self.exports_view.refresh_visible()

    def active_workspace(self):
        return self.query_one("#workspaces", TabbedContent).active

    def action_refresh_services(self):
        if isinstance(self.screen, ModalScreen):
            return
        if "Nemotron" not in self.starting_backends:
            self.parse_view.refresh_connection()
        if "Ollama" not in self.starting_backends:
            self.chat_view.refresh_connection()
        self.exports_view.refresh_index()

    def backend_started(self, name, endpoint, error):
        self.starting_backends.discard(name)
        view = self.parse_view if name == "Nemotron" else self.chat_view
        if error:
            view.update_connection(Discovery("startup failed", endpoint, error))
        else:
            view.refresh_connection()

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

    def check_action(self, action, parameters):
        if action == "paste_image":
            if not hasattr(self, "parse_view") or isinstance(self.screen, ModalScreen) or self.active_workspace() != "parse":
                return False
            return not parameters[0] or not isinstance(self.focused, Input) or self.focused.id == "source"
        return super().check_action(action, parameters)

    def action_paste_image(self, use_text_fallback):
        self.parse_view.paste_image(self.focused if use_text_fallback else None)

    async def confirm(self, title, text, action):
        future = asyncio.get_running_loop().create_future()

        def finish(value):
            if not future.done():
                future.set_result(bool(value))

        self.push_screen(Confirm(title, text, action), finish)
        return await future

    async def action_quit(self):
        if self.startup_task:
            self.startup_task.cancel()
            await asyncio.gather(self.startup_task, return_exceptions=True)
        if hasattr(self, "parse_view"):
            while isinstance(self.screen, ModalScreen):
                self.screen.dismiss(None)
                await asyncio.sleep(0)
            self.parse_view.cancel()
            self.chat_view.stop()
            if self.parse_view.clipboard_task:
                self.parse_view.clipboard_task.cancel()
                await asyncio.gather(self.parse_view.clipboard_task, return_exceptions=True)
            tasks = (self.parse_view.parse_task, self.chat_view.turn_task, self.parse_view.export_task,
                     self.exports_view.index_task, self.parse_view.discovery, self.chat_view.discovery)
            await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)
        self.exit()
