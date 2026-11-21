# Copyright (c) 2026
# ruff: noqa: EM101, TRY003, TRY301, PLR2004

"""Grouped terminal browsing with exact advisory selection and durable execution."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, RichLog, Select, Static

from codex_session_gc.cli.console_display import (
    clean,
    discover_helper,
    preview_lines,
    receipt_path,
    result_text,
)
from codex_session_gc.cli.console_tasks import (
    Intent,
    PreviewMode,
    apply_intent,
    async_preview,
    confirmed_intent,
    parse_cutoff,
)
from codex_session_gc.cli.policy_preview import preview_policy as evaluate_policy
from codex_session_gc.cli.storage_screen import StorageScreen
from codex_session_gc.cli.storage_service import StorageService
from codex_session_gc.cli.timing_display import terminal as timing_terminal
from codex_session_gc.domain import ArchiveState, ExactIds, ThreadId, UtcCutoff
from codex_session_gc.execution.keep_command import KeepAction, change_keep
from codex_session_gc.execution.keep_store import read_kept_threads
from codex_session_gc.execution.progress import ProgressChannel, ProgressEvent, ProgressKind
from codex_session_gc.execution.timing import TimingObserver, measured_to_thread, observe
from codex_session_gc.inventory.catalogue import SessionCatalogue, read_session_catalogue
from codex_session_gc.inventory.query import (
    CatalogueGroup,
    CatalogueIndex,
    CatalogueQuery,
    CatalogueScope,
)
from codex_session_gc.inventory.selection_rule import SelectionRule, parse_rule_json

if TYPE_CHECKING:
    import argparse

    from codex_session_gc.inventory.catalogue import SessionEntry


def _size(value: int, unknown: int = 0) -> str:
    if not value and unknown:
        return "unknown"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    unit = 0
    radix = 1024
    amount = float(value)
    while amount >= radix and unit < len(units) - 1:
        amount /= radix
        unit += 1
    known = f"{value} B" if not unit else f"{amount:.2f} {units[unit]}"
    return f"≥{known} (total unknown)" if unknown else known


def _exact_size(value: int, unknown: int) -> str:
    return (
        f"≥{value:,} stored bytes; total unknown ({unknown} incomplete histories)"
        if unknown
        else f"{value:,} stored bytes"
    )


def _bound(text: str) -> int | None:
    if not text.strip():
        return None
    value = int(text.strip())
    if value < 0:
        raise ValueError("Size must be nonnegative stored bytes.")
    return value


def _directory(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("Choose a stored directory or all directories.")


def _archive(value: object) -> ArchiveState | None:
    if value is None:
        return None
    if isinstance(value, str):
        return ArchiveState(value)
    raise ValueError("Choose a stored archive state or all archive states.")


def _read_rule_file(path: Path, home: Path) -> SelectionRule:
    """Recheck the selected path before every rule evaluation."""
    resolved = path.expanduser().resolve(strict=True)
    if resolved.is_relative_to(home.resolve(strict=True)):
        raise ValueError("Rule file must be outside the Codex home.")
    return parse_rule_json(resolved.read_text(encoding="utf-8"))


class SessionApp(App[tuple[str, int]]):
    """Keep catalogue context separate from exact selected history identifiers."""

    TITLE = "Codex session storage"
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("space", "select_matches", "Select matches"),
        ("r", "reevaluate_rule", "Run rule again"),
        ("escape", "cancel_preview", "Cancel preview"),
        ("k", "mark_keep", "Keep history"),
        ("u", "clear_keep", "Clear Keep"),
    ]
    CSS = """
    Screen { layout: vertical; overflow: hidden hidden; }
    #content { height: 1fr; min-height: 0; }
    #heading, #notice { height: 2; }
    #progress { height: 4; display: none; }
    #root-results { height: 4; display: none; }
    .filters { height: 3; }
    .filters Input { width: 1fr; }
    .filters Select { width: 1fr; }
    #sessions { height: 1fr; min-height: 3; }
    #details-panel { height: auto; min-height: 3; }
    #details { height: auto; }
    #actions { height: 6; layout: grid; grid-size: 4 2; grid-columns: 1fr; }
    #actions Button { width: 100%; min-width: 8; }
    #review { height: auto; display: none; }
    #review-text { height: auto; }
    #confirm { height: 3; display: none; }
    #execute { display: none; }
    #child-ids, #choose-children, #source-preview { display: none; }
    .reviewing .filters, .reviewing #sessions, .reviewing #details-panel { display: none; }
    .reviewing #project-summary { display: none; }
    """

    def __init__(
        self,
        home: Path,
        cutoff: UtcCutoff | None,
        helper: Path | None,
        receipts: Path,
        rule_path: Path | None = None,
    ) -> None:
        """Bind the chosen directory identity for this interactive session."""
        super().__init__()
        self.home, self.cutoff, self.helper, self.receipts = home, cutoff, helper, receipts
        self.rule_path, self.rule_observation = rule_path, ""
        self.rule_ids: frozenset[ThreadId] | None = None
        self.rule_cutoff: UtcCutoff | None = None
        try:
            metadata = home.stat()
            self.startup_home: tuple[int, int] | None = (metadata.st_dev, metadata.st_ino)
        except OSError:
            self.startup_home = None
        self.storage = StorageService(home)
        self.index: CatalogueIndex | None = None
        self.catalogue: SessionCatalogue | None = None
        self.kept: frozenset[ThreadId] | None = None
        self.groups: tuple[CatalogueGroup, ...] = ()
        self.matching_histories: tuple[SessionEntry, ...] = ()
        self.order_target = "work"
        self.selected: dict[ThreadId, ThreadId] = {}
        self.expanded: set[ThreadId] = set()
        self.row_map: dict[str, tuple[CatalogueGroup, ThreadId | None]] = {}
        self.work_roots: dict[str, ThreadId] = {}
        self.preview_task: asyncio.Task[None] | None = None
        self.load_task: asyncio.Task[None] | None = None
        self.catalogue_teardown = False
        self.dispatch_task: asyncio.Task[None] | None = None
        self.dispatching = False
        self.receipt: Path | None = None
        self.guidance: dict[str, Any] | None = None
        self.guided_sources: tuple[ThreadId, ...] = ()
        self.guided_source_roots: dict[ThreadId, ThreadId] = {}
        self.guided_home: tuple[int, int] | None = None
        self.guided_cutoff: UtcCutoff | None = None
        self.guided_first = False
        self.guided_child_roots: dict[ThreadId, ThreadId] = {}
        self.guided_child_sources: dict[str, str] = {}
        self.guided_ready = False
        self.first_result = ""
        self.reviewed: Intent | None = None
        self.interrupt_requested = False
        self.review_open = False
        self.completed_result: tuple[str, int] | None = None
        self.valid_filters = True
        self.timing: TimingObserver | None = None
        self.progress: ProgressChannel | None = None
        self.progress_started = 0.0
        self.progress_operation = ""
        self.progress_current: ThreadId | None = None
        self.progress_results = 0
        self.progress_last: ProgressEvent | None = None

    def compose(self) -> ComposeResult:
        """Compose filters, context rows and explicit execution controls."""
        yield Static(
            "Loading metadata catalogue; histories are read only for explicit preview…",
            id="heading",
            markup=False,
        )
        with VerticalScroll(id="content"):
            with Horizontal(classes="filters"):
                yield Input(placeholder="Search full title / directory / ID", id="search")
                yield Input(
                    self.cutoff.timestamp.isoformat() if self.cutoff else "",
                    placeholder="Activity strictly before UTC",
                    id="before",
                )
                yield Select[str | None](
                    [(Text("All directories"), None)], value=None, allow_blank=False, id="cwd"
                )
                yield Select(
                    [
                        ("All histories", CatalogueScope.ALL),
                        ("Main histories", CatalogueScope.MAIN),
                        ("Agents (all)", CatalogueScope.AGENTS),
                        ("Ordinary agents", CatalogueScope.ORDINARY_AGENTS),
                        ("Guardians", CatalogueScope.GUARDIANS),
                    ],
                    value=CatalogueScope.ALL,
                    allow_blank=False,
                    id="scope",
                )
            with Horizontal(classes="filters"):
                yield Input(placeholder="Minimum stored bytes", id="minimum")
                yield Input(placeholder="Maximum stored bytes", id="maximum")
                yield Select[ArchiveState | None](
                    [
                        ("All archive states", None),
                        ("Archived", ArchiveState.ARCHIVED),
                        ("Not archived", ArchiveState.NOT_ARCHIVED),
                        ("Archive unknown", ArchiveState.UNKNOWN),
                    ],
                    value=None,
                    allow_blank=False,
                    id="archive",
                )
                yield Select(
                    [
                        ("Work total bytes", "total"),
                        ("Work own bytes", "own"),
                        ("Work agent bytes", "agents"),
                        ("Activity", "activity"),
                    ],
                    value="total",
                    allow_blank=False,
                    id="sort",
                )
                yield Select([("All works", "all")], value="all", allow_blank=False, id="work")
            with Horizontal(classes="filters"):
                yield Select(
                    [("Works", "work"), ("Histories", "history")],
                    value="work",
                    allow_blank=False,
                    id="order-target",
                )
                yield Select(
                    [("Descending", "desc"), ("Ascending", "asc")],
                    value="desc",
                    allow_blank=False,
                    id="direction",
                )
            yield DataTable(id="sessions", cursor_type="row")
            with Vertical(id="details-panel"):
                yield Static(
                    "Enter expands a work. Space selects matching histories; "
                    "exact bytes appear here.",
                    id="details",
                    markup=False,
                )
            yield Static("", id="progress", markup=False)
            yield RichLog(id="root-results", markup=False, highlight=False, wrap=True)
            with Vertical(id="review"):
                yield Static("", id="review-text", markup=False)
        yield Static(
            "No histories selected. Stored bytes are not guaranteed reclaimed space.",
            id="notice",
            markup=False,
        )
        yield Input(placeholder="Observed child IDs, comma separated", id="child-ids")
        yield Input(placeholder="Type literal DELETE after reviewing exact selection", id="confirm")
        with Horizontal(id="actions"):
            yield Button("Select matches", id="select")
            yield Button("Clear selection", id="clear")
            yield Button("Preview", id="preview", variant="primary")
            yield Button("Cancel / Back", id="cancel")
            yield Button("Execute DELETE", id="execute", variant="error")
            yield Button("Preview chosen children", id="choose-children")
            yield Button("Fresh source preview", id="source-preview")
            yield Button("Refresh", id="refresh")
            yield Button("Storage", id="storage")
            yield Button("Project summary", id="project-summary")
            yield Button("Quit", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        """Start asynchronous catalogue loading after the loading view appears."""
        table = self.query_one(DataTable)
        table.add_column("Work / history", width=24)
        for heading in ("Own bytes", "Agent bytes", "Total bytes"):
            table.add_column(heading, width=13)
        table.add_column("Matching / context")
        table.add_column("Activity UTC")
        self.load_task = asyncio.create_task(self._load())
        self.set_interval(0.1, self._check_interrupt)
        self.set_interval(0.1, self._show_progress)

    async def _check_interrupt(self) -> None:
        if self.interrupt_requested and not self.dispatching:
            self.interrupt_requested = False
            if isinstance(self.screen, StorageScreen):
                await self.screen.action_back()
            await self.action_quit()

    def _notice(self, text: str) -> None:
        self.query_one("#notice", Static).update(clean(text))

    def _show_progress(self) -> None:
        """Render known boundaries and local elapsed time while awaiting the result."""
        if not self.is_running:
            return
        if not self.dispatching or self.progress is None:
            if (
                self.timing is not None
                and self.preview_task is not None
                and not self.preview_task.done()
            ):
                snapshot = self.timing.snapshot()
                elapsed_ns = snapshot["caller_elapsed_ns"]
                elapsed = elapsed_ns / 1e9 if isinstance(elapsed_ns, int) else 0.0
                self.query_one("#progress", Static).update(
                    f"Waiting for preview worker: {elapsed:.1f}s; "
                    "worker stages unavailable until it returns"
                )
                self.query_one("#progress").display = True
            return
        for event in self.progress.drain():
            self.progress_operation = str(event.operation_id.value)
            if event.kind is ProgressKind.ROOT_STARTED:
                self.progress_current = event.root
            elif event.kind is ProgressKind.ROOT_RESULT:
                self.progress_current = None
                self.progress_results += 1
                self.progress_last = event
                if event.root is not None:
                    outcome = event.outcome.value if event.outcome is not None else "not_dispatched"
                    returned_roots = self.query_one("#root-results", RichLog)
                    returned_roots.display = True
                    returned_roots.write(f"{event.root.value}: {outcome}")
        elapsed = max(0.0, time.monotonic() - self.progress_started)
        current = str(self.progress_current.value) if self.progress_current else "none observed"
        last = self.progress_last
        returned = "none observed"
        if last is not None and last.root is not None:
            outcome = last.outcome.value if last.outcome is not None else "not_dispatched"
            returned = f"{last.root.value}: {outcome}"
        stage = self.timing.snapshot()["current_stage"] if self.timing is not None else None
        self.query_one("#progress", Static).update(
            f"Stage: {stage or 'waiting'}\n"
            f"Operation {self.progress_operation}\n"
            f"Waiting {elapsed:.1f}s; current root: {current}\n"
            f"Root results {self.progress_results}; last: {returned}"
        )

    def _catalogue_is_live(self) -> bool:
        return not self.catalogue_teardown and self.is_running

    async def _load(self) -> None:
        if not self._catalogue_is_live():
            return
        self.query_one("#heading", Static).update("Loading metadata catalogue…")
        try:
            catalogue = await asyncio.to_thread(read_session_catalogue, self.home)
            await self._observe_keep()
            if not self._catalogue_is_live():
                return
            self.catalogue, self.index = catalogue, CatalogueIndex(catalogue)
            all_groups = self.index.query(CatalogueQuery()).groups
            self.work_roots = {str(group.root.id.value): group.root.id for group in all_groups}
            options = [("All works", "all")]
            options.extend(
                (clean(group.root.title[:70]) or "Untitled", str(group.root.id.value))
                for group in all_groups
            )
            self.query_one("#work", Select).set_options(options)
            directory_select = self.query_one("#cwd", Select)
            previous_directory = directory_select.value
            directories = {entry.cwd for entry in catalogue.entries}
            if isinstance(previous_directory, str):
                directories.add(previous_directory)
            directory_options: list[tuple[Text, str | None]] = [(Text("All directories"), None)]
            directory_options.extend(
                (Text(clean(directory) or "(empty directory)"), directory)
                for directory in sorted(directories)
            )
            directory_select.set_options(directory_options)
            if isinstance(previous_directory, str):
                directory_select.value = previous_directory
            self.query_one("#heading", Static).update(
                f"{clean(str(self.home))} — {len(catalogue.entries):,} histories. "
                "Sizes: observed stored file bytes; unknowns retained."
            )
            self._filter()
            if self.rule_path is not None and self.rule_ids is None:
                await self._run_rule(fresh=True)
            if catalogue.diagnostics or catalogue.source_changed:
                self._notice(
                    "Catalogue warnings: "
                    + ", ".join(catalogue.diagnostics)
                    + ("; source changed during observation" if catalogue.source_changed else "")
                )
        except Exception as error:  # noqa: BLE001
            if self._catalogue_is_live():
                self._notice(f"Catalogue unavailable: {error}")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Apply editable filters without reading storage."""
        if event.input.id != "confirm":
            self._filter()

    def on_select_changed(self, event: Select.Changed) -> None:
        """Apply valid visible ordering fields and categorical filters to detached metadata."""
        if event.select.id == "order-target":
            target = self.query_one("#order-target", Select).value
            options = (
                [("History stored bytes", "stored_bytes"), ("Activity", "activity")]
                if target == "history"
                else [
                    ("Work total bytes", "total"),
                    ("Work own bytes", "own"),
                    ("Work agent bytes", "agents"),
                    ("Activity", "activity"),
                ]
            )
            field = self.query_one("#sort", Select)
            previous = field.value
            with self.prevent(Select.Changed):
                field.set_options(options)
                field.value = (
                    previous if previous in {value for _, value in options} else options[0][1]
                )
        self._filter()

    def _filter(self) -> None:
        if self.index is None or self.dispatching or self.review_open:
            return
        try:
            date = self.query_one("#before", Input).value.strip()
            before = parse_cutoff(date).timestamp if date else None
            self.cutoff = UtcCutoff(before) if before is not None else None
            work = self.query_one("#work", Select).value
            roots = (self.work_roots[str(work)],) if str(work) in self.work_roots else ()
            directory = _directory(self.query_one("#cwd", Select).value)
            archive = _archive(self.query_one("#archive", Select).value)
            query = CatalogueQuery(
                text=self.query_one("#search", Input).value,
                before=before,
                min_bytes=_bound(self.query_one("#minimum", Input).value),
                max_bytes=_bound(self.query_one("#maximum", Input).value),
                scope=cast("Any", self.query_one("#scope", Select).value),
                order_target=cast("Any", self.query_one("#order-target", Select).value),
                sort=cast("Any", self.query_one("#sort", Select).value),
                direction=cast("Any", self.query_one("#direction", Select).value),
                roots=roots,
                cwd=directory,
                archive_state=archive,
            )
            result = self.index.query(query)
            self.groups, self.matching_histories = result.groups, result.matching_histories
            self.order_target = query.order_target
            self.valid_filters = True
            self._render_rows()
        except ValueError as error:
            self.valid_filters = False
            self._notice(f"Invalid filter: {error}. Selection is unchanged; preview disabled.")

    def _render_rows(self) -> None:
        table = self.query_one(DataTable)
        cursor = table.cursor_row
        table.clear()
        self.row_map.clear()
        if self.order_target == "history":
            by_history = {
                identifier: group for group in self.groups for identifier in group.matching_ids
            }
            for entry in self.matching_histories:
                self._history_row(table, by_history[entry.id], entry, flat=True)
        for group in self.groups if self.order_target == "work" else ():
            key = "g:" + str(group.root.id.value)
            self.row_map[key] = group, None
            picked = sum(identifier in self.selected for identifier in group.matching_ids)
            caption = (
                f"{'▾' if group.root.id in self.expanded else '▸'} "
                f"[{picked}/{len(group.matching_ids)}] {group.root.title[:120] or 'Untitled'} "
                f"[{group.root.source.value}]"
            )
            if self.kept is not None and group.root.id in self.kept:
                caption += " [Keep]"
            table.add_row(
                Text(clean(caption)),
                _size(group.own_known_stored_bytes, group.own_incomplete),
                _size(group.agent_known_stored_bytes, group.agent_incomplete),
                _size(group.total_known_stored_bytes, group.incomplete_history_count),
                f"{len(group.matching_ids)}/{len(group.entries)}"
                + (" — unresolved" if not group.selectable else ""),
                str(group.last_activity or "unknown")
                + (" + unknown" if group.activity_incomplete else ""),
                key=key,
            )
            if group.root.id not in self.expanded:
                continue
            for entry in group.entries:
                self._history_row(table, group, entry)
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        self._notice(
            f"{len(self.groups):,} work groups; {len(self.selected):,} exact histories selected "
            "(selection persists across filters). "
            + ("Enter expands; " if self.order_target == "work" else "Globally ranked histories; ")
            + "Space toggles matches."
        )

    def _history_row(
        self,
        table: DataTable[Any],
        group: CatalogueGroup,
        entry: SessionEntry,
        *,
        flat: bool = False,
    ) -> None:
        key = "h:" + str(entry.id.value)
        self.row_map[key] = group, entry.id
        matches = entry.id in group.matching_ids
        caption = (
            f"  {'✓' if entry.id in self.selected else '○'} "
            f"{entry.title[:120] or 'Untitled'} [{entry.source.value}]"
        )
        if self.kept is not None and entry.id in self.kept:
            caption += " [Keep]"
        if flat:
            caption += f" — {group.root.title[:60] or 'Untitled work'} ({group.root.id.value})"
        table.add_row(
            Text(clean(caption), style="" if matches else "dim"),
            _size(entry.known_stored_bytes, int(entry.stored_bytes is None)),
            "—",
            "—",
            "match" if matches else "context only",
            str(entry.last_activity or "unknown"),
            key=key,
        )

    def _current(self) -> tuple[CatalogueGroup, ThreadId | None] | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return self.row_map.get(
            str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        )

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        """Expand contextual work without selecting its root."""
        current = self._current()
        if current and current[1] is None:
            root = current[0].root.id
            self.expanded.symmetric_difference_update({root})
            self._render_rows()

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        """Present bounded details for the highlighted history."""
        current = self._current()
        if current:
            group, identifier = current
            entry = next((item for item in group.entries if item.id == identifier), group.root)
            lines = [
                f"Work: {group.root.title[:120]} ({group.root.id.value})",
                "Work own: " + _exact_size(group.own_known_stored_bytes, group.own_incomplete),
                "Work agents: "
                + _exact_size(group.agent_known_stored_bytes, group.agent_incomplete),
                "Work total: "
                + _exact_size(group.total_known_stored_bytes, group.incomplete_history_count),
                "History: "
                + (
                    f"{entry.stored_bytes:,} bytes"
                    if entry.stored_bytes is not None
                    else f"≥{entry.known_stored_bytes:,} stored bytes; total unknown"
                ),
                f"Source: {entry.source.value}",
                (
                    "Keep status unavailable"
                    if self.kept is None
                    else "Keep is set"
                    if entry.id in self.kept
                    else "Keep is clear"
                ),
                f"Physical histories: {len(entry.physical_rollouts)}; formats: "
                + ", ".join(
                    sorted({item.representation.value for item in entry.physical_rollouts})
                ),
                (
                    f"Reported SQL history mode: {entry.reported_history_mode.value}; "
                    "physical lineage unknown"
                ),
                "Immutable rollout IDs: "
                + ", ".join(
                    str(item.ownership.rollout_id.value) for item in entry.physical_rollouts
                ),
                "Shared retained bytes and conditional reclaim: unknown until cleanup preview",
                entry.title[:2000],
                entry.cwd[:1000],
                (
                    f"{entry.id.value} — {entry.agent_status or 'lifecycle unknown'}; "
                    f"not proof of closure. {entry.issue or ''}"
                ),
            ]
            self.query_one("#details", Static).update("\n".join(clean(line) for line in lines))

    async def _observe_keep(self) -> None:
        try:
            self.kept = await asyncio.to_thread(read_kept_threads, self.home)
        except OSError, ValueError:
            self.kept = None

    async def action_mark_keep(self) -> None:
        """Set storage-wide protection for the exact highlighted history."""
        await self._change_keep(KeepAction.MARK)

    async def action_clear_keep(self) -> None:
        """Explicitly clear storage-wide protection for the highlighted history."""
        await self._change_keep(KeepAction.CLEAR)

    async def _change_keep(self, action: KeepAction) -> None:
        if self.review_open or self.dispatching or not isinstance(self.focused, DataTable):
            return
        current = self._current()
        if current is None or self.helper is None or self.startup_home is None:
            self._notice("Choose a history and a matched helper before changing Keep.")
            return
        group, identifier = current
        try:
            result = await asyncio.to_thread(
                change_keep,
                self.home,
                self.helper,
                identifier or group.root.id,
                action,
                expected_home=self.startup_home,
            )
        except (OSError, ValueError) as error:
            self._notice(f"Keep change refused: {error}")
            return
        await self._observe_keep()
        self._render_rows()
        self._notice(f"Keep {result['status']} — {result['detail']}")

    def _select(
        self, groups: tuple[CatalogueGroup, ...], identifier: ThreadId | None = None
    ) -> None:
        if not self.valid_filters or self.review_open or self.dispatching:
            return
        pairs = [
            (item, group.root.id)
            for group in groups
            if group.selectable
            for item in group.matching_ids
            if identifier is None or item == identifier
        ]
        if not pairs:
            self._notice(
                "No selectable matches. Context and unresolved histories are not selected."
            )
            return
        remove = all(item in self.selected for item, _root in pairs)
        for item, root in pairs:
            if remove:
                self.selected.pop(item, None)
            else:
                self.selected.setdefault(item, root)
        self._render_rows()

    def action_select_matches(self) -> None:
        """Toggle exact matches only when the history table has focus."""
        if isinstance(self.focused, DataTable):
            current = self._current()
            if current:
                self._select((current[0],), current[1])

    async def on_button_pressed(self, event: Button.Pressed) -> None:  # noqa: C901, PLR0912
        """Route explicit browsing and execution actions."""
        action = event.button.id
        if self.dispatching:
            self._notice(
                "Native execution in progress. Wait for outcome and durable "
                "receipt; quitting is disabled."
            )
            return
        if self.review_open and action not in {
            "cancel",
            "quit",
            "execute",
            "choose-children",
            "source-preview",
        }:
            return
        if action == "project-summary":
            if self.index is None or (self.load_task is not None and not self.load_task.done()):
                self._notice("Wait for the catalogue to finish loading.")
                return
            if not self.valid_filters:
                self._notice("Correct the filters before opening the project summary.")
                return
            from codex_session_gc.cli.project_summary_screen import (  # noqa: PLC0415
                ProjectSummaryScreen,
            )

            def drilldown(cwd: str | None) -> None:
                if cwd is not None:
                    self.query_one("#cwd", Select).value = cwd

            self.push_screen(ProjectSummaryScreen(self.matching_histories), drilldown)
        elif action == "storage":
            if self.load_task is not None and not self.load_task.done():
                self._notice("Wait for the catalogue to finish loading before opening Storage.")
            else:
                self.push_screen(StorageScreen(self.storage))
        elif action == "select":
            self._select(self.groups)
        elif action == "clear":
            self.selected.clear()
            self._render_rows()
        elif action == "refresh" and (self.load_task is None or self.load_task.done()):
            self.load_task = asyncio.create_task(self._load())
        elif action == "preview":
            self._start_preview()
        elif action == "cancel":
            await self.action_cancel_preview()
        elif action == "execute":
            self._confirm_dispatch()
        elif action == "choose-children":
            self._choose_children()
        elif action == "source-preview":
            await self._source_preview()
        elif action == "quit":
            await self.action_quit()

    def _choose_children(self) -> None:  # noqa: C901
        """Turn only explicitly entered observed children into an ordinary exact request."""
        try:
            guidance = self.guidance
            if not guidance or guidance.get("truncated"):
                raise ValueError("Complete attachment observation is required.")
            sources = guidance["sources"]
            source_ids = {ThreadId.parse(row["id"]) for row in sources}
            if not source_ids or not source_ids <= set(self.selected):
                raise ValueError("Select the displayed source histories for guided cleanup.")
            observed: dict[ThreadId, ThreadId] = {}
            ambiguous: set[ThreadId] = set()
            child_sources: dict[ThreadId, str] = {}
            for row in sources:
                for child in row["children"]:
                    try:
                        identifier = ThreadId.parse(child["id"])
                        root = ThreadId.parse(child["root_id"])
                    except ValueError, TypeError, KeyError:
                        continue
                    source_id = row["id"]
                    if identifier in observed and (
                        observed[identifier] != root or child_sources[identifier] != source_id
                    ):
                        ambiguous.add(identifier)
                    observed[identifier] = root
                    child_sources[identifier] = source_id
            for identifier in ambiguous:
                observed.pop(identifier, None)
            values = [part.strip() for part in self.query_one("#child-ids", Input).value.split(",")]
            chosen = tuple(ThreadId.parse(value) for value in values if value)
            if not chosen or len(set(chosen)) != len(chosen) or not set(chosen) <= observed.keys():
                raise ValueError("Enter distinct IDs from the observed children.")
            identity = guidance["home_identity"]
            if not isinstance(identity, list) or len(identity) != 2:
                raise ValueError("Home identity is unavailable.")
            stat = self.home.stat()
            if self.startup_home != (stat.st_dev, stat.st_ino):
                raise ValueError("Home changed since the console started.")
            if identity != [stat.st_dev, stat.st_ino]:
                raise ValueError("Home changed since observation.")
            self.guided_home = (stat.st_dev, stat.st_ino)
            self.guided_cutoff = self.cutoff
            self.guided_sources = tuple(sorted(source_ids))
            self.guided_source_roots = {
                ThreadId.parse(row["id"]): ThreadId.parse(row["root_id"]) for row in sources
            }
            self.guided_child_roots = {item: observed[item] for item in chosen}
            self.guided_child_sources = {str(item.value): child_sources[item] for item in chosen}
            self.guided_first = True
            self.guided_ready = False
            intent = Intent(
                self.home,
                self.cutoff or ExactIds(frozenset(chosen)),
                tuple(sorted({observed[item] for item in chosen})),
                tuple(sorted(chosen)),
            )
            self.reviewed = None
            self._review_mode(enabled=True)
            self.timing = TimingObserver("preview")
            self.preview_task = asyncio.create_task(self._preview(intent))
        except (ValueError, KeyError, TypeError, OSError) as error:
            self._notice(f"Cannot choose children: {error}")

    async def _source_preview(self) -> None:
        """Reobserve the original source and require a separate confirmation."""
        if not self.guided_ready or self.guided_home is None:
            return
        try:
            stat = self.home.stat()
            if (stat.st_dev, stat.st_ino) != self.guided_home:
                raise ValueError("Home changed since the child preview.")
            catalogue = await asyncio.to_thread(read_session_catalogue, self.home)
            if catalogue.source_changed:
                raise ValueError("Fresh catalogue is incomplete or changed.")
            groups = CatalogueIndex(catalogue).query(CatalogueQuery()).groups
            actual = {
                entry.id: group.root.id
                for group in groups
                if group.selectable
                for entry in group.entries
            }
            if any(
                actual.get(source) != self.guided_source_roots[source]
                for source in self.guided_sources
            ):
                raise ValueError("A source or its work is no longer resolved.")
            roots = tuple(sorted({actual[source] for source in self.guided_sources}))
            intent = Intent(
                self.home,
                self.guided_cutoff or ExactIds(frozenset(self.guided_sources)),
                roots,
                self.guided_sources,
            )
            self.guided_ready = False
            self._review_mode(enabled=True)
            self.timing = TimingObserver("preview")
            self.preview_task = asyncio.create_task(self._preview(intent))
        except Exception as error:  # noqa: BLE001
            self.guided_ready = False
            self._notice(f"Fresh source preview unavailable: {error}. Return to browsing.")

    def _matching_child_result(  # noqa: PLR0911
        self, intent: Intent, report: dict[str, Any], status: int
    ) -> bool:
        """Advance only for a complete confirmed result with exactly reviewed effects."""
        if (
            status != 0
            or report.get("outcome") != "confirmed"
            or report.get("lifecycleUnresolved") is not False
        ):
            return False
        declaration = intent.confirmation()
        if report.get("attemptId") != declaration.operation_id:
            return False
        expected = {target.thread_id for target in declaration.physical_manifest}
        paths = {
            rollout.rollout_path
            for target in declaration.physical_manifest
            for rollout in target.rollouts
        }
        rows = report.get("roots")
        if not isinstance(rows, list) or not rows:
            return False
        actual: set[str] = set()
        removed: set[str] = set()
        seen_roots: set[str] = set()
        for row in rows:
            native = row.get("nativeReceipt")
            if row.get("status") != "confirmed" or not isinstance(native, dict):
                return False
            selected = row.get("selectedThreads")
            effects = native.get("removed_rollouts")
            if not isinstance(selected, list) or not isinstance(effects, list):
                return False
            root = row.get("root")
            if not isinstance(root, str) or root in seen_roots:
                return False
            seen_roots.add(root)
            expected_ids = {
                str(child.value)
                for child, owner in self.guided_child_roots.items()
                if str(owner.value) == root
            }
            expected_paths = {
                rollout.rollout_path
                for target in declaration.physical_manifest
                if target.thread_id in expected_ids
                for rollout in target.rollouts
            }
            if not expected_ids or set(selected) != expected_ids or set(effects) != expected_paths:
                return False
            actual.update(selected)
            removed.update(effects)
        return (
            actual == expected == {str(item.value) for item in intent.selected}
            and removed == paths
            and seen_roots == {str(root.value) for root in intent.roots}
        )

    def _confirm_dispatch(self) -> None:
        if self.query_one("#confirm", Input).value == "DELETE" and self.reviewed is not None:
            self.dispatching = True
            self.dispatch_task = asyncio.create_task(self._dispatch(self.reviewed))
        else:
            self._notice("Review exact selection, then type literal DELETE.")

    def _review_mode(self, *, enabled: bool) -> None:
        self.review_open = enabled
        self.set_class(enabled, "reviewing")
        self.query_one("#review").display = enabled
        self.query_one("#confirm", Input).value = ""
        self.query_one("#confirm").display = False
        self.query_one("#execute").display = False
        self.query_one("#child-ids").display = False
        self.query_one("#choose-children").display = False
        self.query_one("#source-preview").display = False
        for identifier in ("select", "clear", "refresh", "preview", "storage"):
            self.query_one("#" + identifier, Button).disabled = enabled
        for widget in self.query(".filters Input, .filters Select"):
            widget.disabled = enabled

    async def action_reevaluate_rule(self) -> None:
        """Start a new review, allowing new matches at a fresh evaluation time."""
        if self.rule_path is None or self.review_open or self.dispatching:
            return
        await self._run_rule(fresh=True)

    async def _run_rule(self, *, fresh: bool) -> dict[str, Any] | None:
        if self.rule_path is None:
            return None
        try:
            metadata = self.home.stat()
            if (metadata.st_dev, metadata.st_ino) != self.startup_home:
                raise ValueError("Home changed since the console started.")
            rule = await asyncio.to_thread(_read_rule_file, self.rule_path, self.home)
            prior = None if fresh else frozenset(self.selected) & (self.rule_ids or frozenset())
            report = await asyncio.to_thread(
                evaluate_policy,
                self.home,
                rule,
                prior_ids=prior,
                prior_cutoff=None
                if fresh or self.rule_cutoff is None
                else self.rule_cutoff.timestamp,
            )
            if tuple(report["home_identity"]) != self.startup_home:
                raise ValueError("Home changed since the console started.")
            if report.get("unresolved_ids") or report.get("errors"):
                raise ValueError("Rule matches have unresolved ownership or preview errors.")
            roots = report["match_roots"]
            matches = {
                ThreadId.parse(value): ThreadId.parse(roots[value])
                for value in report["rule_matches"]
            }
            self.selected = matches
            self.rule_ids = frozenset(matches)
            self.rule_cutoff = parse_cutoff(report["cutoff"]) if report["cutoff"] else None
            self.cutoff = self.rule_cutoff
            self.query_one("#before", Input).value = report["cutoff"] or ""
            self.rule_observation = (
                f"Rule {self.rule_path}: {rule}; evaluated {report['evaluated_at']}; "
                f"cutoff {report['cutoff'] or 'none'}; removed {report['removed_ids']}"
            )
            self._render_rows()
            self._notice(self.rule_observation)
        except Exception as error:  # noqa: BLE001
            self._notice(f"Rule evaluation unavailable: {error}")
            return None
        else:
            return report

    async def _rule_review(self) -> None:
        self._review_mode(enabled=True)
        report = await self._run_rule(fresh=False)
        if report is None or not self.selected:
            self._review_mode(enabled=False)
            self._notice("Rule refresh produced no reviewable matches; no confirmation created.")
            return
        intent = Intent(
            self.home,
            self.rule_cutoff or ExactIds(frozenset(self.selected)),
            tuple(sorted(set(self.selected.values()))),
            tuple(sorted(self.selected)),
        )
        self.reviewed = None
        self.timing = TimingObserver("preview")
        self._review_mode(enabled=True)
        await self._preview(intent)

    def _start_preview(self) -> None:
        if self.review_open or self.dispatching:
            return
        if not self.selected or not self.valid_filters:
            self._notice("Select exact histories and use a valid UTC cutoff if supplied.")
            return
        try:
            metadata = self.home.stat()
        except OSError as error:
            self._notice(f"Cannot open the console home: {error}")
            return
        if (metadata.st_dev, metadata.st_ino) != self.startup_home:
            self._notice("Home changed since the console started. Reopen the console.")
            return
        if self.rule_path is not None:
            self.preview_task = asyncio.create_task(self._rule_review())
            return
        intent = Intent(
            self.home,
            self.cutoff or ExactIds(frozenset(self.selected)),
            tuple(sorted(set(self.selected.values()))),
            tuple(sorted(self.selected)),
        )
        self.reviewed = None
        self.timing = TimingObserver("preview")
        self._review_mode(enabled=True)
        self.query_one("#review-text", Static).update(
            "Reading scoped SQL and active/archive history headers for conditional preview… "
            "Esc cancels "
            "before dispatch and waits for the preview worker."
        )
        self.preview_task = asyncio.create_task(self._preview(intent))

    async def _preview(self, intent: Intent) -> None:  # noqa: C901, PLR0912
        report: dict[str, Any] | None = None
        try:
            assert self.timing is not None  # noqa: S101
            with observe(self.timing):
                report = await async_preview(intent, mode=PreviewMode.CONFIRM)
            self.timing.finish()
            labels = (
                {
                    str(entry.id.value): clean(f"{entry.title[:300]} | {entry.cwd[:300]}")
                    for entry in self.catalogue.entries
                }
                if self.catalogue
                else {}
            )
            report["labels"] = labels
            lines = preview_lines(report)
            if self.rule_path is not None:
                lines.insert(0, self.rule_observation)
            guidance = report.get("attachment_cleanup")
            if (
                isinstance(guidance, dict)
                and guidance.get("sources")
                and guidance.get("home_identity") != list(self.startup_home or ())
            ):
                raise ValueError("Home changed since the console started.")
            self.guidance = guidance if isinstance(guidance, dict) else None
            if self.guidance and self.guidance.get("sources") and not self.guided_first:
                lines.append("Observed attachment children (advisory; enter IDs deliberately):")
                for source in self.guidance["sources"]:
                    for child in source.get("children", []):
                        lines.append(
                            f"  source {source['id']} child {child['id']} work {child['root_id']}"
                        )
                if self.guidance.get("truncated"):
                    lines.append("Observation truncated; omitted children are unknown.")
                self.query_one("#child-ids").display = True
                self.query_one("#choose-children").display = True
            lines.extend(timing_terminal(self.timing.snapshot()).splitlines())
            self.query_one("#progress").display = False
            self.query_one("#review-text", Static).update("\n".join(clean(line) for line in lines))
            reviewed = confirmed_intent(intent, report)
            if reviewed.confirmation().home_identity != self.startup_home:
                raise ValueError("Home identity changed during preview.")
            if (
                self.guided_first
                and report.get("selected_attachments") != self.guided_child_sources
            ):
                raise ValueError("Child attachments changed or are unavailable; restart preview.")
            if self.guided_first and set(reviewed.selected) != set(intent.selected):
                raise ValueError("Child preview did not include every chosen child.")
            self.reviewed = reviewed
            self.query_one("#confirm").display = True
            self.query_one("#execute").display = True
            self._notice(
                "CONDITIONAL preview; not permission or proof of closed work. "
                "Exact deletion IDs are frozen. Type DELETE to dispatch once."
            )
        except asyncio.CancelledError:
            if self.timing is not None:
                self.timing.finish(interrupted=True)
            raise
        except Exception as error:  # noqa: BLE001
            if self.timing is not None:
                self.timing.finish(interrupted=True)
            self.query_one("#progress").display = False
            if report is None:
                self.query_one("#review-text", Static).update(
                    clean(f"Preview unavailable: {error}")
                )
            self._notice(f"Preview cannot execute: {error}")

    async def action_cancel_preview(self) -> None:
        """Reap an owned preview before returning to browsing."""
        if self.dispatching:
            self._notice("Execution cannot be cancelled; wait for receipt.")
            return
        if self.preview_task is not None and not self.preview_task.done():
            self.preview_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.preview_task
        previous_result = self.first_result
        self.first_result = ""
        self.reviewed = None
        self.guidance = None
        self.guided_sources = ()
        self.guided_source_roots = {}
        self.guided_home = None
        self.guided_cutoff = None
        self.guided_first = False
        self.guided_child_roots = {}
        self.guided_child_sources = {}
        self.guided_ready = False
        self.timing = None
        self.query_one("#progress").display = False
        self._review_mode(enabled=False)
        if previous_result:
            self.selected.clear()
            self.catalogue = None
            self.index = None
            self.groups = ()
            self.matching_histories = ()
            self.row_map.clear()
            self.query_one(DataTable).clear()
            results = self.query_one("#root-results", RichLog)
            results.clear()
            results.display = True
            results.write(previous_result)
            await self._load()
            self._notice("Child result and receipt retained above; catalogue refreshed.")
        else:
            self._notice("Preview closed before dispatch; no cleanup was requested.")

    async def _dispatch(self, intent: Intent) -> None:
        self.dispatching = True
        try:
            if self.helper is None:
                self._notice(
                    "No qualified helper found. Supply --helper with the supported binary."
                )
                return
            operation_id = intent.confirmation().operation_id
            self.receipt = receipt_path(self.receipts, self.home)
            self.timing = TimingObserver("apply")
            self.progress = ProgressChannel()
            self.progress_started = time.monotonic()
            self.progress_operation = operation_id
            self.progress_current, self.progress_last, self.progress_results = None, None, 0
            self.query_one("#progress").display = True
            self.query_one("#root-results", RichLog).clear()
            self.query_one("#root-results").display = False
            self._show_progress()
            self._notice(
                "Executing once. Do not close: awaiting native outcome and durable receipt…"
            )
            with observe(self.timing):
                backend = asyncio.create_task(
                    measured_to_thread(
                        apply_intent, intent, self.helper, self.receipt, progress=self.progress
                    )
                )
            while True:
                try:
                    report, status = await asyncio.shield(backend)
                    break
                except asyncio.CancelledError:
                    # Confirmation transferred ownership to the durable backend.
                    # Teardown must join it, not turn cancellation into a retry.
                    continue
            self._show_progress()
            self.timing.finish()
            result = result_text(report)
            if self.guided_first and self._matching_child_result(intent, report, status):
                self.first_result = f"{result}\nReceipt: {self.receipt}"
                self.guided_first = False
                self.guided_ready = True
                self.reviewed = None
                self.query_one("#review-text", Static).update(
                    "\n".join(clean(line) for line in self.first_result.splitlines())
                )
                self.query_one("#confirm").display = False
                self.query_one("#execute").display = False
                self.query_one("#source-preview").display = True
                self._notice("Children confirmed. Request a fresh source preview or cancel.")
                return
            self.completed_result = (
                (self.first_result + "\n" if self.first_result else "") + result,
                status,
            )
            self.exit(self.completed_result)
        except Exception as error:  # noqa: BLE001
            if self.timing is not None:
                self.timing.finish(interrupted=True)
            message = (
                f"UNKNOWN after confirmation: {error}. Keep receipt and reconcile; do not retry."
                if self.receipt
                else str(error)
            )
            self.completed_result = (
                (self.first_result + "\n" if self.first_result else "") + message,
                4 if self.receipt else 2,
            )
            self.exit(self.completed_result)
        finally:
            self.dispatching = False
            self.progress = None

    async def on_unmount(self) -> None:
        """Join owned work even if framework teardown bypasses ordinary quit actions."""
        self.catalogue_teardown = True
        await self.storage.close()
        if self.preview_task is not None and not self.preview_task.done():
            self.preview_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.preview_task
        if self.load_task is not None and not self.load_task.done():
            await asyncio.shield(self.load_task)
        if self.dispatch_task is not None and not self.dispatch_task.done():
            await asyncio.shield(self.dispatch_task)

    async def action_quit(self) -> None:
        """Prevent abandonment after dispatch and clean pre-dispatch work."""
        if self.dispatching:
            self._notice("Execution cannot be abandoned. Wait for outcome and receipt.")
            return
        previous_result = self.first_result
        await self.action_cancel_preview()
        if self.load_task is not None and not self.load_task.done():
            self._notice("Waiting for catalogue observation to finish before exit…")
            await self.load_task
        self.exit(
            (
                (previous_result + "\n" if previous_result else "") + "Stopped before dispatch.",
                0,
            )
        )


