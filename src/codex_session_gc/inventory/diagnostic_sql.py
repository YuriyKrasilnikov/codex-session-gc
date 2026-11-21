# Copyright (c) 2026
# ruff: noqa: PLR0911

"""Bounded, detached SQL facts for storage diagnostics."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from codex_session_gc import sqlite_engine as sqlite3
from codex_session_gc.inventory.storage_sql import (
    _STATE_NAME,
    _canonical_owner,
    _source,
    _source_identity,
    _supported,
)
from codex_session_gc.inventory.storage_walk import StorageScanCancelled, StorageScanError
from codex_session_gc.sqlite_read import read_transaction

if TYPE_CHECKING:
    from collections.abc import Callable

    from codex_session_gc.inventory.storage_walk import StorageScan

_MAX_SUBJECTS = 25_000
_MAX_ROWS = 250_000
_MAX_EVIDENCE = 50_000
_MAX_POINTER_BYTES = 4096
_MAX_BYTES = 8 * 1024 * 1024

Coverage = Literal["complete", "partial", "unknown", "not_requested"]


@dataclass(frozen=True, slots=True)
class DiagnosticLimits:
    """Independent bounds for one diagnostic SQL and evidence projection."""

    max_rows: int = _MAX_ROWS
    max_edges: int = 250_000
    max_bytes: int = _MAX_BYTES
    max_evidence: int = _MAX_EVIDENCE
    max_subjects: int = _MAX_SUBJECTS
    max_pointer_bytes: int = _MAX_POINTER_BYTES
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        """Reject invalid bounds before opening the source."""
        values = (
            self.max_rows,
            self.max_edges,
            self.max_bytes,
            self.max_evidence,
            self.max_subjects,
            self.max_pointer_bytes,
        )
        if (
            any(type(value) is not int or value < 1 for value in values)
            or self.max_rows > _MAX_ROWS
            or self.max_evidence > _MAX_EVIDENCE
            or self.max_pointer_bytes > _MAX_POINTER_BYTES
            or self.max_subjects > _MAX_SUBJECTS
            or self.max_bytes > _MAX_BYTES
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            msg = "Invalid diagnostic observation budget."
            raise ValueError(msg)


DEFAULT_DIAGNOSTIC_LIMITS = DiagnosticLimits()


@dataclass(frozen=True, slots=True)
class SqlThread:
    """Canonical owner and bounded private SQL values."""

    thread_id: str
    pointer: str | None
    attachment: str | None
    invalid_attachment: bool


@dataclass(frozen=True, slots=True)
class SqlEdge:
    """A row with invalid endpoint values suppressed."""

    parent: str | None
    child: str | None
    status: str | None


@dataclass(frozen=True, slots=True)
class SqlFacts:
    """Witnessed facts, coverage, counters and observation interval."""

    threads: tuple[SqlThread, ...] = ()
    edges: tuple[SqlEdge, ...] = ()
    owners_coverage: Coverage = "unknown"
    pointers_coverage: Coverage = "unknown"
    edges_coverage: Coverage = "unknown"
    started_at: str | None = None
    finished_at: str | None = None
    reason: str | None = None
    invalid_ids: int = 0
    invalid_pointers: int = 0
    invalid_endpoints: int = 0
    status_coverage: Coverage = "unknown"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _check(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        msg = "Storage diagnostic observation cancelled."
        raise StorageScanCancelled(msg)


def observe_sql(  # noqa: C901, PLR0912, PLR0915
    scan: StorageScan, *, cancelled: Callable[[], bool], limits: DiagnosticLimits
) -> SqlFacts:
    """Read bounded facts in one transaction; preserve witnessed rows on a limit."""
    entry = _source(scan)
    if entry is None:
        return SqlFacts(reason="sql_discovery")
    started = _now()
    deadline = time.monotonic() + limits.timeout_seconds
    threads: list[SqlThread] = []
    edges: list[SqlEdge] = []
    owner_complete = pointer_complete = edge_complete = False
    statuses_valid = True
    invalid_ids = invalid_pointers = invalid_endpoints = 0
    used = 0
    rows_used = 0
    reason: str | None = None
    started_transaction = False
    owner_finished = False
    before = None
    try:
        before = _source_identity(scan, entry)
        with read_transaction(scan.home / _STATE_NAME, deadline=deadline) as connection:
            connection.set_progress_handler(
                lambda: int(cancelled() or time.monotonic() >= deadline), 1000
            )
            if not _supported(connection):
                return SqlFacts(started_at=started, finished_at=_now(), reason="sql_schema")
            started_transaction = True
            query = (
                "SELECT CASE WHEN typeof(id)='text' AND octet_length(id)=36 "
                "THEN CAST(id AS BLOB) END, "
                "CASE WHEN typeof(rollout_path)='text' AND octet_length(rollout_path)<=? "
                "THEN CAST(rollout_path AS BLOB) END, "
                "CASE WHEN typeof(attached_thread_parent_id)='text' "
                "AND octet_length(attached_thread_parent_id)=36 "
                "THEN CAST(attached_thread_parent_id AS BLOB) END, "
                "attached_thread_parent_id IS NULL FROM threads LIMIT ?"
            )
            owner_complete = pointer_complete = True
            for index, (raw_id, raw_pointer, raw_parent, parent_null) in enumerate(
                connection.execute(query, (limits.max_pointer_bytes, limits.max_rows + 1))
            ):
                _check(cancelled)
                size = sum(
                    len(item) for item in (raw_id, raw_pointer, raw_parent) if item is not None
                )
                if (
                    index >= limits.max_rows
                    or used + size > limits.max_bytes
                    or time.monotonic() >= deadline
                ):
                    owner_complete = pointer_complete = False
                    reason = (
                        "sql_row_limit"
                        if index >= limits.max_rows
                        else "sql_byte_limit"
                        if used + size > limits.max_bytes
                        else "sql_deadline"
                    )
                    break
                used += size
                rows_used += 1
                identifier = _canonical_owner(raw_id)
                pointer = _text(raw_pointer)
                parent = _canonical_owner(raw_parent)
                if identifier is None:
                    invalid_ids += 1
                    owner_complete = pointer_complete = False
                    continue
                if pointer is not None:
                    path = Path(pointer)
                    if (
                        "\0" in pointer
                        or not path.is_absolute()
                        or ".." in path.parts
                        or str(path) != pointer
                        or not path.is_relative_to(scan.home)
                    ):
                        pointer = None
                if pointer is None:
                    invalid_pointers += 1
                    pointer_complete = False
                threads.append(
                    SqlThread(identifier, pointer, parent, not parent_null and parent is None)
                )
            owner_finished = True
            edge_query = (
                "SELECT CASE WHEN typeof(parent_thread_id)='text' "
                "AND octet_length(parent_thread_id)=36 "
                "THEN CAST(parent_thread_id AS BLOB) END, "
                "CASE WHEN typeof(child_thread_id)='text' AND octet_length(child_thread_id)=36 "
                "THEN CAST(child_thread_id AS BLOB) END, "
                "CASE WHEN typeof(status)='text' AND octet_length(status)<=6 "
                "THEN CAST(status AS BLOB) END FROM thread_spawn_edges LIMIT ?"
            )
            edge_complete = True
            edge_allowance = min(limits.max_edges, limits.max_rows - rows_used)
            for index, (raw_parent, raw_child, raw_status) in enumerate(
                connection.execute(edge_query, (edge_allowance + 1,))
            ):
                _check(cancelled)
                size = sum(
                    len(item) for item in (raw_parent, raw_child, raw_status) if item is not None
                )
                if (
                    index >= edge_allowance
                    or used + size > limits.max_bytes
                    or time.monotonic() >= deadline
                ):
                    edge_complete = False
                    reason = (
                        "sql_row_limit"
                        if index >= edge_allowance
                        else "sql_byte_limit"
                        if used + size > limits.max_bytes
                        else "sql_deadline"
                    )
                    break
                used += size
                parent, child = _canonical_owner(raw_parent), _canonical_owner(raw_child)
                status = _text(raw_status)
                if status not in {"active", "open", "closed"}:
                    statuses_valid = False
                if parent is None or child is None:
                    invalid_endpoints += 1
                    edge_complete = False
                edges.append(
                    SqlEdge(
                        parent, child, status if status in {"active", "open", "closed"} else None
                    )
                )
        after = _source_identity(scan, entry)
        if any(after.get(name) != identity for name, identity in before.items()):
            return SqlFacts(started_at=started, finished_at=_now(), reason="sql_sidecar_changed")
    except StorageScanError, StorageScanCancelled:
        raise
    except OSError, ValueError, sqlite3.Error:
        _check(cancelled)
        if not started_transaction or before is None:
            return SqlFacts(started_at=started, finished_at=_now(), reason="sql_unavailable")
        try:
            after = _source_identity(scan, entry)
        except StorageScanError:
            raise
        except OSError, ValueError:
            return SqlFacts(started_at=started, finished_at=_now(), reason="sql_source_changed")
        if any(after.get(name) != identity for name, identity in before.items()):
            return SqlFacts(started_at=started, finished_at=_now(), reason="sql_sidecar_changed")
        if not owner_finished:
            owner_complete = pointer_complete = False
        edge_complete = False
        reason = "sql_interrupted"
    return SqlFacts(
        tuple(threads),
        tuple(edges),
        "complete" if owner_complete else ("partial" if threads else "unknown"),
        "complete" if pointer_complete else ("partial" if threads else "unknown"),
        "complete" if edge_complete else ("partial" if edges else "unknown"),
        started,
        _now(),
        reason,
        invalid_ids,
        invalid_pointers,
        invalid_endpoints,
        ("complete" if edge_complete and statuses_valid else ("partial" if edges else "unknown")),
    )
