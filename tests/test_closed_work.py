# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from codex_session_gc.domain import (
    ActivityState,
    ArchiveState,
    GraphSnapshot,
    GuardianAssociation,
    HistoryProviderReference,
    LastActivity,
    OwnershipEdge,
    RolloutId,
    RolloutRecord,
    SpawnEdge,
    SpawnEdgeStatus,
    ThreadId,
    ThreadOrigin,
    ThreadRecord,
    UtcCutoff,
)
from codex_session_gc.planning.closed_work import ClosedWorkReason, select_closed_work

CUTOFF = UtcCutoff(datetime(2026, 8, 1, tzinfo=UTC))


def _thread(number: int) -> ThreadId:
    return ThreadId(UUID(int=number))


def _graph() -> GraphSnapshot:
    return GraphSnapshot.from_records(
        threads=[
            ThreadRecord(
                _thread(i),
                ThreadOrigin.MAIN if i == 1 else ThreadOrigin.SUBAGENT,
                ActivityState.UNKNOWN,
                ArchiveState.NOT_ARCHIVED,
                LastActivity(datetime(2026, 1, 1, tzinfo=UTC)),
            )
            for i in range(1, 5)
        ],
        rollouts=[
            RolloutRecord(RolloutId(UUID(int=i)), ArchiveState.NOT_ARCHIVED) for i in range(1, 5)
        ],
        ownership_edges=[OwnershipEdge(_thread(i), RolloutId(UUID(int=i))) for i in range(1, 5)],
        spawn_edges=[
            SpawnEdge(_thread(1), _thread(2), SpawnEdgeStatus.CLOSED),
            SpawnEdge(_thread(1), _thread(3), SpawnEdgeStatus.OPEN),
        ],
    )


def test_explicit_main_and_all_descendants_with_conditional_closure() -> None:
    result = select_closed_work(_graph(), CUTOFF, frozenset({_thread(1)}))
    assert result.ownership_scope == frozenset(map(_thread, (1, 2, 3)))
    assert result.conditional_candidates == result.ownership_scope
    assert result.closure_requirements == result.ownership_scope
    assert result.deletion_roots == frozenset({_thread(1)})
    assert not result.errors


def test_subset_cannot_expand_to_siblings_or_parent() -> None:
    result = select_closed_work(
        _graph(), CUTOFF, frozenset({_thread(1)}), selected_threads=frozenset({_thread(2)})
    )
    assert result.conditional_candidates == frozenset({_thread(2)})
    assert result.ownership_scope == frozenset(map(_thread, (1, 2, 3)))
    assert result.deletion_roots == frozenset({_thread(2)})


@pytest.mark.parametrize("activity", [ActivityState.ACTIVE, ActivityState.IDLE_LOADED])
def test_busy_sibling_blocks_whole_work_but_unrelated_activity_does_not(
    activity: ActivityState,
) -> None:
    graph = _graph()
    unrelated = replace(
        graph,
        threads=tuple(
            replace(item, activity=activity) if item.id == _thread(4) else item
            for item in graph.threads
        ),
    )
    assert select_closed_work(unrelated, CUTOFF, frozenset({_thread(1)})).conditional_candidates
    busy = replace(
        graph,
        threads=tuple(
            replace(item, activity=activity) if item.id == _thread(3) else item
            for item in graph.threads
        ),
    )
    result = select_closed_work(busy, CUTOFF, frozenset({_thread(1)}))
    assert not result.conditional_candidates
    assert {item.reason for item in result.retained} == {ClosedWorkReason.BUSY_WORK}


def test_recent_descendant_retains_cascading_parent_only() -> None:
    graph = _graph()
    graph = replace(
        graph,
        threads=tuple(
            replace(item, last_activity=LastActivity(CUTOFF.timestamp))
            if item.id == _thread(3)
            else item
            for item in graph.threads
        ),
    )
    result = select_closed_work(graph, CUTOFF, frozenset({_thread(1)}))
    assert result.conditional_candidates == frozenset({_thread(2)})
    assert {item.thread_id: item.reason for item in result.retained} == {
        _thread(1): ClosedWorkReason.DESCENDANT,
        _thread(3): ClosedWorkReason.AGE,
    }


def test_external_consumer_protects_provider_and_ancestor() -> None:
    graph = replace(
        _graph(),
        history_provider_references=(
            HistoryProviderReference(RolloutId(UUID(int=4)), RolloutId(UUID(int=2))),
        ),
    )
    result = select_closed_work(graph, CUTOFF, frozenset({_thread(1)}))
    assert result.conditional_candidates == frozenset({_thread(3)})
    assert next(
        item for item in result.retained if item.thread_id == _thread(2)
    ).witness == _thread(4)


