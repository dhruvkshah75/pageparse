"""Keyboard-selectable export groups and confirmed, format-specific deletion."""
import asyncio
from pathlib import Path

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, SelectionList, Static

from .exports import ExportError
from .models import run_sync


class ExportsView(Vertical):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.available = False
        self.index_task = None
        self.groups = {}

    def compose(self):
        yield Static("Opening export index…", id="index-status", classes="connection", markup=False)
        yield SelectionList[str](id="export-list")
        yield Static("", id="delete-feedback", markup=False)
        with Horizontal(classes="buttons"):
            yield Button("Delete Markdown", id="delete-md", disabled=True)
            yield Button("Delete JSON", id="delete-json", disabled=True)
            yield Button("Delete both", id="delete-both", variant="error", disabled=True)

    def refresh_index(self):
        if not self.index_task:
            self.index_task = asyncio.create_task(self.recover())

    async def recover(self):
        try:
            await run_sync(self.app.store.recover)
            await self.load_groups()
        except ExportError as error:
            self.available = False
            self.query_one("#index-status", Static).update(str(error))
        finally:
            self.index_task = None
            self.sync_actions()
            self.app.parse_view.sync_actions()

    async def load_groups(self):
        groups = await run_sync(self.app.store.list)
        self.groups = {group["id"]: group for group in groups}
        selection = self.query_one("#export-list", SelectionList)
        previous = set(selection.selected)
        highlight = selection.highlighted
        selection.clear_options()
        for group in groups:
            option = Text(group["source_name"], style="bold")
            option.append("  ·  " + group["state"], style="#a7b5d0")
            for file in group["files"]:
                size = f"  ·  {file['size']:,} B" if file["size"] is not None else ""
                option.append(f"\n{file['absolute_path']}{size}  ·  {file['status']}")
                if file["stage_state"] != "resolved":
                    option.append(f"\nStaging · {Path(group['parent']) / file['staging']}  ·  {file['stage_state']}")
            selection.add_option((option, group["id"], group["id"] in previous))
        if groups:
            selection.highlighted = min(highlight or 0, len(groups) - 1)
        self.available = True
        status = f"{len(groups)} export group(s) · Space to select" if groups else "No exports saved yet."
        self.query_one("#index-status", Static).update(status)

    @on(SelectionList.SelectedChanged)
    def sync_actions(self):
        for choice in ("md", "json", "both"):
            self.query_one(f"#delete-{choice}", Button).disabled = (
                not self.available or self.index_task is not None or not self.targets(choice)
            )

    def targets(self, choice):
        selected = self.query_one("#export-list", SelectionList).selected
        return [(group["id"], file) for key in selected if (group := self.groups.get(key))
                for file in group["files"] if choice == "both" or file["format"] == choice]

    @on(Button.Pressed, "#delete-md, #delete-json, #delete-both")
    def choose_deletion(self, event):
        targets = self.targets(event.button.id.removeprefix("delete-"))
        if not self.index_task and self.available and targets:
            self.index_task = asyncio.create_task(self.delete(targets))
            self.sync_actions()

    async def delete(self, targets):
        feedback = self.query_one("#delete-feedback", Static)
        try:
            known = sum(row["size"] or 0 for _, row in targets)
            unknown = sum(row["size"] is None for _, row in targets)
            summary = f"{known:,} known bytes" + (f" · {unknown} unknown size(s)" if unknown else "")
            paths = "\n".join(row["absolute_path"] for _, row in targets)
            if not await self.app.confirm("Permanently delete these files?", f"{paths}\n\n{summary}", "Delete"):
                feedback.update("Deletion cancelled.")
                return
            outcomes = await run_sync(
                self.app.store.delete, [(group_id, row["format"]) for group_id, row in targets],
            )
            message = "\n".join(f"{outcome.path} · {outcome.status}" for outcome in outcomes)
            feedback.update(message or "The selected records are already gone.")
            await self.load_groups()
        except ExportError as error:
            feedback.update(str(error))
        finally:
            self.index_task = None
            self.sync_actions()