def run(arguments: argparse.Namespace) -> int:  # noqa: C901
    """Run Textual on a TTY; retain backend results and receipt after terminal teardown."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.stderr.write("ui requires an interactive terminal (TTY).\n")
        return 2
    home = (arguments.home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve()
    if not home.is_dir():
        sys.stderr.write("--home must name an existing directory.\n")
        return 2
    rule_path = getattr(arguments, "rule", None)
    if rule_path is not None:
        try:
            rule_path = rule_path.expanduser().resolve(strict=True)
            _read_rule_file(rule_path, home)
        except (OSError, ValueError) as error:
            sys.stderr.write(f"Invalid rule file: {error}\n")
            return 2
    state = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    app = SessionApp(
        home,
        arguments.cutoff,
        arguments.helper.expanduser().resolve() if arguments.helper else discover_helper(),
        arguments.receipt_dir or state / "codex-session-gc/receipts",
        rule_path,
    )
    previous = {
        number: signal.getsignal(number)
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }

    def interrupt(_number: int, _frame: object) -> None:
        if not app.dispatching:
            app.interrupt_requested = True

    for number in previous:
        signal.signal(number, interrupt)
    try:
        result = app.run()
        message, status = (
            result
            or app.completed_result
            or (
                ("UNKNOWN after confirmation; keep receipt and reconcile, do not retry.", 4)
                if app.receipt is not None
                else ("Console closed before dispatch.", 0)
            )
        )
    except Exception as error:  # noqa: BLE001
        message, status = (
            (f"UNKNOWN: {error}; keep receipt, reconcile, do not retry.", 4)
            if app.receipt
            else (f"Console error: {error}", 2)
        )
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    sys.stdout.write("\n".join(clean(line) for line in message.split("\n")) + "\n")
    if app.receipt is not None:
        sys.stdout.write(
            f"Receipt: {clean(str(app.receipt))}\n"
            "Keep it for reconcile; never retry UNKNOWN blindly.\n"
        )
    if app.completed_result is not None and app.timing is not None and app.timing.scope == "apply":
        sys.stdout.write(timing_terminal(app.timing.snapshot()) + "\n")
    return status
