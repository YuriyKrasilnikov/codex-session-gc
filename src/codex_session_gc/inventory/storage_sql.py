# Copyright (c) 2026

"""Associate observed history names with one bounded SQLite owner observation."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from codex_session_gc import sqlite_engine as sqlite3
from codex_session_gc.domain import ThreadId
from codex_session_gc.inventory.catalogue import (
    _SPAWN_SCHEMA,
    _THREAD_FOREIGN_KEYS,
    _THREAD_SCHEMA_62,
)
from codex_session_gc.inventory.storage_walk import (
    StorageScanCancelled,
    StorageScanError,
    identity_for_fd,
    open_root,
)
from codex_session_gc.sqlite_read import read_transaction

if TYPE_CHECKING:
    from collections.abc import Callable

    from codex_session_gc.inventory.storage_walk import MountIdentity, StorageEntry, StorageScan

# UUIDs in the storage format use 36 ASCII bytes.
OWNER_BYTES = 36
_STATE_NAME = "state_5.sqlite"
_SIDECARS = ("-wal", "-shm", "-journal")
_OWNER_QUERY = (
    "SELECT CASE WHEN typeof(id)='text' AND octet_length(id)=36 "
    "THEN CAST(id AS BLOB) END FROM threads LIMIT ?"
)


@dataclass(frozen=True, slots=True)
class SqlOwners:
    """Observed SQL owner IDs and the applicable reading interval."""

    coverage: str = "unknown"
    ids: frozenset[str] = frozenset()
    duplicates: frozenset[str] = frozenset()
    started: str | None = None
    finished: str | None = None
    reason: str | None = None


class _SqlGapError(ValueError):
    """A gap preventing an authoritative negative SQL association."""


def _columns(
    connection: sqlite3.Connection, table: str, maximum: int
) -> tuple[tuple[object, ...], ...]:
    rows = connection.execute(
        "SELECT CASE WHEN octet_length(name)<=128 THEN name END, "
        "CASE WHEN octet_length(type)<=128 THEN upper(type) END, "
        '"notnull", pk, CASE WHEN dflt_value IS NULL OR '
        "octet_length(dflt_value)<=128 THEN dflt_value ELSE '<oversized>' END "
        "FROM pragma_table_info(?) LIMIT ?",
        (table, maximum + 1),
    )
    return tuple(
        (name, kind, bool(required), pk, default) for name, kind, required, pk, default in rows
    )


def _keys(
    connection: sqlite3.Connection, table: str, maximum: int
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            'SELECT CASE WHEN octet_length("from")<=128 THEN "from" END, '
            'CASE WHEN octet_length("table")<=128 THEN "table" END, '
            'CASE WHEN octet_length("to")<=128 THEN "to" END, '
            "CASE WHEN octet_length(on_delete)<=128 THEN on_delete END "
            "FROM pragma_foreign_key_list(?) LIMIT ?",
            (table, maximum + 1),
        )
    )


def _supported(connection: sqlite3.Connection) -> bool:
    matches = connection.execute("PRAGMA encoding").fetchone()
    keys = _keys(connection, "threads", len(_THREAD_FOREIGN_KEYS))
    return (
        matches is not None
        and matches[0] == "UTF-8"
        and _columns(connection, "threads", len(_THREAD_SCHEMA_62)) == _THREAD_SCHEMA_62
        and _columns(connection, "thread_spawn_edges", len(_SPAWN_SCHEMA)) == _SPAWN_SCHEMA
        and len(keys) == len(_THREAD_FOREIGN_KEYS)
        and frozenset(keys) == _THREAD_FOREIGN_KEYS
        and not _keys(connection, "thread_spawn_edges", 0)
    )


def _source(scan: StorageScan) -> StorageEntry | None:
    if not scan.root_complete:
        return None
    candidates = [
        entry
        for entry in scan.entries
        if len(entry.parts) == 1
        and entry.parts[0].startswith("state_")
        and entry.parts[0].endswith(".sqlite")
    ]
    if len(candidates) != 1 or candidates[0].parts != (_STATE_NAME,):
        return None
    return candidates[0]


def _child_identity(root: int, name: str, mount: int) -> MountIdentity:
    child = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root)
    try:
        metadata = os.fstat(child)
        identity = identity_for_fd(child, metadata)
        if not stat.S_ISREG(metadata.st_mode) or identity.mount_id != mount or identity.ino <= 0:
            reason = "sql_unsafe_source"
            raise _SqlGapError(reason)
        return identity
    finally:
        os.close(child)


def _source_identity(scan: StorageScan, entry: StorageEntry) -> dict[str, MountIdentity]:
    root = open_root(scan.home)
    try:
        if identity_for_fd(root) != scan.home_identity:
            msg = "Storage root identity changed."
            raise StorageScanError(msg)
        mount = scan.home_identity.mount_id
        if _child_identity(root, _STATE_NAME, mount) != entry.identity:
            reason = "sql_source_changed"
            raise _SqlGapError(reason)
        sidecars = {}
        for suffix in _SIDECARS:
            try:
                sidecars[suffix] = _child_identity(root, _STATE_NAME + suffix, mount)
            except FileNotFoundError:  # optional per SQLite journal mode
                continue
        return sidecars
    finally:
        os.close(root)


def _canonical_owner(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    try:
        value = raw.decode("ascii")
        canonical = str(ThreadId.parse(value).value)
    except UnicodeError, ValueError:
        return None
    return value if value == canonical else None


def _read(
    connection: sqlite3.Connection, row_limit: int, deadline: float, cancelled: Callable[[], bool]
) -> tuple[set[str], set[str], bool]:
    if not _supported(connection):
        reason = "sql_schema"
        raise _SqlGapError(reason)
    ids: set[str] = set()
    duplicates: set[str] = set()
    invalid = False
    for index, (raw,) in enumerate(connection.execute(_OWNER_QUERY, (row_limit + 1,))):
        if cancelled():
            msg = "Storage observation cancelled."
            raise StorageScanCancelled(msg)
        if index >= row_limit:
            reason = "sql_limit"
            raise _SqlGapError(reason)
        if time.monotonic() >= deadline:
            reason = "sql_deadline"
            raise _SqlGapError(reason)
        value = _canonical_owner(raw)
        if value is None:
            invalid = True
        elif value in ids:
            duplicates.add(value)
        else:
            ids.add(value)
    return ids, duplicates, invalid


def _observe(
    scan: StorageScan,
    entry: StorageEntry,
    *,
    row_limit: int,
    timeout_seconds: float,
    cancelled: Callable[[], bool],
) -> tuple[set[str], set[str], bool]:
    before = _source_identity(scan, entry)
    deadline = time.monotonic() + timeout_seconds
    with read_transaction(scan.home / _STATE_NAME, deadline=deadline) as connection:
        connection.set_progress_handler(
            lambda: int(cancelled() or time.monotonic() >= deadline), 1000
        )
        result = _read(connection, row_limit, deadline, cancelled)
    after = _source_identity(scan, entry)
    if any(after.get(name) != identity for name, identity in before.items()):
        reason = "sql_sidecar_changed"
        raise _SqlGapError(reason)
    return result


def observe_owners(
    scan: StorageScan,
    *,
    row_limit: int,
    timeout_seconds: float,
    cancelled: Callable[[], bool],
) -> SqlOwners:
    """Return a typed SQL observation with explicit reasons for gaps."""
    entry = _source(scan)
    if entry is None:
        return SqlOwners(reason="sql_discovery")
    if row_limit <= 0:
        return SqlOwners(reason="sql_limit")
    started = datetime.now(UTC).isoformat()
    try:
        ids, duplicates, invalid = _observe(
            scan, entry, row_limit=row_limit, timeout_seconds=timeout_seconds, cancelled=cancelled
        )
    except StorageScanError, StorageScanCancelled:
        raise
    except _SqlGapError as error:
        return SqlOwners(started=started, finished=datetime.now(UTC).isoformat(), reason=str(error))
    except OSError, ValueError, sqlite3.Error:
        if cancelled():
            msg = "Storage observation cancelled."
            raise StorageScanCancelled(msg) from None
        return SqlOwners(
            started=started, finished=datetime.now(UTC).isoformat(), reason="sql_unavailable"
        )
    incomplete = invalid or bool(duplicates)
    return SqlOwners(
        "partial" if incomplete else "complete",
        frozenset(ids),
        frozenset(duplicates),
        started,
        datetime.now(UTC).isoformat(),
        "sql_invalid_id" if incomplete else None,
    )
