# Copyright (c) 2026

"""Select an explicit closed-work scenario without certifying runtime closure."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from codex_session_gc.domain import (
    ActivityState,
    ArchiveState,
    GraphSnapshot,
    GuardianAssociation,
    PhysicalInventory,
    RolloutId,
    ThreadId,
    ThreadOrigin,
    ThreadRecord,
    UtcCutoff,
)
from codex_session_gc.planning.physical_lineage import normalize_history_providers


class ClosedWorkReason(StrEnum):
    """Why an explicitly scoped thread cannot be selected for deletion."""

    NOT_SELECTED = "not_selected"
    BUSY_WORK = "loaded_or_active_work_member"
    PROTECTED_ORIGIN = "unknown_origin"
    ARCHIVE = "unknown_archive"
    AGE = "recent_or_unknown_last_activity"
    NO_HISTORY = "owned_history_unavailable"
    DESCENDANT = "retained_descendant"
    PROVIDER = "required_by_retained_consumer"
    GUARDIAN_ASSOCIATION = "unknown_guardian_association"


@dataclass(frozen=True, slots=True)
class ClosedWorkRetention:
    """A scoped retained thread and its concrete causal witness."""

    thread_id: ThreadId
    reason: ClosedWorkReason
    witness: ThreadId | None = None


@dataclass(frozen=True, slots=True)
class ClosedWorkSelection:
    """Explicit ownership and deletion scopes, never a mutation authorization."""

    roots: frozenset[ThreadId]
    ownership_scope: frozenset[ThreadId]
    requested_threads: frozenset[ThreadId]
    conditional_candidates: frozenset[ThreadId]
    deletion_roots: frozenset[ThreadId]
    closure_requirements: frozenset[ThreadId]
    retained: tuple[ClosedWorkRetention, ...]
    errors: tuple[str, ...]
    guardian_associations: tuple[GuardianAssociation, ...] = ()


def select_closed_work(
    snapshot: GraphSnapshot,
    cutoff: UtcCutoff,
    roots: frozenset[ThreadId],
    *,
    selected_threads: frozenset[ThreadId] | None = None,
    physical_inventory: PhysicalInventory | None = None,
) -> ClosedWorkSelection:
    """Filter chosen work, preserving retained descendants and history consumers.

    Roots express the caller's selected closed-session scenario. Neither that
    assertion, an inactive observation nor a Closed spawn edge certifies native
    ownership release. All scoped members still require native closure checks.
    """
    records = {item.id: item for item in snapshot.threads}
    errors = []
    if not roots:
        errors.append("closed_work_roots_required")
    if roots - records.keys():
        errors.append("unknown_closed_work_root")
    children, parents = _spawn_relations(snapshot, records)
    memberships = {root: _descendants(root, children) for root in roots if root in records}
    scope = frozenset(item for members in memberships.values() for item in members)
    if any(
        records[edge.child_thread_id].origin is ThreadOrigin.GUARDIAN
        and (edge.child_thread_id in scope or edge.parent_thread_id in scope)
        for edge in snapshot.spawn_edges
    ):
        errors.append("guardian_spawn_topology_conflict")
    errors.extend(
        _topology_errors(
            {item: children[item] & scope for item in scope},
            {item: parents[item] for item in scope},
        )
    )
    if any(parents[root] for root in roots if root in records):
        errors.append("closed_work_root_has_spawn_parent")
    requested = scope if selected_threads is None else selected_threads
    if requested - scope:
        errors.append("selected_thread_outside_closed_work")
    if errors:
        return ClosedWorkSelection(
            roots, scope, requested, frozenset(), frozenset(), scope, (), tuple(errors)
        )
    retained = _busy_work(memberships, records)
    associations = {item.guardian_thread_id: item for item in snapshot.guardian_associations}
    owned: dict[ThreadId, set[RolloutId]] = {item: set() for item in records}
    rollout_records = {item.id: item for item in snapshot.rollouts}
    for ownership in snapshot.ownership_edges:
        owned[ownership.thread_id].add(ownership.rollout_id)
    for item in sorted(scope):
        reason = _guardian_reason(
            records[item], association_known=item in associations, selected=item in requested
        ) or _local_reason(
            records[item],
            cutoff,
            selected=item in requested,
            has_history=bool(owned[item]),
            unknown_rollout_archive=any(
                rollout_records[rollout].archive_state is ArchiveState.UNKNOWN
                for rollout in owned[item]
            ),
        )
        if reason is not None:
            retained.setdefault(item, ClosedWorkRetention(item, reason))
    candidates = set(scope - retained.keys())
    _propagate_retention(snapshot, candidates, retained, physical_inventory)
    deletion_roots = frozenset(item for item in candidates if not (parents[item] & candidates))
    return ClosedWorkSelection(
        roots,
        scope,
        requested,
        frozenset(candidates),
        deletion_roots,
        scope,
        tuple(retained[item] for item in sorted(retained)),
        (),
        tuple(associations[item] for item in sorted(candidates) if item in associations),
    )


def _spawn_relations(
    snapshot: GraphSnapshot, records: dict[ThreadId, ThreadRecord]
) -> tuple[dict[ThreadId, set[ThreadId]], dict[ThreadId, set[ThreadId]]]:
    """Project actual spawn ownership without turning Guardians into descendants."""
    children: dict[ThreadId, set[ThreadId]] = {item: set() for item in records}
    parents: dict[ThreadId, set[ThreadId]] = {item: set() for item in records}
    for edge in snapshot.spawn_edges:
        if records[edge.child_thread_id].origin is ThreadOrigin.GUARDIAN:
            continue
        children[edge.parent_thread_id].add(edge.child_thread_id)
        parents[edge.child_thread_id].add(edge.parent_thread_id)
    return children, parents


def _descendants(root: ThreadId, children: dict[ThreadId, set[ThreadId]]) -> frozenset[ThreadId]:
    members: set[ThreadId] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        if current not in members:
            members.add(current)
            pending.extend(children[current])
    return frozenset(members)


def _topology_errors(
    children: dict[ThreadId, set[ThreadId]], parents: dict[ThreadId, set[ThreadId]]
) -> list[str]:
    """Reject ambiguous or cyclic ownership before interpreting work scope."""
    errors = []
    if any(len(items) > 1 for items in parents.values()):
        errors.append("ambiguous_multiple_spawn_parents")
    # Kahn traversal rejects cycles without recursion or trusting lifecycle labels.
    indegree = {item: len(items & children.keys()) for item, items in parents.items()}
    pending = [item for item, count in indegree.items() if not count]
    visited = 0
    while pending:
        current = pending.pop()
        visited += 1
        for child in children[current]:
            indegree[child] -= 1
            if not indegree[child]:
                pending.append(child)
    if visited != len(parents):
        errors.append("cyclic_spawn_topology")
    return errors


def _guardian_reason(
    record: ThreadRecord, *, association_known: bool, selected: bool
) -> ClosedWorkReason | None:
    """A Guardian cannot be selected by guessing its historical association."""
    if selected and record.origin is ThreadOrigin.GUARDIAN and not association_known:
        return ClosedWorkReason.GUARDIAN_ASSOCIATION
    return None


def _local_reason(
    record: ThreadRecord,
    cutoff: UtcCutoff,
    *,
    selected: bool,
    has_history: bool,
    unknown_rollout_archive: bool,
) -> ClosedWorkReason | None:
    """Apply observed eligibility without role or known-archive exclusions."""
    if not selected:
        return ClosedWorkReason.NOT_SELECTED
    if record.origin is ThreadOrigin.UNKNOWN:
        return ClosedWorkReason.PROTECTED_ORIGIN
    if record.archive_state is ArchiveState.UNKNOWN or unknown_rollout_archive:
        return ClosedWorkReason.ARCHIVE
    if record.last_activity.timestamp is None or not cutoff.is_strictly_before(
        record.last_activity.timestamp
    ):
        return ClosedWorkReason.AGE
    if not has_history:
        return ClosedWorkReason.NO_HISTORY
    return None


def _propagate_retention(
    snapshot: GraphSnapshot,
    candidates: set[ThreadId],
    retained: dict[ThreadId, ClosedWorkRetention],
    physical_inventory: PhysicalInventory | None,
) -> None:
    """Close retention over native cascades and retained-consumer dependencies."""
    ownership = (
        snapshot.ownership_edges
        if physical_inventory is None
        else tuple(item.ownership for item in physical_inventory.rollouts)
    )
    references = (
        snapshot.history_provider_references
        if physical_inventory is None
        else normalize_history_providers(physical_inventory)
    )
    owner = {edge.rollout_id: edge.thread_id for edge in ownership}
    dependents: dict[ThreadId, list[tuple[ThreadId, ClosedWorkReason]]] = {}
    for edge in snapshot.spawn_edges:
        dependents.setdefault(edge.child_thread_id, []).append(
            (edge.parent_thread_id, ClosedWorkReason.DESCENDANT)
        )
    for reference in references:
        dependents.setdefault(owner[reference.rollout_id], []).append(
            (owner[reference.provider_rollout_id], ClosedWorkReason.PROVIDER)
        )
    retained_owners = {item.id for item in snapshot.threads} | set(owner.values())
    pending = deque(sorted(retained_owners - candidates))
    while pending:
        witness = pending.popleft()
        for item, reason in dependents.get(witness, []):
            if item in candidates:
                candidates.remove(item)
                retained[item] = ClosedWorkRetention(item, reason, witness)
                pending.append(item)


def _busy_work(
    memberships: dict[ThreadId, frozenset[ThreadId]], records: dict[ThreadId, ThreadRecord]
) -> dict[ThreadId, ClosedWorkRetention]:
    """A busy member prevents deletion anywhere in that selected work."""
    retained: dict[ThreadId, ClosedWorkRetention] = {}
    for members in memberships.values():
        busy = sorted(
            item
            for item in members
            if records[item].activity in {ActivityState.ACTIVE, ActivityState.IDLE_LOADED}
        )
        if busy:
            for item in members:
                retained[item] = ClosedWorkRetention(item, ClosedWorkReason.BUSY_WORK, busy[0])
    return retained