def test_known_fork_and_archive_are_not_categorical_exemptions() -> None:
    graph = _graph()
    graph = replace(
        graph,
        threads=tuple(
            replace(item, origin=ThreadOrigin.USER_FORK, archive_state=ArchiveState.ARCHIVED)
            if item.id == _thread(2)
            else item
            for item in graph.threads
        ),
        rollouts=tuple(
            replace(item, archive_state=ArchiveState.ARCHIVED)
            if item.id == RolloutId(UUID(int=2))
            else item
            for item in graph.rollouts
        ),
    )
    assert (
        _thread(2)
        in select_closed_work(graph, CUTOFF, frozenset({_thread(1)})).conditional_candidates
    )


@pytest.mark.parametrize("status", list(SpawnEdgeStatus))
def test_lifecycle_flag_does_not_infer_activity_or_ownership(status: SpawnEdgeStatus) -> None:
    graph = _graph()
    graph = replace(
        graph, spawn_edges=tuple(replace(item, status=status) for item in graph.spawn_edges)
    )
    result = select_closed_work(graph, CUTOFF, frozenset({_thread(1)}))
    assert result.closure_requirements == frozenset(map(_thread, (1, 2, 3)))
    assert result.conditional_candidates == result.ownership_scope


@pytest.mark.parametrize(
    ("edges", "error"),
    [
        ((SpawnEdge(_thread(2), _thread(1)),), "cyclic_spawn_topology"),
        ((SpawnEdge(_thread(4), _thread(2)),), "ambiguous_multiple_spawn_parents"),
    ],
)
def test_ambiguous_topology_refuses(edges: tuple[SpawnEdge, ...], error: str) -> None:
    graph = _graph()
    result = select_closed_work(
        replace(graph, spawn_edges=graph.spawn_edges + edges), CUTOFF, frozenset({_thread(1)})
    )
    assert error in result.errors
    assert not result.conditional_candidates


def test_unknown_root_or_outside_subset_refuses() -> None:
    assert select_closed_work(_graph(), CUTOFF, frozenset({_thread(9)})).errors == (
        "unknown_closed_work_root",
    )
    result = select_closed_work(
        _graph(), CUTOFF, frozenset({_thread(1)}), selected_threads=frozenset({_thread(4)})
    )
    assert result.errors == ("selected_thread_outside_closed_work",)
    assert not result.conditional_candidates


def test_caller_cutoff_changes_eligibility_without_changing_ownership() -> None:
    earlier = UtcCutoff(datetime(2025, 1, 1, tzinfo=UTC))
    result = select_closed_work(_graph(), earlier, frozenset({_thread(1)}))
    assert not result.conditional_candidates
    assert result.ownership_scope == frozenset(map(_thread, (1, 2, 3)))
    assert {item.reason for item in result.retained} == {ClosedWorkReason.AGE}


def test_busy_independent_work_does_not_block_another_selected_work() -> None:
    graph = _graph()
    graph = replace(
        graph,
        threads=tuple(
            replace(item, activity=ActivityState.ACTIVE) if item.id == _thread(4) else item
            for item in graph.threads
        ),
    )
    result = select_closed_work(graph, CUTOFF, frozenset({_thread(1), _thread(4)}))
    assert result.conditional_candidates == frozenset(map(_thread, (1, 2, 3)))
    assert result.ownership_scope == frozenset(map(_thread, (1, 2, 3, 4)))


def test_retention_propagates_through_multiple_history_providers() -> None:
    graph = replace(
        _graph(),
        history_provider_references=(
            HistoryProviderReference(RolloutId(UUID(int=4)), RolloutId(UUID(int=3))),
            HistoryProviderReference(RolloutId(UUID(int=3)), RolloutId(UUID(int=2))),
        ),
    )
    result = select_closed_work(graph, CUTOFF, frozenset({_thread(1)}))
    assert not result.conditional_candidates
    assert {item.thread_id for item in result.retained} == frozenset(map(_thread, (1, 2, 3)))


def test_explicit_parent_subset_refuses_cascade_into_unselected_child() -> None:
    result = select_closed_work(
        _graph(), CUTOFF, frozenset({_thread(1)}), selected_threads=frozenset({_thread(1)})
    )
    assert not result.conditional_candidates
    assert (
        next(item for item in result.retained if item.thread_id == _thread(1)).reason
        is ClosedWorkReason.DESCENDANT
    )


