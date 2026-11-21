# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Stdio MCP browsing and explicitly confirmed cleanup operations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable  # noqa: TC003
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context  # noqa: TC002
from mcp.types import ToolAnnotations

from codex_session_gc import diagnostics
from codex_session_gc.cli.console_display import discover_helper
from codex_session_gc.cli.console_tasks import Intent, async_preview, cutoff_text, parse_cutoff
from codex_session_gc.cli.mcp_capability import McpCapability
from codex_session_gc.cli.storage_service import StorageService
from codex_session_gc.cli.summary_display import cwd_label
from codex_session_gc.compatibility.observation import ImportedSupport
from codex_session_gc.domain import ArchiveState, ExactIds, ThreadId
from codex_session_gc.execution.confirmed_operation import ConfirmedOperations
from codex_session_gc.execution.selection_provenance import reviewed_associations
from codex_session_gc.execution.timing import TimingObserver, observe, worker_wakeup
from codex_session_gc.installation.model import InstallLayout
from codex_session_gc.inventory.catalogue import SessionEntry, read_session_catalogue
from codex_session_gc.inventory.query import (
    CatalogueIndex,
    CatalogueQuery,
    CatalogueScope,
    ProjectRow,
    Summary,
    summarize_catalogue,
)

_PAGE = 100
_TEXT = 500
_SELECTION = 200
_REPORT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SupportScope:
    """Locally selected installation bounds for support record observations."""

    layout: InstallLayout
    installation_id: uuid.UUID

    def __post_init__(self) -> None:
        """Require an absolute application layout and one installation identity."""
        if type(self.layout) is not InstallLayout or type(self.installation_id) is not uuid.UUID:
            raise ValueError("A support scope requires an installation layout and UUID.")


def _text(value: str) -> str:
    return "".join(char if char.isprintable() else " " for char in value)[:_TEXT]


def _entry(entry: SessionEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id.value),
        "title": _text(entry.title),
        "cwd": _text(entry.cwd),
        "last_activity": entry.last_activity.isoformat() if entry.last_activity else None,
        "archive_state": entry.archive_state.value,
        "parent_id": str(entry.parent_id.value) if entry.parent_id else None,
        "root_id": str(entry.root_id.value) if entry.root_id else None,
        "stored_bytes": entry.stored_bytes,
        "known_stored_bytes": entry.known_stored_bytes,
        "physical_complete": entry.physical_complete,
        "reported_history_mode": entry.reported_history_mode.value,
        "physical_lineage": "unknown",
        "shared_retained_bytes": entry.shared_retained_bytes,
        "conditional_reclaim_bytes": entry.conditional_reclaim_bytes,
        "physical_rollouts": [
            {
                "thread_id": str(item.ownership.thread_id.value),
                "rollout_id": str(item.ownership.rollout_id.value),
                "path": str(item.path),
                "representation": item.representation.value,
                "stored_bytes": item.stat.stored_bytes,
            }
            for item in entry.physical_rollouts
        ],
        "agent_status": entry.agent_status,
        "source": entry.source.value,
        "issue": entry.issue,
    }


def _page(offset: int, limit: int) -> None:
    if offset < 0 or not 1 <= limit <= _PAGE:
        raise ValueError("Invalid page bounds.")


