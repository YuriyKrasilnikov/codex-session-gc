# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004, PT011, DTZ001

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from codex_session_gc.domain import ArchiveState, ThreadId
from codex_session_gc.inventory.catalogue import SessionCatalogue, SessionEntry, SessionSource
from codex_session_gc.inventory.query import CatalogueIndex, CatalogueQuery
from tests.current_storage_fixture import _stored_rollout

_A = ThreadId.parse("11111111-1111-4111-8111-111111111111")
_B = ThreadId.parse("22222222-2222-4222-8222-222222222222")
_C = ThreadId.parse("33333333-3333-4333-8333-333333333333")
_D = ThreadId.parse("44444444-4444-4444-8444-444444444444")
_DATE = datetime(2026, 1, 1, tzinfo=UTC)


def _entry(identifier: ThreadId, size: int | None, parent: ThreadId | None = None) -> SessionEntry:
    return SessionEntry(
        identifier,
        "project agent" if parent else "project main",
        "/project",
        _DATE,
        ArchiveState.ARCHIVED,
        parent,
        _A if parent else identifier,
        (),
        size,
        "closed" if parent else None,
        None,
        SessionSource.AGENT if parent else SessionSource.MAIN,
        known_stored_bytes=size or 0,
        physical_rollouts=() if size is None else (_stored_rollout(identifier, size),),
    )


def _index() -> CatalogueIndex:
    return CatalogueIndex(
        SessionCatalogue(
            (
                _entry(_A, 100),
                _entry(_B, 300, _A),
                _entry(_C, 500),
                replace(
                    _entry(_D, None),
                    source=SessionSource.UNKNOWN,
                    root_id=None,
                    last_activity=None,
                    issue="invalid_spawn_topology",
                ),
            ),
            (),
            source_changed=False,
        )
    )


def test_largest_groups_full_context_and_exact_history_matches() -> None:
    index = _index()
    groups = index.query(CatalogueQuery())
    assert [group.root.id for group in groups] == [_C, _A, _D]
    group = index.query(CatalogueQuery(text="agent", min_bytes=200, max_bytes=400))[0]
    assert group.root.id == _A
    assert group.matching_ids == (_B,)
    assert [entry.id for entry in group.entries] == [_B, _A]
    assert (
        group.own_known_stored_bytes,
        group.agent_known_stored_bytes,
        group.total_known_stored_bytes,
    ) == (100, 300, 400)
    assert group.selectable


def test_strict_before_and_unknown_dates() -> None:
    assert not _index().query(CatalogueQuery(before=_DATE))
    assert len(_index().query(CatalogueQuery(before=datetime(2027, 1, 1, tzinfo=UTC)))) == 2


def test_scope_roots_and_per_history_size_bounds_are_conjunctive() -> None:
    index = _index()
    group = index.query(CatalogueQuery(scope="agents", roots=(_A,), min_bytes=250))[0]
    assert group.matching_ids == (_B,)
    assert not index.query(CatalogueQuery(scope="main", roots=(_A,), min_bytes=250))
    assert {
        identifier
        for group in index.query(CatalogueQuery(scope="main"))
        for identifier in group.matching_ids
    } == {_A, _C}
    assert not index.query(CatalogueQuery(text="missing", scope="agents"))


def test_unknowns_remain_visible_without_false_zero_or_main_classification() -> None:
    group = _index().query(CatalogueQuery(roots=(_D,)))[0]
    assert group.own_incomplete == group.incomplete_history_count == 1
    assert group.own_known_stored_bytes == 0
    assert group.last_activity is None
    assert group.activity_incomplete
    assert not group.selectable
    assert _index().query(CatalogueQuery(roots=(_D,), min_bytes=0))
    assert not _index().query(CatalogueQuery(roots=(_D,), max_bytes=0))


def test_group_activity_incompleteness_and_duplicate_members_deduplicated() -> None:
    root = _entry(_A, None)
    child = replace(_entry(_B, 300, _A), last_activity=None)
    group = CatalogueIndex(SessionCatalogue((root, child, child), (), source_changed=False)).query(
        CatalogueQuery()
    )[0]
    assert len(group.entries) == 2
    assert group.total_known_stored_bytes == 300
    assert group.incomplete_history_count == 1
    assert group.last_activity == _DATE
    assert group.activity_incomplete
    stale = CatalogueIndex(SessionCatalogue((root, child), (), source_changed=True)).query(
        CatalogueQuery()
    )[0]
    assert stale.selectable


@pytest.mark.parametrize(
    "query",
    [
        {"min_bytes": -1},
        {"max_bytes": -1},
        {"min_bytes": 10, "max_bytes": 5},
        {"before": datetime(2026, 1, 1)},
        {"scope": "unknown"},
        {"sort": "unknown"},
    ],
)
def test_invalid_queries_refuse_without_widening(query: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CatalogueQuery(**query)  # type: ignore[arg-type]


def test_equal_known_amounts_use_exactness_then_stable_identity() -> None:
    incomplete = replace(_entry(_A, 100), stored_bytes=None, physical_complete=False)
    exact = _entry(_C, 100)
    index = CatalogueIndex(SessionCatalogue((incomplete, exact), (), source_changed=False))
    assert [group.root.id for group in index.query(CatalogueQuery())] == [_C, _A]
    assert {group.root.id for group in index.query(CatalogueQuery(min_bytes=100))} == {_A, _C}
    assert [group.root.id for group in index.query(CatalogueQuery(max_bytes=100))] == [_C]
