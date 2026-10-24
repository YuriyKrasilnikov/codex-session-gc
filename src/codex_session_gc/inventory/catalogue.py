# Copyright (c) 2026

"""Cheap advisory session metadata, independent of cleanup qualification."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from codex_session_gc.domain import (
    ArchiveState,
    FileStatIdentity,
    HistoryMode,
    OwnershipEdge,
    PhysicalRepresentation,
    RolloutId,
    ThreadId,
)
from codex_session_gc.domain.physical import ownership_from_path
from codex_session_gc.inventory import metadata_walk
from codex_session_gc.sqlite_read import read_transaction

_THREAD_SCHEMA = (
    ("id", "TEXT", False, 1, None),
    ("rollout_path", "TEXT", True, 0, None),
    ("created_at", "INTEGER", True, 0, None),
    ("updated_at", "INTEGER", True, 0, None),
    ("source", "TEXT", True, 0, None),
    ("model_provider", "TEXT", True, 0, None),
    ("cwd", "TEXT", True, 0, None),
    ("title", "TEXT", True, 0, None),
    ("sandbox_policy", "TEXT", True, 0, None),
    ("approval_mode", "TEXT", True, 0, None),
    ("tokens_used", "INTEGER", True, 0, "0"),
    ("has_user_event", "INTEGER", True, 0, "0"),
    ("archived", "INTEGER", True, 0, "0"),
    ("archived_at", "INTEGER", False, 0, None),
    ("git_sha", "TEXT", False, 0, None),
    ("git_branch", "TEXT", False, 0, None),
    ("git_origin_url", "TEXT", False, 0, None),
    ("cli_version", "TEXT", True, 0, "''"),
    ("first_user_message", "TEXT", True, 0, "''"),
    ("agent_nickname", "TEXT", False, 0, None),
    ("agent_role", "TEXT", False, 0, None),
    ("memory_mode", "TEXT", True, 0, "'enabled'"),
    ("model", "TEXT", False, 0, None),
    ("reasoning_effort", "TEXT", False, 0, None),
    ("agent_path", "TEXT", False, 0, None),
    ("created_at_ms", "INTEGER", False, 0, None),
    ("updated_at_ms", "INTEGER", False, 0, None),
    ("thread_source", "TEXT", False, 0, None),
    ("preview", "TEXT", True, 0, "''"),
    ("recency_at", "INTEGER", True, 0, "0"),
    ("recency_at_ms", "INTEGER", True, 0, "0"),
    ("history_mode", "TEXT", True, 0, "'legacy'"),
    ("name", "TEXT", False, 0, None),
    ("is_pinned", "INTEGER", True, 0, "0"),
    ("thread_section_id", "TEXT", False, 0, None),
    ("section_position", "INTEGER", False, 0, None),
    ("section_entered_at_ms", "INTEGER", False, 0, None),
    ("project_id", "TEXT", False, 0, None),
    ("originator", "TEXT", False, 0, None),
    ("daybreak_enabled", "BOOLEAN", False, 0, None),
)
_THREAD_SCHEMA_WITH_CREATORS = (
    *_THREAD_SCHEMA,
    ("creator_user_id", "TEXT", False, 0, None),
    ("creator_account_id", "TEXT", False, 0, None),
)
_SPAWN_SCHEMA = (
    ("parent_thread_id", "TEXT", True, 0, None),
    ("child_thread_id", "TEXT", True, 1, None),
    ("status", "TEXT", True, 0, None),
)
_THREAD_FOREIGN_KEYS = frozenset(
    {
        ("thread_section_id", "thread_sections", "id", "SET NULL"),
        ("project_id", "projects", "id", "SET NULL"),
    }
)


def _supported_schema(connection: sqlite3.Connection) -> bool:
    """Compare the declared profile through SQLite's structured schema metadata."""
    return (
        _table_signature(connection, "threads") in {_THREAD_SCHEMA, _THREAD_SCHEMA_WITH_CREATORS}
        and _table_signature(connection, "thread_spawn_edges") == _SPAWN_SCHEMA
        and _foreign_keys(connection, "threads") == _THREAD_FOREIGN_KEYS
        and not _foreign_keys(connection, "thread_spawn_edges")
    )


