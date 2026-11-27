# Copyright (c) 2026
"""Review exact accumulated selections without reading storage."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, DataTable, Static

from codex_session_gc.cli.summary_display import cwd_label, totals_label
from codex_session_gc.domain import ThreadId

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from codex_session_gc.inventory.selection import SelectionProjection


class SelectionScreen(Screen[ThreadId | None]):
    """Show every chosen ID and return only an explicit removal intent."""

    BINDINGS: ClassVar = [Binding("escape", "back", "Back", priority=True)]
    CSS = """
    SelectionScreen { layout: vertical; }
    #selection-table { height: 1fr; min-height: 0; }
    #selection-summary, #selection-context { height: 2; }
    #remove-selection, #selection-back { height: 3; }
    """

    def __init__(self, projection: SelectionProjection) -> None:
        """Bind the detached selection projection."""
        super().__init__()
        self.projection = projection
        self.rows = tuple(
            sorted(
                projection.rows,
                key=lambda pair: (
                    pair[1] is None,
                    pair[1].cwd if pair[1] else "",
                    str(pair[1].root_id) if pair[1] and pair[1].root_id else "",
                    str(pair[0]),
                ),
            )
        )

    def compose(self) -> ComposeResult:
        """Create the selection review controls."""
        yield Static("Select a row and press Remove to deselect it. Escape returns.")
        visibility = (
            "visibility unavailable (invalid or unspecified filter)"
            if self.projection.visible is None
            else f"{self.projection.visible} visible, {self.projection.hidden} hidden"
        )
        yield Static(
            f"{totals_label(self.projection.totals)}; {visibility}; "
            f"{self.projection.missing} unavailable (missing or unresolved work)",
            id="selection-summary",
            markup=False,
        )
        yield DataTable(id="selection-table", cursor_type="row")
        yield Static("", id="selection-context", markup=False)
        yield Button("Remove selected history", id="remove-selection")
        yield Button("Back", id="selection-back")

    def on_mount(self) -> None:
        """Display one row for each exact selected ID."""
        table = self.query_one(DataTable)
        for title in ("ID", "Type", "Directory", "Work root", "Parent", "Stored bytes", "Title"):
            table.add_column(title)
        for identifier, entry in self.rows:
            table.add_row(
                str(identifier.value),
                (
                    f"{entry.source.value} (unavailable)"
                    if identifier in self.projection.unavailable_ids and entry
                    else entry.source.value
                    if entry
                    else "unavailable"
                ),
                Text(cwd_label(entry.cwd, 40)[0] if entry else "unavailable"),
                str(entry.root_id.value) if entry and entry.root_id else "unavailable",
                str(entry.parent_id.value) if entry and entry.parent_id else "none / unavailable",
                str(entry.stored_bytes)
                if entry
                and entry.stored_bytes is not None
                and identifier not in self.projection.unavailable_ids
                else "unknown",
                Text(cwd_label(entry.title, 80)[0] if entry else "unavailable"),
                key=str(identifier.value),
            )
        table.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return the explicitly selected removal or close the view."""
        if event.button.id == "selection-back":
            self.dismiss(None)
        elif event.button.id == "remove-selection":
            table = self.query_one(DataTable)
            if table.row_count:
                key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                self.dismiss(ThreadId.parse(key))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Show the highlighted history's project and work context."""
        if event.cursor_row >= len(self.rows):
            return
        identifier, entry = self.rows[event.cursor_row]
        context = (
            "unavailable in current catalogue"
            if entry is None
            else (
                f"Directory: {cwd_label(entry.cwd, 500)[0]} | "
                f"Work: {entry.root_id or 'unavailable'} | Parent: {entry.parent_id or 'none'}"
            )
        )
        self.query_one("#selection-context", Static).update(Text(f"{identifier}: {context}"))

    def action_back(self) -> None:
        """Close without changing selection."""
        self.dismiss(None)