class CatalogueObservation:
    """Read-only catalogue generation and one bounded summary cache."""

    def __init__(self, home: Path) -> None:
        """Bind a home without acquiring cleanup state."""
        self.home = home.resolve()
        self.snapshot_id: str | None = None
        self.index: CatalogueIndex | None = None
        self._lock = asyncio.Lock()
        self._summary_cache: tuple[str, CatalogueQuery, str, Summary] | None = None
        self._project_keys: dict[str, str] = {}
        self._cwd_keys: dict[str, str] = {}

    async def _snapshot(
        self, snapshot_id: str | None, *, refresh: bool = False
    ) -> tuple[str, CatalogueIndex]:
        async with self._lock:
            if snapshot_id is not None and (refresh or snapshot_id != self.snapshot_id):
                raise ValueError("The catalogue snapshot has expired; query the current snapshot.")
            if refresh or self.index is None:
                self.index = CatalogueIndex(
                    await asyncio.to_thread(read_session_catalogue, self.home)
                )
                self.snapshot_id = str(uuid.uuid4())
                self._summary_cache = None
                self._project_keys = {
                    f"project/{self.snapshot_id}/{ordinal}": cwd
                    for ordinal, cwd in enumerate(
                        sorted({e.cwd for e in self.index.entries.values()})
                    )
                }
                self._cwd_keys = {cwd: key for key, cwd in self._project_keys.items()}
            if snapshot_id is not None and snapshot_id != self.snapshot_id:
                raise ValueError("The catalogue snapshot has expired; query the current snapshot.")
            assert self.snapshot_id is not None  # noqa: S101
            assert self.index is not None  # noqa: S101
            return self.snapshot_id, self.index

    async def catalogue_summary(  # noqa: PLR0913
        self,
        *,
        snapshot_id: str | None = None,
        refresh: bool = False,
        text: str = "",
        before: str | None = None,
        min_bytes: int | None = None,
        max_bytes: int | None = None,
        scope: CatalogueScope = CatalogueScope.ALL,
        roots: list[str] | None = None,
        cwd: str | None = None,
        archive_state: ArchiveState | None = None,
        rank: Literal["stored_bytes", "closed_agent_bytes"] = "stored_bytes",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Summarize matching histories by exact stored cwd; grants no cleanup permission."""
        _page(offset, limit)
        if len(text) > _TEXT or (roots is not None and len(roots) > _SELECTION):
            raise ValueError("Query input exceeds the transport limit.")
        if rank not in ("stored_bytes", "closed_agent_bytes"):
            raise ValueError("Unknown summary rank.")
        query = CatalogueQuery(
            text=text.casefold(),
            before=parse_cutoff(before).timestamp if before else None,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            scope=scope,
            roots=tuple(
                sorted(
                    {ThreadId.parse(value) for value in roots or []},
                    key=lambda value: str(value.value),
                )
            ),
            cwd=cwd,
            archive_state=archive_state,
        )
        identity, index = await self._snapshot(snapshot_id, refresh=refresh)
        if self._summary_cache is not None and self._summary_cache[:3] == (identity, query, rank):
            summary = self._summary_cache[3]
        else:
            summary = summarize_catalogue(index.query(query).matching_histories, rank)
            if self.snapshot_id == identity:
                self._summary_cache = (identity, query, rank, summary)
        page = summary.projects[offset : offset + limit]

        def project(row: ProjectRow) -> dict[str, Any]:
            display, truncated = cwd_label(row.cwd, _TEXT)
            return {
                "project_key": self._cwd_keys[row.cwd],
                "cwd_display": display,
                "cwd_truncated": truncated,
                "breakdown": asdict(row.breakdown),
            }

        return {
            "snapshot_id": identity,
            "rank": rank,
            "overall": asdict(summary.overall),
            "projects": [project(row) for row in page],
            "total_projects": len(summary.projects),
            "offset": offset,
            "next_offset": offset + len(page)
            if offset + len(page) < len(summary.projects)
            else None,
            "advisory": True,
            "permission": False,
            "source_changed": index.catalogue.source_changed,
            "physical_complete": index.catalogue.physical_complete,
            "diagnostics": [_text(value) for value in index.catalogue.diagnostics[:_PAGE]],
            "diagnostics_truncated": len(index.catalogue.diagnostics) > _PAGE,
        }


class CatalogueService(CatalogueObservation):
    """One fixed-home observation cache; explicit refresh replaces its identity."""

    def __init__(
        self,
        home: Path,
        helper: Path | None = None,
        receipts: Path | None = None,
        *,
        capability: McpCapability = McpCapability.CLEANUP,
        support_scope: SupportScope | None = None,
    ) -> None:
        """Bind all tools to the startup home, never client-supplied paths."""
        super().__init__(home)
        self.capability = McpCapability(capability)
        self.support_scope = support_scope
        state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        self.operations = ConfirmedOperations(
            self.home, helper, receipts or state / "codex-session-gc/receipts"
        )
        self.storage = StorageService(self.home)

    async def storage_overview(self, *, refresh: bool = False) -> dict[str, Any]:
        """Observe storage once, or explicitly refresh the published generation."""
        return await self.storage.overview(refresh=refresh)

    async def storage_objects(
        self,
        storage_snapshot_id: str,
        association: Literal[
            "all", "matched", "missing", "ambiguous", "unknown", "unparsed"
        ] = "all",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read a bounded page from the current storage generation."""
        return self.storage.objects(
            storage_snapshot_id, association=association, offset=offset, limit=limit
        )

    async def storage_diagnostics(
        self, storage_snapshot_id: str, *, refresh: bool = False, providers: bool = False
    ) -> dict[str, Any]:
        """Build diagnostic evidence for the current storage snapshot."""
        return await self.storage.diagnostics(
            storage_snapshot_id, refresh=refresh, providers=providers
        )

    async def storage_diagnostic_subjects(
        self, diagnostic_id: str, reason: str = "problems", offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Page through subjects from one diagnostic observation."""
        return self.storage.diagnostic_subjects(
            diagnostic_id, reason=reason, offset=offset, limit=limit
        )

    async def storage_explain(
        self,
        diagnostic_id: str,
        subject_id: str,
        direction: Literal["all", "incoming", "outgoing"] = "all",
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Explain one observed subject with direct and derived evidence."""
        return self.storage.explain(
            diagnostic_id, subject_id, direction=direction, offset=offset, limit=limit
        )

    async def compatibility_status(
        self, record_sha256: str | None = None, context_sha256: str | None = None
    ) -> dict[str, Any]:
        """Observe compatibility; omit both digests for the bundled support record.

        Imported records require both digests and a locally configured startup scope.
        The result does not grant cleanup authority.
        """
        if record_sha256 is None and context_sha256 is None:
            report = await asyncio.to_thread(
                diagnostics.inspect_compatibility, self.home, self.operations.helper
            )
        else:
            if record_sha256 is None or context_sha256 is None or self.support_scope is None:
                raise ValueError(
                    "Both support identities and a startup support scope are required."
                )
            selection = ImportedSupport(
                self.support_scope.layout,
                self.support_scope.installation_id,
                record_sha256,
                context_sha256,
            )
            report = await asyncio.to_thread(
                diagnostics.inspect_selected_compatibility,
                self.home,
                self.operations.helper,
                selection,
            )
        return asdict(report)

    async def query_catalogue(  # noqa: PLR0913
        self,
        *,
        snapshot_id: str | None = None,
        refresh: bool = False,
        text: str = "",
        before: str | None = None,
        min_bytes: int | None = None,
        max_bytes: int | None = None,
        scope: CatalogueScope = CatalogueScope.ALL,
        order_target: Literal["work", "history"] = "work",
        sort: Literal["total", "own", "agents", "stored_bytes", "activity"] = "total",
        direction: Literal["asc", "desc"] = "desc",
        roots: list[str] | None = None,
        cwd: str | None = None,
        project_key: str | None = None,
        archive_state: ArchiveState | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Browse exact matching histories with full work totals; sizes are stat bytes."""
        _page(offset, limit)
        if len(text) > _TEXT or (roots is not None and len(roots) > _SELECTION):
            raise ValueError("Query input exceeds the transport limit.")
        if project_key is not None and (cwd is not None or snapshot_id is None or refresh):
            raise ValueError("A current snapshot and valid project key are required, without cwd.")
        query = CatalogueQuery(
            text=text,
            before=parse_cutoff(before).timestamp if before else None,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            scope=scope,
            order_target=order_target,
            sort=sort,
            direction=direction,
            roots=tuple(ThreadId.parse(value) for value in roots or []),
            cwd=cwd,
            archive_state=archive_state,
        )
        identity, index = await self._snapshot(snapshot_id, refresh=refresh)
        if project_key is not None:
            if project_key not in self._project_keys:
                raise ValueError("Unknown project key for this catalogue snapshot.")
            from dataclasses import replace  # noqa: PLC0415

            query = replace(query, cwd=self._project_keys[project_key])
        result = index.query(query)
        groups = result.groups
        matches = result.matching_histories
        page = matches[offset : offset + limit]
        by_history = {identifier: group for group in groups for identifier in group.matching_ids}
        page_groups = {by_history[entry.id].root.id: by_history[entry.id] for entry in page}
        summaries = {
            str(group.root.id.value): {
                "root": _entry(group.root),
                "own_stored_bytes": group.own_stored_bytes,
                "agent_stored_bytes": group.agent_stored_bytes,
                "total_stored_bytes": group.total_stored_bytes,
                "shared_retained_bytes": group.shared_retained_bytes,
                "conditional_reclaim_bytes": group.conditional_reclaim_bytes,
                "own_known_stored_bytes": group.own_known_stored_bytes,
                "agent_known_stored_bytes": group.agent_known_stored_bytes,
                "total_known_stored_bytes": group.total_known_stored_bytes,
                "own_incomplete": group.own_incomplete,
                "agent_incomplete": group.agent_incomplete,
                "incomplete_history_count": group.incomplete_history_count,
                "last_activity": group.last_activity.isoformat() if group.last_activity else None,
                "activity_incomplete": group.activity_incomplete,
                "selectable": group.selectable,
                "member_count": len(group.entries),
                "matching_count": len(group.matching_ids),
            }
            for group in page_groups.values()
        }
        return {
            "snapshot_id": identity,
            "total_matches": len(matches),
            "total_groups": len(groups),
            "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < len(matches) else None,
            "histories": [_entry(entry) for entry in page],
            "order": {
                "target": query.order_target,
                "field": query.sort,
                "direction": query.direction,
            },
            "groups": summaries,
            "source_changed": index.catalogue.source_changed,
            "physical_complete": index.catalogue.physical_complete,
            "diagnostics": [_text(value) for value in index.catalogue.diagnostics[:_PAGE]],
            "diagnostics_truncated": len(index.catalogue.diagnostics) > _PAGE,
            "advisory": True,
            "permission": False,
        }

    async def session_details(
        self, snapshot_id: str, thread_id: str, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """Inspect one history and paginated full work context from the same snapshot."""
        _page(offset, limit)
        identity, index = await self._snapshot(snapshot_id)
        identifier = ThreadId.parse(thread_id)
        if identifier not in index.entries:
            raise ValueError("Unknown catalogue history.")
        group = next(
            group
            for group in index.query(CatalogueQuery()).groups
            if identifier in {entry.id for entry in group.entries}
        )
        return {
            "snapshot_id": identity,
            "session": _entry(index.entries[identifier]),
            "selectable": group.selectable,
            "member_count": len(group.entries),
            "context": [_entry(entry) for entry in group.entries[offset : offset + limit]],
            "next_offset": offset + limit if offset + limit < len(group.entries) else None,
            "advisory": True,
            "permission": False,
        }

    async def prepare_cleanup(
        self, snapshot_id: str, thread_ids: list[str], cutoff: str | None = None
    ) -> dict[str, Any]:
        """Preview scoped metadata for exact IDs without dispatch or journal creation."""
        if not thread_ids or len(thread_ids) > _SELECTION:
            raise ValueError("Preparation requires a bounded nonempty exact selection.")
        self._require_startup_home()
        selected = tuple(ThreadId.parse(value) for value in thread_ids)
        if len(set(selected)) != len(selected):
            raise ValueError("Selection contains duplicate histories.")
        policy = parse_cutoff(cutoff) if cutoff is not None else ExactIds(frozenset(selected))
        identity, index = await self._snapshot(snapshot_id)
        if any(value not in index.entries for value in selected):
            raise ValueError("Selection contains duplicate or unknown histories.")
        roots = set()
        for identifier in selected:
            entry = index.entries[identifier]
            if entry.root_id is None or entry.root_id not in index.entries:
                raise ValueError("Selection has unresolved catalogue ownership.")
            roots.add(entry.root_id)
        intent = Intent(self.home, policy, tuple(sorted(roots)), selected)
        report = await async_preview(intent)
        self._require_startup_home()
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
        if len(encoded) > _REPORT_BYTES:
            raise ValueError(
                "Full preview exceeds the bounded transport response; use the terminal UI."
            )
        prepared = {
            "snapshot_id": identity,
            "preview_sha256": hashlib.sha256(encoded).hexdigest(),
            "report": report,
            "conditional": True,
            "can_apply_via_mcp": (
                self.capability is McpCapability.CLEANUP
                and bool(report["deletions"])
                and not report["errors"]
            ),
            "source_changed": index.catalogue.source_changed,
            "physical_complete": index.catalogue.physical_complete,
        }
        if prepared["can_apply_via_mcp"]:
            declaration = await asyncio.to_thread(
                self.operations.prepare,
                tuple(str(root.value) for root in sorted(roots)),
                tuple(report["deletions"]),
                cutoff_text(intent.cutoff),
                report["physical_manifest"],
                reviewed_associations(report["guardian_associations"]),
            )
            prepared["confirmation"] = {
                "operation_id": declaration.operation_id,
                "digest": declaration.digest,
            }
        if len(json.dumps(prepared, ensure_ascii=False).encode()) > _REPORT_BYTES:
            raise ValueError(
                "Complete preparation exceeds the response bound; reduce the selection."
            )
        return prepared

    def _require_startup_home(self) -> None:
        metadata = self.home.stat()
        if (metadata.st_dev, metadata.st_ino) != self.operations.home_identity:
            raise ValueError("The startup home identity has changed; restart the server.")

    async def apply_cleanup(self, operation_id: str, digest: str) -> dict[str, Any]:
        """Apply the exact server-held preparation referenced by two unchanged strings."""
        self._require_cleanup()
        return await self.operations.apply_owned(operation_id, digest)

    async def cleanup_status(self, operation_id: str) -> dict[str, Any]:
        """Read the saved operation receipt without dispatch or fresh history observation."""
        return await asyncio.to_thread(self.operations.status, operation_id)

    async def reconcile_cleanup(self, operation_id: str) -> dict[str, Any]:
        """Observe saved intents and write reconciliation evidence; never dispatch delete."""
        self._require_cleanup()
        return await asyncio.to_thread(self.operations.reconcile, operation_id)

    def _require_cleanup(self) -> None:
        if self.capability is McpCapability.READ_ONLY:
            raise PermissionError(
                "Read-only MCP cannot apply cleanup or append reconciliation evidence."
            )


async def _observed_call(
    ctx: Context[Any, Any], observer: TimingObserver, operation: Awaitable[dict[str, Any]]
) -> dict[str, Any]:
    """Own a cancellable coalescing sender independently of the backend result."""
    wake = asyncio.Event()

    async def send() -> None:
        sequence = 0
        try:
            while True:
                snapshot = observer.snapshot()
                sequence += 1
                message = json.dumps(snapshot, separators=(",", ":"))
                with anyio.fail_after(0.2):
                    await ctx.report_progress(sequence, total=None, message=message)
                with anyio.move_on_after(0.1):
                    await wake.wait()
                wake.clear()
        except TimeoutError, OSError, RuntimeError:
            return

    sender = asyncio.create_task(send())
    failed = True
    try:
        with observe(observer), worker_wakeup(wake):
            result = await operation
        failed = False
        return result
    finally:
        observer.finish(interrupted=failed)
        with anyio.CancelScope(shield=True):
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)


def create_server(
    home: Path,
    helper: Path | None = None,
    receipts: Path | None = None,
    *,
    capability: McpCapability = McpCapability.CLEANUP,
    support_scope: SupportScope | None = None,
) -> MCPServer[Any]:
    """Expose browsing and the shared confirmed operation over official SDK stdio."""
    service = CatalogueService(
        home, helper, receipts, capability=capability, support_scope=support_scope
    )

    @asynccontextmanager
    async def lifespan(_server: MCPServer[Any]) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            with anyio.CancelScope(shield=True):
                await service.storage.close()

    server = MCPServer(
        "codex-session-gc",
        lifespan=lifespan,
        instructions=(
            "Read-only mode exposes catalogue, details, conditional preview and saved status. "
            "Preparation creates no cleanup operation. Apply and reconciliation are unavailable. "
            "Attachment guidance is advisory and does not authorize cleanup."
            if service.capability is McpCapability.READ_ONLY
            else "Review prepare_cleanup report and confirmation before explicitly calling "
            "apply_cleanup. A digest binds the payload and does not prove human consent. "
            "After unknown or response loss, use cleanup_status/reconcile_cleanup; "
            "never create a replacement operation to retry deletion. Attachment guidance is "
            "advisory: explicitly select child IDs and prepare and authorize that operation "
            "separately. Observe its result, refresh the catalogue, then prepare the exact "
            "source IDs again and obtain separate authorization before applying them."
        ),
    )
    annotations = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, open_world_hint=False
    )

    async def prepare_cleanup(
        snapshot_id: str, thread_ids: list[str], ctx: Context[Any, Any], cutoff: str | None = None
    ) -> dict[str, Any]:
        """Preview scoped metadata for exact IDs without dispatch or journal creation."""
        return await _observed_call(
            ctx, TimingObserver("preview"), service.prepare_cleanup(snapshot_id, thread_ids, cutoff)
        )

    async def apply_cleanup(
        operation_id: str, digest: str, ctx: Context[Any, Any]
    ) -> dict[str, Any]:
        """Apply the exact server-held preparation referenced by two unchanged strings."""
        return await _observed_call(
            ctx, TimingObserver("apply"), service.apply_cleanup(operation_id, digest)
        )

    for tool in (
        service.storage_overview,
        service.storage_objects,
        service.storage_diagnostics,
        service.storage_diagnostic_subjects,
        service.storage_explain,
        service.query_catalogue,
        service.catalogue_summary,
        service.session_details,
        prepare_cleanup,
        service.compatibility_status,
    ):
        server.tool(annotations=annotations)(tool)
    server.tool(annotations=annotations)(service.cleanup_status)
    if service.capability is McpCapability.CLEANUP:
        server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                open_world_hint=False,
            )
        )(apply_cleanup)
        server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                open_world_hint=False,
            )
        )(service.reconcile_cleanup)
    return server


def run(
    home: Path,
    helper: Path | None = None,
    receipts: Path | None = None,
    *,
    capability: McpCapability = McpCapability.CLEANUP,
    support_scope: SupportScope | None = None,
) -> int:
    """Serve MCP on stdio; stdout is reserved for protocol messages."""
    capability = McpCapability(capability)
    if capability is McpCapability.CLEANUP and helper is None:
        helper = discover_helper()
    create_server(home, helper, receipts, capability=capability, support_scope=support_scope).run(
        "stdio"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--helper", type=Path)
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--support-install-dir", type=Path)
    parser.add_argument("--support-installation-id", type=uuid.UUID)
    parser.add_argument(
        "--read-only",
        action="store_const",
        const=McpCapability.READ_ONLY,
        default=McpCapability.CLEANUP,
        dest="capability",
    )
    arguments = parser.parse_args()
    if (arguments.support_install_dir is None) != (arguments.support_installation_id is None):
        parser.error(
            "--support-install-dir and --support-installation-id must be supplied together"
        )
    scope = (
        SupportScope(
            InstallLayout(arguments.support_install_dir.expanduser().absolute()),
            arguments.support_installation_id,
        )
        if arguments.support_install_dir is not None
        else None
    )
    raise SystemExit(
        run(
            arguments.home,
            arguments.helper,
            arguments.receipt_dir,
            capability=arguments.capability,
            support_scope=scope,
        )
    )
