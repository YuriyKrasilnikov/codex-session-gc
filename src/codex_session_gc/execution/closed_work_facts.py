# Copyright (c) 2026
# ruff: noqa: EM101, TRY003, S608, SLF001

"""Observe selected work metadata without reading history bodies.

These finite facts do not establish global incoming-reference completeness. The
native operation must walk bounded namespace headers under ownership before it
can approve deletion. Selection here remains conditional on that validation.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from codex_session_gc.domain import (
    ActivityState,
    ArchiveState,
    GraphSnapshot,
    GuardianAssociation,
    LastActivity,
    RolloutRecord,
    SpawnEdge,
    SpawnEdgeStatus,
    ThreadId,
    ThreadOrigin,
    ThreadRecord,
    UtcCutoff,
)
from codex_session_gc.execution import closed_work_request as collector
from codex_session_gc.execution.physical_inventory import observe_inventory
from codex_session_gc.inventory.observation import RolloutObservation


def _source_facts(value: object) -> tuple[ThreadOrigin, ThreadId | None]:
    """Validate an explicit spawn variant before classifying its parent relation."""
    if value in ("cli", "vscode", "exec"):
        return ThreadOrigin.MAIN, None
    if isinstance(value, dict):
        subagent = value.get("subagent")
        if isinstance(subagent, dict) and subagent.get("other") == "guardian":
            if value != {"subagent": {"other": "guardian"}}:
                raise collector.PreparationRefused("The Guardian source variant is ambiguous.")
            return ThreadOrigin.GUARDIAN, None
        if isinstance(subagent, dict) and "thread_spawn" in subagent:
            if set(value) != {"subagent"} or set(subagent) != {"thread_spawn"}:
                raise collector.PreparationRefused("The session spawn source variant is ambiguous.")
            spawn = subagent["thread_spawn"]
            if not isinstance(spawn, dict):
                raise collector.PreparationRefused("The session spawn source is malformed.")
            try:
                parent = ThreadId.parse(spawn.get("parent_thread_id"))
            except (TypeError, ValueError) as error:
                raise collector.PreparationRefused(
                    "The session spawn source requires a valid parent_thread_id."
                ) from error
            return ThreadOrigin.SUBAGENT, parent
    return ThreadOrigin.UNKNOWN, None


def _requested_scope(
    roots: frozenset[ThreadId], edges: tuple[SpawnEdge, ...], records: dict[ThreadId, ThreadRecord]
) -> frozenset[ThreadId]:
    """Follow actual spawn ownership without importing Guardian associations."""
    children: dict[ThreadId, list[ThreadId]] = {}
    for edge in edges:
        if records[edge.child_thread_id].origin is not ThreadOrigin.GUARDIAN:
            children.setdefault(edge.parent_thread_id, []).append(edge.child_thread_id)
    scope = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current not in scope:
            scope.add(current)
            pending.extend(children.get(current, []))
    return frozenset(scope)


def _guardian_association(identifier: ThreadId, metadata: dict[str, object]) -> GuardianAssociation:
    """Read a historical header association without looking up or closing its parent."""
    try:
        return GuardianAssociation(identifier, ThreadId.parse(metadata.get("parent_thread_id")))
    except (TypeError, ValueError) as error:
        raise collector.PreparationRefused(
            "Selected Guardian requires a valid, distinct historical parent association."
        ) from error


def _selected_provenance(
    identifier: ThreadId, metadata: dict[str, object], sql_source: object, origin: ThreadOrigin
) -> tuple[object, ThreadId | None, GuardianAssociation | None]:
    source = metadata.get("source")
    header_origin, parent = _source_facts(source)
    if ThreadOrigin.GUARDIAN not in (origin, header_origin):
        return source, parent, None
    if collector._session_source(sql_source) != source:
        raise collector.PreparationRefused("Selected Guardian SQL/header source disagrees.")
    return source, parent, _guardian_association(identifier, metadata)


def read_closed_work_facts(  # noqa: C901, PLR0912, PLR0915
    home: Path,
    cutoff: UtcCutoff,
    roots: frozenset[ThreadId],
    selected: frozenset[ThreadId] | None,
    limits: collector.CollectionLimits,
) -> collector.ClosedWorkFacts:
    """Read scoped SQL and selected headers once; leave global proof to native."""
    if not roots:
        raise collector.PreparationRefused("Closed-work roots are required.")
    collector._absolute(home)
    budget = collector.CollectionBudget(limits, time.monotonic() + limits.deadline_seconds)
    values = tuple(str(item.value) for item in sorted(roots))
    marks = ",".join("(?)" for _ in values)
    with collector._read_transaction(home / collector.DatabaseKind.STATE, budget) as connection:
        scope = tuple(
            row[0]
            for row in connection.execute(
                f"WITH RECURSIVE scope(id) AS (VALUES {marks} UNION "
                "SELECT child_thread_id FROM thread_spawn_edges "
                "JOIN scope ON parent_thread_id=scope.id) "
                "SELECT id FROM scope",
                values,
            )
        )
        marks = ",".join("?" for _ in scope)
        edges = tuple(
            connection.execute(
                f"SELECT parent_thread_id,child_thread_id,status FROM thread_spawn_edges "
                f"WHERE parent_thread_id IN ({marks}) OR child_thread_id IN ({marks})",
                scope + scope,
            )
        )
        connection.row_factory = sqlite3.Row
        rows = {
            row["id"]: row
            for row in connection.execute(
                "SELECT id,source,updated_at_ms,archived,rollout_path "
                f"FROM threads WHERE id IN ({marks})",
                scope,
            )
        }
        current_rows = {
            row[0]: {"rollout_path": row[1]}
            for row in connection.execute("SELECT id,rollout_path FROM threads")
        }
    if any(value not in rows for value in values):
        raise collector.PreparationRefused("A required closed-work root state record is absent.")
    records = {}
    for value in set(scope) | {item for edge in edges for item in edge[:2]}:
        identifier = ThreadId.parse(value)
        row = rows.get(value)
        origin = ThreadOrigin.UNKNOWN
        archive = ArchiveState.UNKNOWN
        timestamp = None
        if row is not None:
            origin, _ = _source_facts(collector._session_source(row["source"]))
            archive = {0: ArchiveState.NOT_ARCHIVED, 1: ArchiveState.ARCHIVED}.get(
                row["archived"], ArchiveState.UNKNOWN
            )
            updated = row["updated_at_ms"]
            if type(updated) is int:
                timestamp = datetime.fromtimestamp(updated // 1000, UTC) + timedelta(
                    milliseconds=updated % 1000
                )
        records[identifier] = ThreadRecord(
            identifier, origin, ActivityState.UNKNOWN, archive, LastActivity(timestamp)
        )
    spawn_edges = tuple(
        SpawnEdge(ThreadId.parse(p), ThreadId.parse(c), SpawnEdgeStatus(status))
        for p, c, status in edges
    )
    ownership_scope = _requested_scope(roots, spawn_edges, records)
    # The planner reports foreign requested IDs. Observe provenance only for
    # members of this work, whose scoped SQL records can supply that provenance.
    requested = ownership_scope if selected is None else selected & ownership_scope
    inventory, current_headers = observe_inventory(home, current_rows, frozenset(requested), budget)
    observations = []
    associations = []
    pointers = {pointer.ownership.thread_id: pointer for pointer in inventory.current_pointers}
    physicals = {item.ownership.rollout_id: item for item in inventory.rollouts}
    for identifier in sorted(requested):
        pointer = pointers.get(identifier)
        if pointer is None:
            continue
        current = physicals.get(pointer.ownership.rollout_id)
        if current is None or current.physical is None:
            continue
        header = current_headers[current.physical.path]
        metadata = header.metadata
        src, parent, association = _selected_provenance(
            identifier, metadata, rows[str(identifier.value)]["source"], records[identifier].origin
        )
        guardian_parent = None if association is None else association.associated_parent_thread_id
        if association is not None:
            associations.append(association)
        if parent is None and guardian_parent is None:
            incoming = [
                edge.parent_thread_id for edge in spawn_edges if edge.child_thread_id == identifier
            ]
            if len(incoming) == 1:
                parent = incoming[0]
        for item in inventory.rollouts:
            if item.ownership.thread_id != identifier or item.physical is None:
                continue
            physical = item.physical
            observations.append(
                RolloutObservation(
                    physical.path,
                    item.ownership.rollout_id,
                    identifier,
                    ArchiveState.ARCHIVED
                    if physical.path.is_relative_to(home / "archived_sessions")
                    else ArchiveState.NOT_ARCHIVED,
                    physical.stat.stored_bytes,
                    None,
                    "guardian"
                    if guardian_parent
                    else "subagent"
                    if parent
                    else src
                    if isinstance(src, str)
                    else None,
                    parent,
                    None
                    if item.history_prefix is None
                    else item.history_prefix.provider_rollout_id,
                    guardian_parent,
                )
            )
    known = tuple(item for item in inventory.rollouts if item.ownership.thread_id in records)
    snapshot = GraphSnapshot(
        threads=tuple(records.values()),
        rollouts=tuple(
            RolloutRecord(
                item.ownership.rollout_id,
                ArchiveState.ARCHIVED
                if item.physical is not None
                and item.physical.path.is_relative_to(home / "archived_sessions")
                else ArchiveState.NOT_ARCHIVED,
            )
            for item in known
        ),
        spawn_edges=spawn_edges,
        ownership_edges=tuple(item.ownership for item in known),
        guardian_associations=tuple(associations),
    )
    return collector.ClosedWorkFacts(
        snapshot,
        tuple(observations),
        cutoff,
        inventory,
        tuple(current_headers.values()),
        incoming_references_complete=True,
    )
