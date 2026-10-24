# Copyright (c) 2026

"""Pure lineage normalization and integration with the existing retention policy."""

from dataclasses import replace
from datetime import UTC, datetime
from itertools import permutations, product
from pathlib import Path

import pytest

from codex_session_gc.domain import (
    ActivityState,
    ArchiveState,
    GraphSnapshot,
    HistoryMode,
    HistoryPrefix,
    HistoryProviderReference,
    InvalidGraphError,
    InventoryCompleteness,
    LastActivity,
    LogicalForkBoundary,
    PhysicalInventory,
    RolloutMetadata,
    RolloutRecord,
    SelectedPhysicalManifest,
    ThreadOrigin,
    ThreadRecord,
    UtcCutoff,
)
from codex_session_gc.planning.closed_work import ClosedWorkReason, select_closed_work
from codex_session_gc.planning.physical_lineage import normalize_history_providers
from tests.test_physical import _check, _metadata, _pointer, _rollout, _thread


def _inventory(*items: RolloutMetadata) -> PhysicalInventory:
    return PhysicalInventory(items, (), InventoryCompleteness.COMPLETE)


def _prefix(item: RolloutMetadata, provider: int) -> RolloutMetadata:
    return replace(item, history_prefix=HistoryPrefix(_rollout(provider), 19, 7))


def test_transitive_archive_providers_feed_existing_retention_policy() -> None:
    """The retained consumer protects both providers through the existing planner."""
    provider = _metadata(1, 1)
    intermediate = _prefix(_metadata(2, 2), 1)
    physical = intermediate.physical
    if physical is None:
        pytest.fail("Test provider identity is required.")
    intermediate = replace(
        intermediate,
        physical=replace(physical, path=Path("/synthetic/archived_sessions") / physical.path.name),
    )
    consumer = _prefix(_metadata(3, 3), 2)
    items = (provider, intermediate, consumer)
    references = normalize_history_providers(_inventory(*items))
    graph = GraphSnapshot.from_records(
        threads=[
            ThreadRecord(
                item.ownership.thread_id,
                ThreadOrigin.MAIN,
                ActivityState.UNKNOWN,
                ArchiveState.ARCHIVED if item is intermediate else ArchiveState.NOT_ARCHIVED,
                LastActivity(datetime(2026, 1, 1, tzinfo=UTC)),
            )
            for item in items
        ],
        rollouts=[
            RolloutRecord(
                item.ownership.rollout_id,
                ArchiveState.ARCHIVED if item is intermediate else ArchiveState.NOT_ARCHIVED,
            )
            for item in items
        ],
        ownership_edges=[item.ownership for item in items],
        history_provider_references=references,
    )
    result = select_closed_work(
        graph, UtcCutoff(datetime(2026, 8, 1, tzinfo=UTC)), frozenset({_thread(1), _thread(2)})
    )
    _check(condition=not result.conditional_candidates)
    reasons = {item.thread_id: item.reason for item in result.retained}
    _check(condition=reasons[_thread(1)] is ClosedWorkReason.PROVIDER)
    _check(condition=reasons[_thread(2)] is ClosedWorkReason.PROVIDER)


def test_revert_keeps_logical_fork_and_current_pointer_out_of_provider_identity() -> None:
    """One owner may have an old provider and a distinct current immutable rollout."""
    logical = LogicalForkBoundary(_thread(9), 51)
    old = replace(_metadata(1, 1), logical_fork=logical)
    current = replace(_prefix(_metadata(1, 2), 1), logical_fork=logical)
    retained = _prefix(_metadata(3, 3), 2)
    inventory = PhysicalInventory(
        (old, current, retained), (_pointer(current),), InventoryCompleteness.COMPLETE
    )
    references = normalize_history_providers(inventory)
    _check(
        condition=references
        == frozenset(
            {
                HistoryProviderReference(_rollout(2), _rollout(1)),
                HistoryProviderReference(_rollout(3), _rollout(2)),
            }
        )
    )
    _check(
        condition=SelectedPhysicalManifest((_thread(1),), inventory).physical_rollouts
        == (old, current)
    )
    _check(condition=old.logical_fork == current.logical_fork)


def test_missing_historical_logical_parent_does_not_create_physical_edge() -> None:
    """Logical ancestry alone neither requires a provider object nor adds an edge."""
    item = replace(_metadata(1, 1), logical_fork=LogicalForkBoundary(_thread(99), None))
    _check(condition=normalize_history_providers(_inventory(item)) == frozenset())