def test_unrelated_spawn_cycle_does_not_refuse_selected_work() -> None:
    graph = _graph()
    extra = ThreadRecord(
        _thread(5),
        ThreadOrigin.SUBAGENT,
        ActivityState.UNKNOWN,
        ArchiveState.NOT_ARCHIVED,
        LastActivity(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    graph = replace(
        graph,
        threads=(*graph.threads, extra),
        spawn_edges=(
            *graph.spawn_edges,
            SpawnEdge(_thread(4), _thread(5)),
            SpawnEdge(_thread(5), _thread(4)),
        ),
    )
    result = select_closed_work(graph, CUTOFF, frozenset({_thread(1)}))
    assert not result.errors
    assert result.conditional_candidates == frozenset(map(_thread, (1, 2, 3)))


def test_known_child_is_not_an_independent_work_root() -> None:
    result = select_closed_work(_graph(), CUTOFF, frozenset({_thread(2)}))
    assert result.errors == ("closed_work_root_has_spawn_parent",)
    assert not result.conditional_candidates


def test_unrelated_cycle_does_not_hide_external_consumer_dependency() -> None:
    graph = _graph()
    extra = ThreadRecord(
        _thread(5),
        ThreadOrigin.SUBAGENT,
        ActivityState.UNKNOWN,
        ArchiveState.NOT_ARCHIVED,
        LastActivity(datetime(2026, 1, 1, tzinfo=UTC)),
    )
    graph = replace(
        graph,
        threads=(*graph.threads, extra),
        spawn_edges=(
            *graph.spawn_edges,
            SpawnEdge(_thread(4), _thread(5)),
            SpawnEdge(_thread(5), _thread(4)),
        ),
        history_provider_references=(
            HistoryProviderReference(RolloutId(UUID(int=4)), RolloutId(UUID(int=2))),
        ),
    )
    result = select_closed_work(graph, CUTOFF, frozenset({_thread(1)}))
    assert not result.errors
    assert result.conditional_candidates == frozenset({_thread(3)})
    assert next(
        item for item in result.retained if item.thread_id == _thread(2)
    ).witness == _thread(4)


def test_guardian_selection_does_not_require_associated_parent_to_be_closed_or_present() -> None:
    guardian = _thread(4)
    graph = _graph()
    graph = replace(
        graph,
        threads=tuple(
            replace(record, origin=ThreadOrigin.GUARDIAN)
            if record.id == guardian
            else replace(record, activity=ActivityState.ACTIVE)
            if record.id == _thread(1)
            else record
            for record in graph.threads
        ),
        guardian_associations=(GuardianAssociation(guardian, _thread(1)),),
    )
    selected = select_closed_work(graph, CUTOFF, frozenset({guardian}))
    assert selected.conditional_candidates == frozenset({guardian})
    assert selected.ownership_scope == selected.closure_requirements == frozenset({guardian})
    assert selected.guardian_associations == graph.guardian_associations
    parent_selected = select_closed_work(graph, CUTOFF, frozenset({_thread(1)}))
    assert guardian not in parent_selected.ownership_scope
    assert guardian not in parent_selected.requested_threads
    absent = replace(graph, guardian_associations=(GuardianAssociation(guardian, _thread(99)),))
    assert select_closed_work(
        absent, CUTOFF, frozenset({guardian})
    ).conditional_candidates == frozenset({guardian})


def test_guardian_missing_association_or_actual_spawn_parent_is_not_selected() -> None:
    guardian = _thread(4)
    graph = replace(
        _graph(),
        threads=tuple(
            replace(record, origin=ThreadOrigin.GUARDIAN) if record.id == guardian else record
            for record in _graph().threads
        ),
    )
    selected = select_closed_work(graph, CUTOFF, frozenset({guardian}))
    assert not selected.conditional_candidates
    assert selected.retained[0].reason is ClosedWorkReason.GUARDIAN_ASSOCIATION
    mixed = replace(graph, spawn_edges=(*graph.spawn_edges, SpawnEdge(_thread(1), guardian)))
    for root in (_thread(1), guardian):
        selected = select_closed_work(mixed, CUTOFF, frozenset({root}))
        assert "guardian_spawn_topology_conflict" in selected.errors
        assert not selected.conditional_candidates
        if root != guardian:
            assert guardian not in selected.ownership_scope