def _table_signature(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[str, str, bool, int, str | None], ...]:
    """Return column types, nullability, keys and defaults without DDL text matching."""
    return tuple(
        (str(row[1]), str(row[2]).upper(), bool(row[3]), int(row[5]), row[4])
        for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _foreign_keys(
    connection: sqlite3.Connection, table: str
) -> frozenset[tuple[str, str, str, str]]:
    """Return declared foreign-key relations relevant to the supported profile."""
    return frozenset(
        (str(row[3]), str(row[2]), str(row[4]), str(row[6]))
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    )


def _archive_state(value: object) -> ArchiveState | None:
    """Map supported database archive values without inferring other values."""
    if value == 1:
        return ArchiveState.ARCHIVED
    if value == 0:
        return ArchiveState.NOT_ARCHIVED
    return None


def _milliseconds(milliseconds: object, seconds: object) -> int | None:
    """Prefer observed millisecond precision, then a supported seconds value."""
    if isinstance(milliseconds, int):
        return milliseconds
    if isinstance(seconds, int):
        return seconds * 1000
    return None


class SessionSource(StrEnum):
    """Actual SQL source classification, independent of spawn topology."""

    MAIN = "main"
    AGENT = "agent"
    GUARDIAN = "guardian"
    UNKNOWN = "unknown"


def _source(raw: object) -> SessionSource:
    if not isinstance(raw, str):
        return SessionSource.UNKNOWN
    try:
        value = raw if raw in {"cli", "vscode", "exec"} else json.loads(raw)
    except ValueError:
        return SessionSource.UNKNOWN
    if value in ("cli", "vscode", "exec"):
        return SessionSource.MAIN
    if (
        isinstance(value, dict)
        and set(value) == {"subagent"}
        and isinstance(value["subagent"], dict)
    ):
        subagent = value["subagent"]
        if subagent == {"other": "guardian"}:
            return SessionSource.GUARDIAN
        if set(subagent) == {"thread_spawn"} and isinstance(subagent["thread_spawn"], dict):
            return SessionSource.AGENT
    return SessionSource.UNKNOWN


@dataclass(frozen=True, slots=True)
class StoredRollout:
    """Advisory filename ownership and descriptor-relative stat, without header proof."""

    ownership: OwnershipEdge
    path: Path
    representation: PhysicalRepresentation
    stat: FileStatIdentity


@dataclass(frozen=True, slots=True)
class SessionEntry:
    """One display row; a root identifier never certifies runtime closure."""

    id: ThreadId
    title: str
    cwd: str
    last_activity: datetime | None
    archive_state: ArchiveState
    parent_id: ThreadId | None
    root_id: ThreadId | None
    children: tuple[ThreadId, ...]
    stored_bytes: int | None
    agent_status: str | None
    issue: str | None
    source: SessionSource = SessionSource.UNKNOWN
    known_stored_bytes: int = 0
    physical_rollouts: tuple[StoredRollout, ...] = ()
    physical_complete: bool = False
    reported_history_mode: HistoryMode = HistoryMode.UNKNOWN
    shared_retained_bytes: int | None = None
    conditional_reclaim_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SessionCatalogue:
    """Detached advisory rows and explicit observation uncertainties."""

    entries: tuple[SessionEntry, ...]
    diagnostics: tuple[str, ...]
    source_changed: bool
    physical_complete: bool = False


def _descend(descriptor: int, parts: tuple[str, ...]) -> int:
    """Return an owned directory descriptor reached without following symlinks."""
    current = os.dup(descriptor)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    return current


@dataclass(frozen=True, slots=True)
class _Namespace:
    rollouts: tuple[StoredRollout, ...]
    complete: bool
    ambiguous: frozenset[ThreadId]
    diagnostics: tuple[str, ...]


def _physical_namespace(home: Path) -> _Namespace:  # noqa: C901
    """Observe finite physical membership and stored extents without opening histories."""
    diagnostics: list[str] = []
    items: list[StoredRollout] = []
    anchor = os.open(home.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = _descend(anchor, home.relative_to(home.anchor).parts)
    finally:
        os.close(anchor)
    try:
        with metadata_walk.Walker() as walker:
            for name in ("sessions", "archived_sessions"):
                try:
                    root = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                except FileNotFoundError:
                    continue
                except OSError:
                    diagnostics.append("physical_namespace_unavailable")
                    continue
                with walker.files(root, home / name, diagnostics) as files:
                    for path, value in files:
                        representation = (
                            PhysicalRepresentation.ZSTD
                            if path.name.endswith(".jsonl.zst")
                            else PhysicalRepresentation.PLAIN
                        )
                        try:
                            ownership = ownership_from_path(path, representation)
                        except ValueError:
                            if value.st_size:
                                diagnostics.append("physical_filename_unknown")
                            continue
                        items.append(
                            StoredRollout(
                                ownership,
                                path,
                                representation,
                                FileStatIdentity(
                                    value.st_dev,
                                    value.st_ino,
                                    value.st_size,
                                    value.st_mtime_ns,
                                    value.st_ctime_ns,
                                ),
                            )
                        )
    finally:
        os.close(descriptor)
    ambiguous: set[ThreadId] = set()
    excluded_paths: set[Path] = set()
    by_rollout: dict[RolloutId, StoredRollout] = {}
    by_descriptor: dict[tuple[int, int], StoredRollout] = {}
    for item in items:
        key = item.stat.dev, item.stat.ino
        for previous in (by_rollout.get(item.ownership.rollout_id), by_descriptor.get(key)):
            if previous is not None:
                ambiguous.update((previous.ownership.thread_id, item.ownership.thread_id))
                excluded_paths.update((previous.path, item.path))
        by_rollout[item.ownership.rollout_id] = item
        by_descriptor[key] = item
    if ambiguous:
        diagnostics.append("ambiguous_physical_identity")
    return _Namespace(
        tuple(item for item in items if item.path not in excluded_paths),
        not any(value != "ambiguous_physical_identity" for value in diagnostics),
        frozenset(ambiguous),
        tuple(dict.fromkeys(diagnostics)),
    )


def _current_matches(declared: str, owner: ThreadId, items: tuple[StoredRollout, ...]) -> bool:
    """Match only namespace-validated owner paths and their canonical encoding aliases."""
    raw = Path(declared)
    for item in items:
        if item.ownership.thread_id != owner:
            continue
        if raw == item.path:
            return True
        alternate = (
            item.path.with_suffix("")
            if item.representation is PhysicalRepresentation.ZSTD
            else item.path.with_name(item.path.name + ".zst")
        )
        if raw == alternate:
            return True
    return False


def _rows(
    rows: list[tuple[object, ...]], home: Path, diagnostics: list[str]
) -> tuple[list[SessionEntry], bool]:
    entries = []
    namespace = _physical_namespace(home)
    diagnostics.extend(namespace.diagnostics)
    by_owner: dict[ThreadId, list[StoredRollout]] = {}
    for item in namespace.rollouts:
        by_owner.setdefault(item.ownership.thread_id, []).append(item)
    for row in rows:
        try:
            identifier = ThreadId.parse(row[0])
            if not (
                isinstance(row[1], str) and isinstance(row[2], str) and isinstance(row[6], str)
            ):
                diagnostics.append("invalid_thread_metadata")
                continue
            archive = _archive_state(row[5])
            milliseconds = _milliseconds(row[3], row[4])
            if archive is None:
                diagnostics.append("invalid_thread_metadata")
                continue
            activity = (
                None if milliseconds is None else datetime.fromtimestamp(milliseconds / 1000, UTC)
            )
            items = tuple(
                sorted(by_owner.get(identifier, ()), key=lambda item: item.ownership.rollout_id)
            )
            known = sum(item.stat.stored_bytes for item in items)
            complete = (
                namespace.complete
                and identifier not in namespace.ambiguous
                and _current_matches(row[6], identifier, items)
            )
            mode = HistoryMode(row[8]) if row[8] in {"legacy", "paginated"} else HistoryMode.UNKNOWN
            entries.append(
                SessionEntry(
                    identifier,
                    row[1],
                    row[2],
                    activity,
                    archive,
                    None,
                    None,
                    (),
                    known if complete else None,
                    None,
                    None if complete else "physical_history_incomplete",
                    _source(row[7]),
                    known,
                    items,
                    complete,
                    mode,
                )
            )
        except ValueError, TypeError, OverflowError, OSError:
            diagnostics.append("invalid_thread_metadata")
    return entries, namespace.complete and not namespace.ambiguous


def _topology(  # noqa: C901, PLR0912, PLR0915
    edges: tuple[tuple[object, ...], ...], entries: list[SessionEntry], diagnostics: list[str]
) -> tuple[SessionEntry, ...]:
    identifiers = {entry.id for entry in entries}
    parents: dict[ThreadId, set[ThreadId]] = {identifier: set() for identifier in identifiers}
    children: dict[ThreadId, set[ThreadId]] = {identifier: set() for identifier in identifiers}
    statuses: dict[ThreadId, str] = {}
    invalid: set[ThreadId] = set()
    for raw_parent, raw_child, status in edges:
        try:
            child = ThreadId.parse(raw_child)
        except TypeError, ValueError:
            diagnostics.append("invalid_spawn_child")
            # An unidentifiable child could belong to any visible work.
            invalid.update(identifiers)
            continue
        try:
            parent = ThreadId.parse(raw_parent)
        except TypeError, ValueError:
            invalid.add(child)
            diagnostics.append("invalid_spawn_parent")
            continue
        if child not in identifiers or parent not in identifiers:
            diagnostics.append("dangling_spawn_relation")
            invalid.update({child, parent} & identifiers)
        if child in identifiers:
            parents[child].add(parent)
            if status not in {"active", "open", "closed"}:
                invalid.add(child)
                diagnostics.append("invalid_spawn_status")
            else:
                statuses[child] = status
        if parent in identifiers:
            children[parent].add(child)
    pending = list(invalid)
    while pending:
        identifier = pending.pop()
        for neighbour in parents.get(identifier, set()) | children.get(identifier, set()):
            if neighbour not in invalid:
                invalid.add(neighbour)
                pending.append(neighbour)
    result = []
    for entry in entries:
        current = entry.id
        visited: set[ThreadId] = set()
        root = None
        while current in identifiers and current not in visited and current not in invalid:
            visited.add(current)
            incoming = parents[current]
            if not incoming:
                root = current
                break
            if len(incoming) != 1:
                break
            current = next(iter(incoming))
        issue = entry.issue
        if root is None:
            issue = "invalid_spawn_topology"
            diagnostics.append(issue)
        incoming = parents[entry.id]
        result.append(
            replace(
                entry,
                parent_id=next(iter(incoming)) if len(incoming) == 1 else None,
                root_id=root,
                children=tuple(sorted(children[entry.id])),
                agent_status=statuses.get(entry.id),
                issue=issue,
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda entry: (
                -(entry.last_activity.timestamp())
                if entry.last_activity is not None
                else float("inf"),
                str(entry.id),
            ),
        )
    )


def read_session_catalogue(home: Path) -> SessionCatalogue:
    """Read one live metadata transaction, then stat rollouts without opening history."""
    diagnostics: list[str] = []
    try:
        home = home.resolve(strict=True)
        sources = tuple(home.glob("state_*.sqlite"))
        if len(sources) != 1:
            return SessionCatalogue((), ("expected_one_state_database",), source_changed=False)
        with read_transaction(sources[0]) as connection:
            if not _supported_schema(connection):
                return SessionCatalogue((), ("unsupported_state_schema",), source_changed=False)
            rows = connection.execute(
                "SELECT id,title,cwd,updated_at_ms,updated_at,archived,rollout_path,source,"
                "history_mode "
                "FROM threads"
            ).fetchall()
            edges = tuple(
                connection.execute(
                    "SELECT parent_thread_id,child_thread_id,status FROM thread_spawn_edges"
                )
            )
        observed, complete = _rows(rows, home, diagnostics)
        entries = _topology(edges, observed, diagnostics)
        return SessionCatalogue(
            entries,
            tuple(dict.fromkeys(diagnostics)),
            source_changed=False,
            physical_complete=complete and all(entry.physical_complete for entry in entries),
        )
    except OSError, sqlite3.Error, ValueError, TypeError, OverflowError:
        return SessionCatalogue(
            (), (*dict.fromkeys(diagnostics), "catalogue_unavailable"), source_changed=False
        )
