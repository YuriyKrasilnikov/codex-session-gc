# Copyright (c) 2026

"""Read-only evidence from a single diagnostic generation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Select, Static

from codex_session_gc.cli.storage_display import diagnostic_evidence_line, diagnostic_subject_line
from codex_session_gc.inventory.storage import PAGE_LIMIT

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from codex_session_gc.cli.storage_service import StorageService
    from codex_session_gc.inventory.storage_diagnostics import Direction


class StorageDiagnosticScreen(Screen[None]):
    """View observed problems and their incoming and outgoing evidence."""

    BINDINGS: ClassVar = [
        Binding("space", "ignore_space", show=False, priority=True),
        Binding("escape", "back", "Back", priority=True),
        Binding("q", "back", "Back", priority=True),
    ]

    CSS = """
    #diagnostic-summary { height: auto; max-height: 35%; overflow-y: auto; }
    #diagnostic-rows { height: 1fr; }
    #diagnostic-controls { height: 3; }
    #diagnostic-controls Button { width: 1fr; min-width: 8; }
    """

    def __init__(self, service: StorageService, snapshot_id: str, subject_id: str | None) -> None:
        """Bind the screen to its originating storage generation and optional row."""
        super().__init__()
        self._service = service
        self._snapshot_id = snapshot_id
        self._subject_id = subject_id
        self._physical_id: str | None = None
        self._summary: dict[str, Any] | None = None
        self._providers = False
        self._problems_only = True
        self._scan: asyncio.Task[None] | None = None
        self._offset = 0
        self._next_offset: int | None = None
        self._neighbours: list[str | None] = []

    def compose(self) -> ComposeResult:
        """Lay out the evidence page and its controls."""
        yield Static("Reading diagnostic evidence…", id="diagnostic-summary", markup=False)
        yield Select(
            [("All evidence", "all"), ("Incoming", "incoming"), ("Outgoing", "outgoing")],
            value="all",
            allow_blank=False,
            id="diagnostic-direction",
        )
        yield DataTable(id="diagnostic-rows", cursor_type="row")
        with Horizontal(id="diagnostic-controls"):
            yield Button("Prev", id="diagnostic-prev", disabled=True)
            yield Button("Next", id="diagnostic-next", disabled=True)
            yield Button("Problems", id="diagnostic-problems")
            yield Button("Providers", id="diagnostic-providers")
            yield Button("Back", id="diagnostic-back")

    def on_mount(self) -> None:
        """Start diagnostic observation without blocking the terminal."""
        self.query_one(DataTable).add_column("Observation")
        self._scan = asyncio.create_task(self._load())

    async def _load(self) -> None:
        providers = self.query_one("#diagnostic-providers", Button)
        providers.disabled = True
        try:
            self._summary = await self._service.diagnostics(
                self._snapshot_id, providers=self._providers
            )
        except (OSError, ValueError) as error:
            self._show_error(error)
            return
        finally:
            providers.disabled = False
        self._render_page()

    def _show_error(self, error: Exception) -> None:
        self.query_one("#diagnostic-summary", Static).update(
            Text(f"Diagnostic evidence unavailable · {error}")
        )

    def _page(self, diagnostic_id: str) -> dict[str, Any]:
        if self._subject_id is None:
            return self._service.diagnostic_subjects(
                diagnostic_id,
                reason="problems" if self._problems_only else "all",
                offset=self._offset,
                limit=PAGE_LIMIT,
            )
        return self._service.explain(
            diagnostic_id,
            self._subject_id,
            direction=cast("Direction", self.query_one(Select).value),
            offset=self._offset,
            limit=PAGE_LIMIT,
        )

    def _render_page(self) -> None:
        if self._summary is None:
            return
        table = self.query_one(DataTable)
        table.clear()
        self._neighbours.clear()
        self.query_one(Select).disabled = self._subject_id is None
        try:
            page = self._page(self._summary["diagnostic_id"])
        except ValueError as error:
            self._show_error(error)
            self._next_offset = None
            self.query_one("#diagnostic-prev", Button).disabled = True
            self.query_one("#diagnostic-next", Button).disabled = True
            return
        coverage = " · ".join(
            f"{name} {state}" for name, state in self._summary["coverage"].items()
        )
        subject = page.get("subject")
        self._physical_id = subject["object_id"] if subject is not None else None
        heading = (
            diagnostic_subject_line(subject)
            if subject is not None
            else ("Observed problems" if self._problems_only else "Observed subjects")
        )
        self.query_one("#diagnostic-summary", Static).update(
            Text(f"{heading}\n{coverage}\n{page['total_matching']} observed")
        )
        self._next_offset = page["next_offset"]
        self.query_one("#diagnostic-prev", Button).disabled = self._offset == 0
        self.query_one("#diagnostic-next", Button).disabled = self._next_offset is None
        for item in page["items"]:
            if subject is None:
                table.add_row(Text(diagnostic_subject_line(item)), key=item["subject_id"])
            else:
                table.add_row(Text(diagnostic_evidence_line(item)))
                neighbour = (
                    item["target"] if item["source"] == subject["subject_id"] else item["source"]
                )
                self._neighbours.append(neighbour if neighbour != subject["subject_id"] else None)

    def on_select_changed(self, event: Select.Changed) -> None:
        """Re-page an evidence view after changing direction."""
        event.stop()
        self._offset = 0
        self._render_page()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the evidence page for the selected problem."""
        event.stop()
        selected = event.row_key.value if self._subject_id is None else None
        if self._subject_id is not None and 0 <= event.cursor_row < len(self._neighbours):
            selected = self._neighbours[event.cursor_row]
        if isinstance(selected, str):
            self._subject_id = selected
            self._offset = 0
            self._render_page()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Navigate pages or leave the evidence view."""
        event.stop()
        match event.button.id:
            case "diagnostic-back":
                await self.action_back()
            case "diagnostic-problems":
                self._subject_id = None
                self._problems_only = True
                self._offset = 0
                self._render_page()
            case "diagnostic-providers" if self._scan is None or self._scan.done():
                self._providers = not self._providers
                self._subject_id = self._physical_id
                self._problems_only = not self._providers
                self._summary = None
                self._offset = 0
                self._next_offset = None
                self.query_one(DataTable).clear()
                self.query_one("#diagnostic-summary", Static).update("Reading diagnostic evidence…")
                event.button.label = "SQL evidence" if self._providers else "Providers"
                self._scan = asyncio.create_task(self._load())
            case "diagnostic-prev" if self._offset:
                self._offset = max(0, self._offset - PAGE_LIMIT)
                self._render_page()
            case "diagnostic-next" if self._next_offset is not None:
                self._offset = self._next_offset
                self._render_page()

    def action_ignore_space(self) -> None:
        """Keep the underlying cleanup selection inaccessible."""

    async def action_back(self) -> None:
        """Return to storage when the observation is settled."""
        await self._stop()
        self.app.pop_screen()

    async def _stop(self) -> None:
        if self._scan is not None and not self._scan.done():
            self._scan.cancel()
            await asyncio.gather(self._scan, return_exceptions=True)
            await self._service.wait_idle()

    async def on_unmount(self) -> None:
        """Join the worker before discarding the screen."""
        await self._stop()
