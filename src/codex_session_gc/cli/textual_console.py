# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Grouped terminal browsing with exact advisory selection and durable execution."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, Select, Static

from codex_session_gc.cli.console_display import (
    clean,
    discover_helper,
    preview_lines,
    receipt_path,
    result_text,
)
from codex_session_gc.cli.console_tasks import (
    Intent,
    apply_intent,
    async_preview,
    confirmed_intent,
    parse_cutoff,
)
from codex_session_gc.domain import ThreadId, UtcCutoff
from codex_session_gc.inventory.catalogue import SessionCatalogue, read_session_catalogue
from codex_session_gc.inventory.query import CatalogueGroup, CatalogueIndex, CatalogueQuery

if TYPE_CHECKING:
    import argparse


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


class SessionApp(App[tuple[str, int]]):
    """Keep catalogue context separate from exact selected history identifiers."""

    TITLE = "Codex session storage"
    BINDINGS: ClassVar = [
        ("q", "quit", "Quit"),
        ("space", "select_matches", "Select matches"),
        ("escape", "cancel_preview", "Cancel preview"),
    ]
    CSS = """
    Screen { layout: vertical; }
    #heading, #notice { height: 2; }
    .filters { height: 3; }
    .filters Input { width: 1fr; }
    .filters Select { width: 1fr; }
    #sessions { height: 1fr; min-height: 3; }
    #details-panel { height: 3; }
    #details { height: auto; }
    #actions { height: 6; layout: grid; grid-size: 4 2; grid-columns: 1fr; }
    #actions Button { width: 100%; min-width: 8; }
    #review { height: 1fr; display: none; }
    #review-text { height: auto; }
    #confirm { height: 3; display: none; }
    #execute { display: none; }
    .reviewing .filters, .reviewing #sessions, .reviewing #details-panel,
    .reviewing #heading { display: none; }
    """

    def __init__(self, home: Path, cutoff: UtcCutoff, helper: Path | None, receipts: Path) -> None:
        """Bind one home and frozen selection without storage I/O."""
        super().__init__()
        self.home, self.cutoff, self.helper, self.receipts = home, cutoff, helper, receipts
        self.index: CatalogueIndex | None = None
        self.catalogue: SessionCatalogue | None = None
        self.groups: tuple[CatalogueGroup, ...] = ()
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
        self.reviewed: Intent | None = None
        self.interrupt_requested = False
        self.review_open = False
        self.completed_result: tuple[str, int] | None = None
        self.valid_filters = True

    def compose(self) -> ComposeResult:
        """Compose filters, context rows and explicit execution controls."""
        yield Static(
            "Loading metadata catalogue; histories are read only for explicit preview…",
            id="heading",
            markup=False,
        )
        with Horizontal(classes="filters"):
            yield Input(placeholder="Search full title / directory / ID", id="search")
            yield Input(
                self.cutoff.timestamp.isoformat(),
                placeholder="Activity strictly before UTC",
                id="before",
            )
            yield Select(
                [("All histories", "all"), ("Main histories", "main"), ("Agents", "agents")],
                value="all",
                allow_blank=False,
                id="scope",
            )
        with Horizontal(classes="filters"):
            yield Input(placeholder="Minimum stored bytes", id="minimum")
            yield Input(placeholder="Maximum stored bytes", id="maximum")
            yield Select(
                [
                    ("Largest total", "total"),
                    ("Largest own", "own"),
                    ("Largest agents", "agents"),
                    ("Latest activity", "activity"),
                ],
                value="total",
                allow_blank=False,
                id="sort",
            )
            yield Select([("All works", "all")], value="all", allow_blank=False, id="work")
        yield DataTable(id="sessions", cursor_type="row")
        with VerticalScroll(id="details-panel"):
            yield Static(
                "Enter expands a work. Space selects matching histories; exact bytes appear here.",
                id="details",
                markup=False,
            )
        yield Static(
            "No histories selected. Stored bytes are not guaranteed reclaimed space.",
            id="notice",
            markup=False,
        )
        with VerticalScroll(id="review"):
            yield Static("", id="review-text", markup=False)
        yield Input(placeholder="Type literal DELETE after reviewing exact selection", id="confirm")
        with Horizontal(id="actions"):
            yield Button("Select matches", id="select")
            yield Button("Clear selection", id="clear")
            yield Button("Preview", id="preview", variant="primary")
            yield Button("Cancel / Back", id="cancel")
            yield Button("Execute DELETE", id="execute", variant="error")
            yield Button("Refresh", id="refresh")
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

    async def _check_interrupt(self) -> None:
        if self.interrupt_requested and not self.dispatching:
            self.interrupt_requested = False
            await self.action_quit()

    def _notice(self, text: str) -> None:
        self.query_one("#notice", Static).update(clean(text))

    def _catalogue_is_live(self) -> bool:
        return not self.catalogue_teardown and self.is_running

    async def _load(self) -> None:
        if not self._catalogue_is_live():
            return
        self.query_one("#heading", Static).update("Loading metadata catalogue…")
        try:
            catalogue = await asyncio.to_thread(read_session_catalogue, self.home)
            if not self._catalogue_is_live():
                return
            self.catalogue, self.index = catalogue, CatalogueIndex(catalogue)
            all_groups = self.index.query(CatalogueQuery())
            self.work_roots = {str(group.root.id.value): group.root.id for group in all_groups}
            options = [("All works", "all")]
            options.extend(
                (clean(group.root.title[:70]) or "Untitled", str(group.root.id.value))
                for group in all_groups
            )
            self.query_one("#work", Select).set_options(options)
            self.query_one("#heading", Static).update(
                f"{clean(str(self.home))} — {len(catalogue.entries):,} histories. "
                "Sizes: observed stored file bytes; unknowns retained."
            )
            self._filter()
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

    def on_select_changed(self, _event: Select.Changed) -> None:
        """Apply categorical filters to detached metadata."""
        self._filter()

    def _filter(self) -> None:
        if self.index is None or self.dispatching or self.review_open:
            return
        try:
            date = self.query_one("#before", Input).value.strip()
            before = parse_cutoff(date).timestamp if date else None
            if before is not None:
                self.cutoff = UtcCutoff(before)
            work = self.query_one("#work", Select).value
            roots = (self.work_roots[str(work)],) if str(work) in self.work_roots else ()
            query = CatalogueQuery(
                text=self.query_one("#search", Input).value,
                before=before,
                min_bytes=_bound(self.query_one("#minimum", Input).value),
                max_bytes=_bound(self.query_one("#maximum", Input).value),
                scope=cast("Any", self.query_one("#scope", Select).value),
                sort=cast("Any", self.query_one("#sort", Select).value),
                roots=roots,
            )
            self.groups = self.index.query(query)
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
        for group in self.groups:
            key = "g:" + str(group.root.id.value)
            self.row_map[key] = group, None
            picked = sum(identifier in self.selected for identifier in group.matching_ids)
            caption = (
                f"{'▾' if group.root.id in self.expanded else '▸'} "
                f"[{picked}/{len(group.matching_ids)}] {group.root.title[:120] or 'Untitled'} "
                f"[{group.root.source.value}]"
            )
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
                key = "h:" + str(entry.id.value)
                self.row_map[key] = group, entry.id
                matches = entry.id in group.matching_ids
                caption = (
                    f"  {'✓' if entry.id in self.selected else '○'} "
                    f"{entry.title[:120] or 'Untitled'} [{entry.source.value}]"
                )
                table.add_row(
                    Text(clean(caption), style="" if matches else "dim"),
                    _size(entry.known_stored_bytes, int(entry.stored_bytes is None)),
                    "—",
                    "—",
                    "match" if matches else "context only",
                    str(entry.last_activity or "unknown"),
                    key=key,
                )
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        self._notice(
            f"{len(self.groups):,} work groups; {len(self.selected):,} exact histories selected "
            "(selection persists across filters). Enter expands; Space toggles matches."
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

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route explicit browsing and execution actions."""
        action = event.button.id
        if self.dispatching:
            self._notice(
                "Native execution in progress. Wait for outcome and durable "
                "receipt; quitting is disabled."
            )
            return
        if self.review_open and action not in {"cancel", "quit", "execute"}:
            return
        if action == "select":
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
        elif action == "quit":
            await self.action_quit()

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
        for identifier in ("select", "clear", "refresh", "preview"):
            self.query_one("#" + identifier, Button).disabled = enabled
        for widget in self.query(".filters Input, .filters Select"):
            widget.disabled = enabled

    def _start_preview(self) -> None:
        if self.review_open or self.dispatching:
            return
        if (
            not self.selected
            or not self.valid_filters
            or not self.query_one("#before", Input).value.strip()
        ):
            self._notice("Select exact histories and a valid UTC cutoff before preview.")
            return
        intent = Intent(
            self.home,
            self.cutoff,
            tuple(sorted(set(self.selected.values()))),
            tuple(sorted(self.selected)),
        )
        self.reviewed = None
        self._review_mode(enabled=True)
        self.query_one("#review-text", Static).update(
            "Reading selected metadata headers for conditional preview… Esc cancels "
            "before dispatch and waits for the preview worker."
        )
        self.preview_task = asyncio.create_task(self._preview(intent))

    async def _preview(self, intent: Intent) -> None:
        report: dict[str, Any] | None = None
        try:
            report = await async_preview(intent)
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
            self.query_one("#review-text", Static).update("\n".join(clean(line) for line in lines))
            self.reviewed = confirmed_intent(intent, report)
            self.query_one("#confirm").display = True
            self.query_one("#execute").display = True
            self._notice(
                "CONDITIONAL preview; not permission or proof of closed work. "
                "Exact deletion IDs are frozen. Type DELETE to dispatch once."
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
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
        self.reviewed = None
        self._review_mode(enabled=False)
        self._notice("Preview closed before dispatch; no cleanup was requested.")

    async def _dispatch(self, intent: Intent) -> None:
        self.dispatching = True
        try:
            if self.helper is None:
                self._notice(
                    "No qualified helper found. Supply --helper with the supported binary."
                )
                return
            self.receipt = receipt_path(self.receipts, self.home)
            self._notice(
                "Executing once. Do not close: awaiting native outcome and durable receipt…"
            )
            backend = asyncio.create_task(
                asyncio.to_thread(apply_intent, intent, self.helper, self.receipt)
            )
            while True:
                try:
                    report, status = await asyncio.shield(backend)
                    break
                except asyncio.CancelledError:
                    # Confirmation transferred ownership to the durable backend.
                    # Teardown must join it, not turn cancellation into a retry.
                    continue
            self.completed_result = (result_text(report), status)
            self.exit(self.completed_result)
        except Exception as error:  # noqa: BLE001
            self.completed_result = (
                f"UNKNOWN after confirmation: {error}. Keep receipt and reconcile; do not retry."
                if self.receipt
                else str(error),
                4 if self.receipt else 2,
            )
            self.exit(self.completed_result)
        finally:
            self.dispatching = False

    async def on_unmount(self) -> None:
        """Join owned work even if framework teardown bypasses ordinary quit actions."""
        self.catalogue_teardown = True
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
        await self.action_cancel_preview()
        if self.load_task is not None and not self.load_task.done():
            self._notice("Waiting for catalogue observation to finish before exit…")
            await self.load_task
        self.exit(("Stopped before dispatch.", 0))


def run(arguments: argparse.Namespace) -> int:
    """Run Textual on a TTY; retain backend results and receipt after terminal teardown."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.stderr.write("ui requires an interactive terminal (TTY).\n")
        return 2
    home = (arguments.home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve()
    if not home.is_dir():
        sys.stderr.write("--home must name an existing directory.\n")
        return 2
    state = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    app = SessionApp(
        home,
        arguments.cutoff or UtcCutoff(datetime.now(UTC)),
        arguments.helper.expanduser().resolve() if arguments.helper else discover_helper(),
        arguments.receipt_dir or state / "codex-session-gc/receipts",
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
    return status