@pytest.mark.parametrize("kind", ["coverage", "metadata", "physical", "mode", "missing"])
def test_unknown_or_contradictory_graph_is_not_an_empty_dependency_graph(kind: str) -> None:
    """Missing evidence never becomes a successful observation of no references."""
    provider, consumer = _metadata(1, 1), _prefix(_metadata(2, 2), 1)
    coverage = InventoryCompleteness.COMPLETE
    if kind == "coverage":
        coverage = InventoryCompleteness.UNKNOWN
    elif kind == "metadata":
        provider = replace(provider, history_mode=HistoryMode.UNKNOWN)
    elif kind == "physical":
        provider = replace(provider, physical=None)
    elif kind == "mode":
        consumer = replace(consumer, history_mode=HistoryMode.LEGACY)
    else:
        consumer = _prefix(consumer, 99)
    with pytest.raises(InvalidGraphError):
        normalize_history_providers(PhysicalInventory((provider, consumer), (), coverage))


@pytest.mark.parametrize("providers", [(1,), (2, 1), (2, 3, 1)])
def test_self_and_transitive_cycles_refuse(providers: tuple[int, ...]) -> None:
    """A cycle cannot be interpreted as a complete readable lineage."""
    items = tuple(
        _prefix(_metadata(index, index), provider) for index, provider in enumerate(providers, 1)
    )
    with pytest.raises(InvalidGraphError):
        normalize_history_providers(_inventory(*items))


def test_normalization_is_order_independent_and_preserves_metadata_coordinates() -> None:
    """Permutation changes neither dependencies nor independent ordinal/byte values."""
    items = (_metadata(1, 1), _prefix(_metadata(2, 2), 1), _prefix(_metadata(3, 3), 2))
    expected = frozenset(
        {
            HistoryProviderReference(_rollout(2), _rollout(1)),
            HistoryProviderReference(_rollout(3), _rollout(2)),
        }
    )
    for permutation in permutations(items):
        _check(condition=normalize_history_providers(_inventory(*permutation)) == expected)
    _check(condition=items[1].history_prefix == HistoryPrefix(_rollout(1), 19, 7))


def test_small_graphs_agree_with_independent_reachability_oracle() -> None:
    """Enumerate small functional graphs and compare cycle refusal with reachability."""
    for choices in product(range(4), repeat=3):
        items = tuple(
            _metadata(index, index) if provider == 0 else _prefix(_metadata(index, index), provider)
            for index, provider in enumerate(choices, 1)
        )
        edges = {(index, provider) for index, provider in enumerate(choices, 1) if provider}
        reachability = set(edges)
        for _ in range(3):
            reachability |= {(a, d) for a, b in reachability for c, d in edges if b == c}
        if any(a == b for a, b in reachability):
            with pytest.raises(InvalidGraphError):
                normalize_history_providers(_inventory(*items))
        else:
            observed = normalize_history_providers(_inventory(*items))
            expected = frozenset(
                HistoryProviderReference(_rollout(a), _rollout(b)) for a, b in edges
            )
            _check(condition=observed == expected)


def test_chain_longer_than_python_recursion_limit_is_processed_iteratively() -> None:
    """A long chain does not recurse or repeatedly traverse all previous providers."""
    count = 1500
    items = tuple(
        _metadata(index, index) if index == 1 else _prefix(_metadata(index, index), index - 1)
        for index in range(1, count + 1)
    )
    result = normalize_history_providers(_inventory(*items))
    _check(condition=len(result) == count - 1)


def test_orphan_consumer_seeds_same_retention_closure_without_sql_record() -> None:
    """Observed orphan history protects selected providers without a fabricated SQL owner."""
    provider, intermediate = _metadata(1, 1), _prefix(_metadata(2, 2), 1)
    orphan = _prefix(_metadata(9, 9), 2)
    graph = GraphSnapshot.from_records(
        threads=[
            ThreadRecord(
                _thread(number),
                ThreadOrigin.MAIN,
                ActivityState.UNKNOWN,
                ArchiveState.NOT_ARCHIVED,
                LastActivity(datetime(2026, 1, 1, tzinfo=UTC)),
            )
            for number in (1, 2)
        ],
        rollouts=[RolloutRecord(_rollout(number), ArchiveState.NOT_ARCHIVED) for number in (1, 2)],
        ownership_edges=[provider.ownership, intermediate.ownership],
    )
    result = select_closed_work(
        graph,
        UtcCutoff(datetime(2026, 8, 1, tzinfo=UTC)),
        frozenset({_thread(1), _thread(2)}),
        physical_inventory=_inventory(provider, intermediate, orphan),
    )
    _check(condition=not result.conditional_candidates)
    witnesses = {item.thread_id: item.witness for item in result.retained}
    _check(condition=witnesses[_thread(2)] == _thread(9))
    _check(condition=witnesses[_thread(1)] == _thread(2))
    _check(condition=all(item.id != _thread(9) for item in graph.threads))
