# Copyright (c) 2026
# ruff: noqa: D102, D107, TC001, TC002
"""Read-only project summaries for the current matching histories."""

from __future__ import annotations

from typing import ClassVar, Literal, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Select, Static

from codex_session_gc.cli.summary_display import cwd_label, totals_label
from codex_session_gc.inventory.catalogue import SessionEntry
from codex_session_gc.inventory.query import Breakdown, Summary, summarize_catalogue


def _details(b: Breakdown) -> str:
    return " | ".join(
        f"{name}: {totals_label(getattr(b, name))}"
        for name in (
            "total",
            "main",
            "agent",
            "guardian",
            "unknown",
            "closed_agents",
            "active_or_open_agents",
            "unknown_status_agents",
        )
    )


class ProjectSummaryScreen(Screen[str | None]):
    """Inspect and drill down into an exact stored cwd without changing selection."""

    BINDINGS: ClassVar = [Binding("escape", "back", "Back", priority=True)]

    def __init__(self, histories: tuple[SessionEntry, ...]) -> None:
        super().__init__()
        self.histories = histories
        self.paths: list[str] = []
        self.summary: Summary | None = None

    def compose(self) -> ComposeResult:
        yield Select(
            [
                ("All stored bytes", "stored_bytes"),
                ("Closed ordinary agent bytes", "closed_agent_bytes"),
            ],
            value="stored_bytes",
            allow_blank=False,
            id="project-rank",
        )
        yield Static("", id="project-overall", markup=False)
        yield DataTable(id="project-table", cursor_type="row")
        yield Static("", id="project-detail", markup=False)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for column in (
            "Project (stored cwd)",
            "Total",
            "Main",
            "Agent",
            "Guardian",
            "Unknown",
            "Closed",
        ):
            table.add_column(column)
        self._render_summary()
        table.focus()

    def _render_summary(self) -> None:
        rank = cast(
            'Literal["stored_bytes", "closed_agent_bytes"]',
            self.query_one("#project-rank", Select).value,
        )
        summary = summarize_catalogue(self.histories, rank)
        self.summary = summary
        self.query_one("#project-overall", Static).update(
            Text("Overall\n" + _details(summary.overall))
        )
        table = self.query_one(DataTable)
        cursor = table.cursor_row
        table.clear()
        self.paths = [row.cwd for row in summary.projects]
        for ordinal, row in enumerate(summary.projects):
            b = row.breakdown
            label, truncated = cwd_label(row.cwd, 30)
            table.add_row(
                Text(f"{ordinal + 1}: {label}{'…' if truncated else ''}"),
                *(
                    str(x.known_stored_bytes) + ("+" if x.stored_bytes is None else "")
                    for x in (b.total, b.main, b.agent, b.guardian, b.unknown, b.closed_agents)
                ),
            )
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
            self.query_one("#project-detail", Static).update(
                Text(_details(summary.projects[table.cursor_row].breakdown))
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "project-rank" and self.is_mounted:
            self._render_summary()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        summary = self.summary
        if summary is None:
            return
        if event.cursor_row < len(summary.projects):
            self.query_one("#project-detail", Static).update(
                Text(_details(summary.projects[event.cursor_row].breakdown))
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row < len(self.paths):
            self.dismiss(self.paths[event.cursor_row])

    def action_back(self) -> None:
        self.dismiss(None)
