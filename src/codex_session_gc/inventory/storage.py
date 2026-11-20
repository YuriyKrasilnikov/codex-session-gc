# Copyright (c) 2026

"""Detached, physical storage measurements and advisory rollout associations."""

from __future__ import annotations

import math
import os
import stat
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from codex_session_gc.domain.physical import PhysicalRepresentation, ownership_from_path
from codex_session_gc.inventory.storage_sql import OWNER_BYTES, observe_owners
from codex_session_gc.inventory.storage_walk import (
    StorageScanCancelled,
    StorageScanError,
    identity_for_fd,
    open_root,
    scan_storage,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from codex_session_gc.domain import RolloutId, ThreadId
    from codex_session_gc.inventory.storage_sql import SqlOwners
    from codex_session_gc.inventory.storage_walk import MountIdentity, StorageEntry, StorageScan

PAGE_LIMIT = 100
ASSOCIATIONS = ("all", "matched", "missing", "ambiguous", "unknown", "unparsed")
_HISTORY_CATEGORIES = frozenset(("sessions", "archived_sessions"))
_DIRECTORIES = _HISTORY_CATEGORIES | {"packages", "worktrees", "cache", "plugins"}
_DISPLAY_LIMIT = 500
_BYTES_PER_BLOCK = 512
_SURROGATE_BASE = 0xDC00
_BYTE_SURROGATES = range(0xDC80, 0xDD00)


@dataclass(frozen=True, slots=True)
class StorageLimits:
    """Resource limits for one independent storage observation."""

    max_operations: int = 250_000
    max_sql_rows: int = 250_000
    max_path_bytes: int = 32 * 1024 * 1024
    sql_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        """Refuse invalid budget values before opening the home."""
        if (
            any(
                type(value) is not int or value < 1
                for value in (self.max_operations, self.max_sql_rows, self.max_path_bytes)
            )
            or not math.isfinite(self.sql_timeout_seconds)
            or self.sql_timeout_seconds <= 0
        ):
            msg = "Invalid storage observation budget."
            raise ValueError(msg)


DEFAULT_LIMITS = StorageLimits()


@dataclass(slots=True)
class _Bytes:
    known_bytes: int = 0
    unknown_objects: int = 0

    def add(self, value: int | None) -> None:
        if value is None:
            self.unknown_objects += 1
        else:
            self.known_bytes += value


@dataclass(slots=True)
class _Total:
    logical_file_bytes: _Bytes
    allocated_bytes: _Bytes
    unique_objects: int = 0

    @classmethod
    def empty(cls) -> _Total:
        return cls(_Bytes(), _Bytes())

    def add(self, values: _Measurements, *, identified: bool) -> None:
        self.logical_file_bytes.add(values.logical)
        self.allocated_bytes.add(values.allocated)
        self.unique_objects += identified


@dataclass(frozen=True, slots=True)
class _Measurements:
    logical: int | None
    allocated: int | None


@dataclass(frozen=True, slots=True)
class _History:
    entry: StorageEntry
    representation: str | None
    owner: ThreadId | None
    rollout: RolloutId | None


@dataclass(frozen=True, slots=True)
class _HistoryRow:
    object_id: str
    display_path: str
    path_truncated: bool
    category: str
    representation: str | None
    filename_thread_id: str | None
    rollout_id: str | None
    logical_file_bytes: int | None
    allocated_bytes: int | None
    association: str


@dataclass(frozen=True, slots=True)
class _Category:
    category: str
    logical_file_bytes: _Bytes
    allocated_bytes: _Bytes
    unique_objects: int


@dataclass(frozen=True, slots=True)
class _Summary:
    home_identity: MountIdentity
    started_at: str
    finished_at: str
    fs_started_at: str
    fs_finished_at: str
    sql_started_at: str | None
    sql_finished_at: str | None
    fs_coverage: str
    sql_coverage: str
    totals: _Total
    categories: tuple[_Category, ...]
    counts_by_reason: dict[str, int]
    history_coverage: str


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    """An observation with detached summaries and independently pageable rows."""

    storage_snapshot_id: str
    _summary: _Summary
    _rows: tuple[_HistoryRow, ...]

    def summary(self) -> dict[str, Any]:
        """Return summary values without sharing the observation's state."""
        result = asdict(self._summary)
        result["storage_snapshot_id"] = self.storage_snapshot_id
        return result

    def page(
        self, *, association: str = "all", offset: int = 0, limit: int = PAGE_LIMIT
    ) -> dict[str, Any]:
        """Return one detached page after filtering the observed history names."""
        if (
            association not in ASSOCIATIONS
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= PAGE_LIMIT
        ):
            msg = "Invalid storage page arguments."
            raise ValueError(msg)
        rows = (
            self._rows
            if association == "all"
            else tuple(row for row in self._rows if row.association == association)
        )
        end = offset + limit
        return {
            "storage_snapshot_id": self.storage_snapshot_id,
            "total_matching": len(rows),
            "offset": offset,
            "limit": limit,
            "next_offset": end if end < len(rows) else None,
            "items": [asdict(row) for row in rows[offset:end]],
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        msg = "Storage observation cancelled."
        raise StorageScanCancelled(msg)


def _category(parts: tuple[str, ...]) -> str:
    if not parts:
        return "home_root"
    first = parts[0]
    if first in _DIRECTORIES:
        return first
    if len(parts) == 1:
        if first.endswith(".sqlite"):
            return "databases"
        if first.endswith((".sqlite-wal", ".sqlite-shm", ".sqlite-journal")):
            return "database_sidecars"
    return "other"


def _display(parts: tuple[str, ...]) -> tuple[str, bool]:
    value = b"/".join(os.fsencode(part) for part in parts).decode("utf-8", "surrogateescape")
    pieces: list[str] = []
    length = 0
    for char in value:
        code = ord(char)
        if code in _BYTE_SURROGATES:
            piece = f"\\x{code - _SURROGATE_BASE:02x}"
        else:
            piece = char if char.isprintable() else f"\\u{code:04x}"
        if length + len(piece) > _DISPLAY_LIMIT:
            return "".join(pieces), True
        pieces.append(piece)
        length += len(piece)
    return "".join(pieces), False


def _nonnegative(value: int | None) -> int | None:
    return value if value is not None and value >= 0 else None


def _measure(entry: StorageEntry) -> _Measurements:
    info = entry.stat
    blocks = _nonnegative(getattr(info, "st_blocks", None))
    return _Measurements(
        _nonnegative(info.st_size) if stat.S_ISREG(info.st_mode) else 0,
        blocks * _BYTES_PER_BLOCK if blocks is not None else None,
    )


def _signature(entry: StorageEntry) -> tuple[int, int, int | None, int, int]:
    return (
        stat.S_IFMT(entry.stat.st_mode),
        entry.stat.st_size,
        getattr(entry.stat, "st_blocks", None),
        entry.stat.st_mtime_ns,
        entry.stat.st_ctime_ns,
    )


def _account(
    entries: tuple[StorageEntry, ...], counts: Counter[str], cancelled: Callable[[], bool]
) -> tuple[_Total, tuple[_Category, ...], set[MountIdentity]]:
    groups: dict[MountIdentity, list[StorageEntry]] = defaultdict(list)
    buckets: dict[str, _Total] = defaultdict(_Total.empty)
    total = _Total.empty()
    ambiguous: set[MountIdentity] = set()
    for entry in entries:
        _check_cancelled(cancelled)
        if entry.identity.ino > 0:
            groups[entry.identity].append(entry)
        else:
            counts["inode_unavailable"] += 1
            unknown = _Measurements(None, None)
            buckets[_category(entry.parts)].add(unknown, identified=False)
            total.add(unknown, identified=False)
    for identity, aliases in groups.items():
        _check_cancelled(cancelled)
        first, *remaining = aliases
        categories = {_category(entry.parts) for entry in aliases}
        category = next(iter(categories)) if len(categories) == 1 else "shared"
        if any(_signature(entry) != _signature(first) for entry in remaining):
            values = _Measurements(None, None)
            ambiguous.add(identity)
            counts["inode_changed"] += 1
        else:
            values = _measure(first)
        buckets[category].add(values, identified=True)
        total.add(values, identified=True)
    order = sorted(buckets, key=lambda name: (-buckets[name].allocated_bytes.known_bytes, name))
    return (
        total,
        tuple(
            _Category(
                name,
                buckets[name].logical_file_bytes,
                buckets[name].allocated_bytes,
                buckets[name].unique_objects,
            )
            for name in order
        ),
        ambiguous,
    )


def _history(home: Path, entry: StorageEntry) -> _History:
    name = entry.parts[-1]
    if name.endswith(".jsonl.zst"):
        representation = PhysicalRepresentation.ZSTD
    elif name.endswith(".jsonl"):
        representation = PhysicalRepresentation.PLAIN
    else:
        return _History(entry, None, None, None)
    try:
        edge = ownership_from_path(home.joinpath(*entry.parts), representation)
    except ValueError:
        return _History(entry, representation.value, None, None)
    return _History(entry, representation.value, edge.thread_id, edge.rollout_id)


def _association(owner: str | None, sql: SqlOwners, *, ambiguous: bool) -> str:
    if ambiguous or owner in sql.duplicates:
        return "ambiguous"
    if owner is None:
        return "unparsed"
    if owner in sql.ids:
        return "matched"
    return "missing" if sql.coverage == "complete" else "unknown"


def _rows(
    scan: StorageScan,
    sql: SqlOwners,
    disputed: set[MountIdentity],
    cancelled: Callable[[], bool],
    remaining_bytes: int,
) -> tuple[tuple[_HistoryRow, ...], int]:
    candidates: list[_History] = []
    for entry in scan.entries:
        _check_cancelled(cancelled)
        if (
            entry.parts
            and entry.parts[0] in _HISTORY_CATEGORIES
            and stat.S_ISREG(entry.stat.st_mode)
        ):
            candidates.append(_history(scan.home, entry))
    candidates.sort(key=lambda history: tuple(map(os.fsencode, history.entry.parts)))
    rollouts = Counter(history.rollout for history in candidates if history.rollout is not None)
    inodes = Counter(history.entry.identity for history in candidates)
    output = []
    for ordinal, history in enumerate(candidates, start=1):
        _check_cancelled(cancelled)
        entry = history.entry
        display, truncated = _display(entry.parts)
        object_id = f"obj/{ordinal}"
        identifier_bytes = OWNER_BYTES * sum(
            x is not None for x in (history.owner, history.rollout)
        )
        remaining_bytes -= len(display.encode("utf-8")) + len(object_id) + identifier_bytes
        if remaining_bytes < 0:
            break
        owner = str(history.owner.value) if history.owner is not None else None
        rollout = str(history.rollout.value) if history.rollout is not None else None
        ambiguous = (
            entry.identity.ino <= 0
            or entry.identity in disputed
            or inodes[entry.identity] > 1
            or (history.rollout is not None and rollouts[history.rollout] > 1)
        )
        values = _measure(entry)
        output.append(
            _HistoryRow(
                object_id,
                display,
                truncated,
                _category(entry.parts),
                history.representation,
                owner,
                rollout,
                values.logical,
                values.allocated,
                _association(owner, sql, ambiguous=ambiguous),
            )
        )
    return tuple(output), len(candidates) - len(output)


def _verify_root(home: Path, expected: MountIdentity) -> None:
    try:
        fd = open_root(home)
        try:
            identity = identity_for_fd(fd)
        finally:
            os.close(fd)
    except OSError, ValueError, IndexError:
        msg = "Storage root identity is unavailable."
        raise StorageScanError(msg) from None
    if identity != expected or identity.ino <= 0:
        msg = "Storage root identity is unavailable or changed."
        raise StorageScanError(msg)


def _active() -> bool:
    return False


def observe_storage(
    home: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
    limits: StorageLimits = DEFAULT_LIMITS,
) -> StorageSnapshot:
    """Sample filesystem metadata and SQL owners in separate intervals."""
    stop = _active if cancelled is None else cancelled
    started = _now()
    scan = scan_storage(
        home,
        cancelled=stop,
        max_operations=limits.max_operations,
        max_path_bytes=limits.max_path_bytes,
    )
    fs_finished = _now()
    retained_bytes = sum(
        max(1, sum(len(os.fsencode(part)) + 1 for part in entry.parts)) for entry in scan.entries
    )
    available = max(0, limits.max_path_bytes - retained_bytes)
    sql = observe_owners(
        scan,
        row_limit=min(limits.max_sql_rows, available // OWNER_BYTES),
        timeout_seconds=limits.sql_timeout_seconds,
        cancelled=stop,
    )
    counts = Counter({gap.reason: gap.count for gap in scan.gaps})
    if sql.reason is not None:
        counts[sql.reason] += 1
    total, categories, disputed = _account(scan.entries, counts, stop)
    row_bytes = available - OWNER_BYTES * (len(sql.ids) + len(sql.duplicates))
    rows, omitted = _rows(scan, sql, disputed, stop, row_bytes)
    if omitted:
        counts["history_row_limit"] += omitted
    _verify_root(scan.home, scan.home_identity)
    partial = bool(
        scan.gaps
        or total.logical_file_bytes.unknown_objects
        or total.allocated_bytes.unknown_objects
    )
    summary = _Summary(
        scan.home_identity,
        started,
        _now(),
        started,
        fs_finished,
        sql.started,
        sql.finished,
        "partial" if partial else "complete",
        sql.coverage,
        total,
        categories,
        dict(counts),
        "partial" if partial or omitted else "complete",
    )
    return StorageSnapshot(f"storage/{uuid4()}", summary, rows)
