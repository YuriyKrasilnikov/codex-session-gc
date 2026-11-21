# Copyright (c) 2026
# ruff: noqa: D102, D107, PERF401, TC001, TC002
"""Read-only storage overview and physical history pages."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Select, Static

from codex_session_gc.cli.storage_diagnostic_screen import StorageDiagnosticScreen
from codex_session_gc.cli.storage_service import StorageService


class StorageScreen(Screen[None]):
    """Present one cached snapshot without affecting catalogue selection."""

    BINDINGS: ClassVar = [
        Binding("space", "ignore_space", show=False, priority=True),
        Binding("escape", "back", "Back", priority=True),
        Binding("q", "back", "Back", priority=True),
    ]

    CSS = """
    StorageScreen { layout: vertical; }
    #storage-summary { height: auto; max-height: 45%; overflow-y: auto; }
    #storage-objects { height: 1fr; }
    #storage-controls { height: 3; }
    """

    def __init__(self, service: StorageService) -> None:
        super().__init__()
        self.service = service
        self._observation: asyncio.Task[None] | None = None
        self.summary: dict[str, Any] | None = None
        self._page_offset = 0
        self.next_offset: int | None = None

    def compose(self) -> ComposeResult:
        yield Static("Reading storage…", id="storage-summary", markup=False)
        yield Select(
            [
                ("All", "all"),
                ("Without SQL", "missing"),
                ("Ambiguous", "ambiguous"),
                ("Unknown", "unknown"),
                ("With SQL", "matched"),
                ("Unparsed", "unparsed"),
            ],
            value="all",
            allow_blank=False,
            id="storage-filter",
        )
        yield DataTable(id="storage-objects", cursor_type="row")
        with Horizontal(id="storage-controls"):
            yield Button("Prev", id="storage-prev")
            yield Button("Next", id="storage-next")
            yield Button("Refresh", id="storage-refresh")
            yield Button("Problems", id="storage-problems")
            yield Button("Back", id="storage-back")

    def on_mount(self) -> None:
        table = self.query_one("#storage-objects", DataTable)
        for heading in ("Path", "Association", "Logical bytes", "Allocated bytes"):
            table.add_column(heading)
        self._observation = asyncio.create_task(self._load())

    async def _load(self, *, refresh: bool = False) -> None:
        try:
            result = await self.service.overview(refresh=refresh)
            self.summary = result
            self._page_offset = 0
            self._render_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            self.query_one("#storage-summary", Static).update(Text(f"Storage unavailable: {error}"))

    def _render_snapshot(self) -> None:
        if self.summary is None:
            return
        summary = self.summary

        def metric(value: dict[str, Any]) -> str:
            return f"{value['known_bytes']:,} bytes; {value['unknown_objects']:,} unknown objects"

        totals = summary["totals"]
        lines = [
            (
                f"Filesystem {summary['fs_coverage']}  "
                f"{summary.get('fs_started_at') or '—'} → {summary.get('fs_finished_at') or '—'}"
            ),
            (
                f"SQL {summary['sql_coverage']}  "
                f"{summary.get('sql_started_at') or '—'} → {summary.get('sql_finished_at') or '—'}"
            ),
            "Logical file bytes: " + metric(totals["logical_file_bytes"]),
            "Allocated bytes: " + metric(totals["allocated_bytes"]),
            "Categories (logical / allocated):",
        ]
        for category in summary["categories"]:
            lines.append(
                f"  {category['category']}: {metric(category['logical_file_bytes'])} / "
                f"{metric(category['allocated_bytes'])}; {category['unique_objects']:,} objects"
            )
        reasons = summary["counts_by_reason"]
        lines.append(
            "Gaps: " + (", ".join(f"{key}: {value}" for key, value in reasons.items()) or "none")
        )
        self.query_one("#storage-summary", Static).update(Text("\n".join(lines)))
        association = str(self.query_one("#storage-filter", Select).value)
        page = self.service.objects(
            summary["storage_snapshot_id"],
            association=association,
            offset=self._page_offset,
            limit=100,
        )
        self.next_offset = page["next_offset"]
        table = self.query_one("#storage-objects", DataTable)
        table.clear()
        for item in page["items"]:
            table.add_row(
                Text(str(item.get("display_path", ""))),
                Text(str(item.get("association", ""))),
                Text(str(item.get("logical_file_bytes", ""))),
                Text(str(item.get("allocated_bytes", ""))),
                key=item["object_id"],
            )

    def _open_diagnostics(self, subject_id: str | None) -> None:
        if self.summary is not None:
            self.app.push_screen(
                StorageDiagnosticScreen(
                    self.service, self.summary["storage_snapshot_id"], subject_id
                )
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Explain the selected physical name in its originating observation."""
        event.stop()
        if isinstance(event.row_key.value, str):
            self._open_diagnostics(event.row_key.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "storage-filter" and self.summary is not None:
            self._page_offset = 0
            self._render_snapshot()
        event.stop()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = event.button.id
        if action == "storage-back":
            await self.action_back()
        elif action == "storage-problems":
            self._open_diagnostics(None)
        elif action == "storage-refresh" and (
            self._observation is None or self._observation.done()
        ):
            self.query_one("#storage-summary", Static).update("Reading storage…")
            self._observation = asyncio.create_task(self._load(refresh=True))
        elif action == "storage-prev" and self.summary is not None:
            self._page_offset = max(0, self._page_offset - 100)
            self._render_snapshot()
        elif action == "storage-next" and self.next_offset is not None:
            self._page_offset = self.next_offset
            self._render_snapshot()

    def action_ignore_space(self) -> None:
        """Keep catalogue selection inaccessible on this screen."""

    async def action_back(self) -> None:
        """Return to the unchanged catalogue after joining the scan."""
        await self._stop()
        self.app.pop_screen()

    async def _stop(self) -> None:
        if self._observation is not None and not self._observation.done():
            self._observation.cancel()
            with suppress(asyncio.CancelledError):
                await self._observation
            await self.service.wait_idle()

    async def on_unmount(self) -> None:
        await self._stop()
