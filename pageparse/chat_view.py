"""In-memory Ollama chat with bounded streaming and completed-turn history."""
import asyncio
from contextlib import aclosing
from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, Select, Static, TextArea

from .config import endpoint_label
from .models import ServiceError

CHAT_LIMIT = 256 * 1024
PREFERRED_MODEL = "qwen2.5-coder:3b"


@dataclass
class Turn:
    prompt: str
    reply: str = ""
    state: str = "streaming"


class ChatView(Vertical):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.status = None
        self.discovery = None
        self.turn_task = None
        self.turns = []
        self.retained = 0
        self.model = None
        self.options = ()
        self.stop_requested = False

    def compose(self):
        yield Static("Checking Ollama…", id="chat-connection", classes="connection", markup=False)
        with Horizontal(id="chat-toolbar"):
            yield Label("Model", classes="section-title")
            yield Select([], prompt="Installed Ollama models", id="chat-model")
            yield Button("New chat", id="new-chat")
        with VerticalScroll(id="chat-scroll"):
            yield Static("Send a message to begin.", id="transcript", markup=False)
        yield Static("", id="chat-feedback", markup=False)
        yield TextArea("", id="prompt", soft_wrap=True, placeholder="Write a message")
        with Horizontal(classes="buttons"):
            yield Button("Send", id="send", variant="primary", disabled=True)
            yield Button("Stop", id="stop-chat", disabled=True)

    def refresh_connection(self):
        if self.discovery is None or self.discovery.done():
            self.discovery = asyncio.create_task(self.discover())

    async def discover(self):
        endpoint = endpoint_label(self.app.settings.ollama_url)
        self.query_one("#chat-connection", Static).update(f"Ollama · {endpoint} · checking")
        self.update_connection(await self.app.ollama.status())

    def update_connection(self, state):
        self.status = state
        detail = f" · {state.message}" if state.state != "ready" else ""
        self.query_one("#chat-connection", Static).update(f"Ollama · {state.endpoint} · {state.state}{detail}")
        if not self.turn_task:
            self.apply_models()
        self.sync_actions()

    def apply_models(self):
        if self.status is None or self.status.state == "offline" or self.status.models == self.options:
            return
        self.options = self.status.models
        select = self.query_one("#chat-model", Select)
        model = self.model if self.model in self.options else (
            PREFERRED_MODEL if PREFERRED_MODEL in self.options else next(iter(self.options), None)
        )
        with self.prevent(Select.Changed):
            select.set_options((name, name) for name in self.options)
            select.value = model if model is not None else Select.NULL
        self.use_model(model)

    def use_model(self, model):
        if model != self.model:
            self.model = model
            self.new_chat()

    @on(Select.Changed, "#chat-model")
    def model_changed(self, event):
        if not self.turn_task:
            self.use_model(None if event.value is Select.NULL else event.value)
        self.sync_actions()

    @on(TextArea.Changed, "#prompt")
    def prompt_changed(self):
        self.sync_actions()

    def sync_actions(self):
        self.query_one("#send", Button).disabled = (
            self.app.slot is not None or self.status is None or self.status.state != "ready"
            or self.model not in self.status.models or not self.query_one("#prompt", TextArea).text.strip()
        )
        self.query_one("#stop-chat", Button).disabled = self.turn_task is None or self.stop_requested
        self.query_one("#new-chat", Button).disabled = self.turn_task is not None
        self.query_one("#chat-model", Select).disabled = self.turn_task is not None or not self.options

    @on(Button.Pressed, "#new-chat")
    def new_chat(self):
        if self.turn_task:
            return
        self.turns.clear()
        self.retained = 0
        self.feedback("")
        self.refresh_transcript()

    @on(Button.Pressed, "#send")
    def send(self):
        self.sync_actions()
        if self.query_one("#send", Button).disabled:
            return
        composer = self.query_one("#prompt", TextArea)
        prompt = composer.text.strip()
        size = len(prompt.encode("utf-8"))
        if self.retained + size > CHAT_LIMIT:
            self.feedback("Transcript limit reached. Start a New chat or shorten the message.")
            return
        history = []
        for past in self.turns:
            if past.state == "complete":
                history.extend([{"role": "user", "content": past.prompt},
                                {"role": "assistant", "content": past.reply}])
        history.append({"role": "user", "content": prompt})
        turn = Turn(prompt)
        self.retained += size
        self.turns.append(turn)
        composer.clear()
        self.stop_requested = False
        self.feedback("Streaming…")
        self.refresh_transcript()
        self.turn_task = asyncio.create_task(self.run_turn(self.model, turn, history))
        self.app.set_slot("chat")

    async def run_turn(self, model, turn, history):
        try:
            if self.stop_requested:
                raise asyncio.CancelledError
            async with aclosing(self.app.ollama.chat(model, history)) as stream:
                async for chunk in stream:
                    if self.stop_requested:
                        raise asyncio.CancelledError
                    data = chunk.encode("utf-8")
                    room = CHAT_LIMIT - self.retained
                    if len(data) > room:
                        piece = data[:room].decode("utf-8", errors="ignore")
                        turn.reply += piece
                        self.retained += len(piece.encode("utf-8"))
                        raise ServiceError("result_limit", "Transcript limit reached. Start a New chat.")
                    turn.reply += chunk
                    self.retained += len(data)
                    self.refresh_transcript()
            if self.stop_requested:
                raise asyncio.CancelledError
            turn.state = "complete"
            self.feedback("")
        except asyncio.CancelledError:
            turn.state = "stopped · incomplete"
            self.feedback("Stopped locally.")
        except ServiceError as error:
            turn.state = "failed · incomplete"
            self.feedback(str(error))
        finally:
            self.turn_task = None
            self.stop_requested = False
            self.refresh_transcript()
            self.apply_models()
            self.app.set_slot(None)

    @on(Button.Pressed, "#stop-chat")
    def stop(self):
        if not self.turn_task or self.stop_requested:
            return
        self.stop_requested = True
        self.sync_actions()
        # Defer cancellation one loop turn so a just-created task enters its cleanup block.
        asyncio.get_running_loop().call_soon(self.cancel_active, self.turn_task)

    @staticmethod
    def cancel_active(task):
        if not task.done() and task.cancelling() == 0:
            task.cancel()

    def feedback(self, message):
        self.query_one("#chat-feedback", Static).update(message)

    def refresh_transcript(self):
        text = Text()
        for turn in self.turns:
            text.append("You\n", style="bold")
            text.append(turn.prompt + "\n\n")
            heading = "Assistant" if turn.state == "complete" else f"Assistant · {turn.state}"
            text.append(heading + "\n", style="bold")
            text.append(turn.reply + "\n\n")
        self.query_one("#transcript", Static).update(text or Text("Send a message to begin."))
        self.query_one("#chat-scroll").scroll_end(animate=False)
