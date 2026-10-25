# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004, PT011, DTZ001

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from codex_session_gc.domain import ArchiveState, ThreadId
from codex_session_gc.inventory.catalogue import SessionCatalogue, SessionEntry, SessionSource
from codex_session_gc.inventory.query import CatalogueIndex, CatalogueQuery, CatalogueScope
from tests.current_storage_fixture import _stored_rollout

_A = ThreadId.parse("11111111-1111-4111-8111-111111111111")
_B = ThreadId.parse("22222222-2222-4222-8222-222222222222")
_C = ThreadId.parse("33333333-3333-4333-8333-333333333333")
_D = ThreadId.parse("44444444-4444-4444-8444-444444444444")
_E = ThreadId.parse("55555555-5555-4555-8555-555555555555")
_DATE = datetime(2026, 1, 1, tzinfo=UTC)
_CWD = "/project/[Alpha] + draft"


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
    groups = index.query(CatalogueQuery()).groups
    assert [group.root.id for group in groups] == [_C, _A, _D]
    group = index.query(CatalogueQuery(text="agent", min_bytes=200, max_bytes=400)).groups[0]
    assert group.root.id == _A
    assert group.matching_ids == (_B,)
    assert [entry.id for entry in group.entries] == [_B, _A]
    assert (
        group.own_known_stored_bytes,
        group.agent_known_stored_bytes,
        group.total_known_stored_bytes,
    ) == (100, 300, 400)
    assert group.selectable


def _ordered_index() -> CatalogueIndex:
    """The largest agent belongs to a smaller work, with distinct activity dates."""
    return CatalogueIndex(
        SessionCatalogue(
            (
                replace(_entry(_A, 700), last_activity=_DATE + timedelta(days=20)),
                _entry(_B, 100, _A),
                _entry(_C, 10),
                replace(_entry(_D, 300, _C), root_id=_C, last_activity=_DATE + timedelta(days=2)),
                replace(_entry(_E, 200, _C), root_id=_C, last_activity=_DATE + timedelta(days=1)),
            ),
            (),
            source_changed=False,
        )
    )


def test_history_order_ranks_across_work_groups_before_paging() -> None:
    index = _ordered_index()
    work = index.query(CatalogueQuery(scope="agents"))
    histories = index.query(
        CatalogueQuery(scope="agents", order_target="history", sort="stored_bytes")
    )
    assert [group.root.id for group in work.groups] == [_A, _C]
    assert [entry.id for entry in work.matching_histories] == [_B, _D, _E]
    assert [entry.id for entry in histories.matching_histories] == [_D, _E, _B]
    assert [group.total_stored_bytes for group in histories.groups] == [800, 510]
    assert {entry.id for group in histories.groups for entry in group.entries} == {
        _A,
        _B,
        _C,
        _D,
        _E,
    }
    assert {entry.id for entry in histories.matching_histories} == {_B, _D, _E}
    pages = [histories.matching_histories[offset : offset + 1] for offset in range(3)]
    assert tuple(entry for page in pages for entry in page) == histories.matching_histories
    assert work.groups == histories.groups


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            CatalogueQuery(
                scope="agents", order_target="history", sort="activity", direction="asc"
            ),
            [_B, _E, _D],
        ),
        (
            CatalogueQuery(
                scope="agents", order_target="history", sort="activity", direction="desc"
            ),
            [_D, _E, _B],
        ),
        (
            CatalogueQuery(
                scope="agents",
                order_target="history",
                sort="activity",
                direction="asc",
                roots=(_C,),
            ),
            [_E, _D],
        ),
        (
            CatalogueQuery(
                scope="agents",
                order_target="history",
                sort="activity",
                direction="asc",
                roots=(_C,),
                min_bytes=250,
            ),
            [_D],
        ),
    ],
)
def test_activity_orders_exact_filtered_histories(
    query: CatalogueQuery, expected: list[ThreadId]
) -> None:
    result = _ordered_index().query(query)
    assert [entry.id for entry in result.matching_histories] == expected


@pytest.mark.parametrize("field", ["total", "own", "agents", "activity"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_all_work_order_fields_keep_existing_within_work_order(field: str, direction: str) -> None:
    query = CatalogueQuery(sort=field, direction=direction)  # type: ignore[arg-type]
    result = _ordered_index().query(query)
    descending = [_C, _A] if field == "agents" else [_A, _C]
    expected = descending if direction == "desc" else list(reversed(descending))
    assert [group.root.id for group in result.groups] == expected
    by_root = {group.root.id: group for group in result.groups}
    assert [entry.id for entry in by_root[_A].entries] == [_A, _B]
    assert [entry.id for entry in by_root[_C].entries] == [_D, _E, _C]
    assert [entry.id for entry in result.matching_histories] == [
        entry.id for group in result.groups for entry in group.entries
    ]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (CatalogueQuery(), [_E, _D, _C, _B, _A]),
        (CatalogueQuery(direction="asc"), [_B, _D, _C, _E, _A]),
        (CatalogueQuery(order_target="history", sort="stored_bytes"), [_E, _D, _C, _B, _A]),
        (
            CatalogueQuery(order_target="history", sort="stored_bytes", direction="asc"),
            [_B, _D, _C, _E, _A],
        ),
    ],
)
def test_size_orders_lower_bounds_exact_zero_and_unknowns(
    query: CatalogueQuery, expected: list[ThreadId]
) -> None:
    index = CatalogueIndex(
        SessionCatalogue(
            (
                _entry(_A, None),
                _entry(_B, 0),
                replace(_entry(_C, 100), stored_bytes=None),
                _entry(_D, 100),
                _entry(_E, 200),
            ),
            (),
            source_changed=False,
        )
    )
    result = index.query(query)
    assert [entry.id for entry in result.matching_histories] == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (CatalogueQuery(sort="activity", direction="asc"), [_B, _C, _E, _A]),
        (CatalogueQuery(sort="activity", direction="desc"), [_E, _B, _C, _A]),
        (
            CatalogueQuery(order_target="history", sort="activity", direction="asc"),
            [_B, _C, _E, _A, _D],
        ),
        (
            CatalogueQuery(order_target="history", sort="activity", direction="desc"),
            [_E, _B, _C, _A, _D],
        ),
    ],
)
def test_activity_unknowns_remain_last_and_complete_group_wins_tie(
    query: CatalogueQuery, expected: list[ThreadId]
) -> None:
    index = CatalogueIndex(
        SessionCatalogue(
            (
                replace(_entry(_A, 100), last_activity=None),
                _entry(_B, 100),
                _entry(_C, 100),
                replace(_entry(_D, 100, _C), root_id=_C, last_activity=None),
                replace(_entry(_E, 100), last_activity=_DATE + timedelta(days=1)),
            ),
            (),
            source_changed=False,
        )
    )
    result = index.query(query)
    actual = (
        [group.root.id for group in result.groups]
        if query.order_target == "work"
        else [entry.id for entry in result.matching_histories]
    )
    assert actual == expected


def test_order_retains_large_integer_and_distant_microsecond_precision() -> None:
    older = datetime(9999, 1, 1, tzinfo=UTC)
    index = CatalogueIndex(
        SessionCatalogue(
            (
                replace(_entry(_A, 2**60 + 1), last_activity=older + timedelta(microseconds=1)),
                replace(_entry(_B, 2**60), last_activity=older),
            ),
            (),
            source_changed=False,
        )
    )
    by_size = index.query(CatalogueQuery(order_target="history", sort="stored_bytes"))
    by_date = index.query(CatalogueQuery(order_target="history", sort="activity", direction="asc"))
    assert [entry.id for entry in by_size.matching_histories] == [_A, _B]
    assert [entry.id for entry in by_date.matching_histories] == [_B, _A]


@pytest.mark.parametrize(
    "query",
    [
        CatalogueQuery(),
        CatalogueQuery(direction="asc"),
        CatalogueQuery(sort="activity"),
        CatalogueQuery(sort="activity", direction="asc"),
        CatalogueQuery(order_target="history", sort="stored_bytes"),
        CatalogueQuery(order_target="history", sort="stored_bytes", direction="asc"),
        CatalogueQuery(order_target="history", sort="activity"),
        CatalogueQuery(order_target="history", sort="activity", direction="asc"),
    ],
)
def test_equal_metrics_use_uuid_ascending_independently_of_enumeration(
    query: CatalogueQuery,
) -> None:
    entries = (_entry(_C, 100), _entry(_B, 100), _entry(_A, 100))
    result = CatalogueIndex(SessionCatalogue(entries, (), source_changed=False)).query(query)
    reversed_result = CatalogueIndex(
        SessionCatalogue(tuple(reversed(entries)), (), source_changed=False)
    ).query(query)
    assert [entry.id for entry in result.matching_histories] == [_A, _B, _C]
    assert result == reversed_result


def test_empty_result_has_no_groups_or_matching_histories() -> None:
    result = _index().query(CatalogueQuery(text="not present"))
    assert result.groups == ()
    assert result.matching_histories == ()


def test_ordering_only_reads_detached_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    index = _ordered_index()

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Ordering reread storage")

    with monkeypatch.context() as patch:
        patch.setattr("pathlib.Path.stat", forbidden)
        patch.setattr("sqlite3.connect", forbidden)
        result = index.query(
            CatalogueQuery(scope="agents", order_target="history", sort="activity", direction="asc")
        )
    assert [entry.id for entry in result.matching_histories] == [_B, _E, _D]


def test_strict_before_and_unknown_dates() -> None:
    assert not _index().query(CatalogueQuery(before=_DATE)).groups
    assert len(_index().query(CatalogueQuery(before=datetime(2027, 1, 1, tzinfo=UTC))).groups) == 2


def test_scope_roots_and_per_history_size_bounds_are_conjunctive() -> None:
    index = _index()
    group = index.query(CatalogueQuery(scope="agents", roots=(_A,), min_bytes=250)).groups[0]
    assert group.matching_ids == (_B,)
    assert not index.query(CatalogueQuery(scope="main", roots=(_A,), min_bytes=250)).groups
    assert {
        identifier
        for group in index.query(CatalogueQuery(scope="main")).groups
        for identifier in group.matching_ids
    } == {_A, _C}
    assert not index.query(CatalogueQuery(text="missing", scope="agents")).groups


def test_unknowns_remain_visible_without_false_zero_or_main_classification() -> None:
    group = _index().query(CatalogueQuery(roots=(_D,))).groups[0]
    assert group.own_incomplete == group.incomplete_history_count == 1
    assert group.own_known_stored_bytes == 0
    assert group.last_activity is None
    assert group.activity_incomplete
    assert not group.selectable
    assert _index().query(CatalogueQuery(roots=(_D,), min_bytes=0)).groups
    assert not _index().query(CatalogueQuery(roots=(_D,), max_bytes=0)).groups


def test_group_activity_incompleteness_and_duplicate_members_deduplicated() -> None:
    root = _entry(_A, None)
    child = replace(_entry(_B, 300, _A), last_activity=None)
    group = (
        CatalogueIndex(SessionCatalogue((root, child, child), (), source_changed=False))
        .query(CatalogueQuery())
        .groups[0]
    )
    assert len(group.entries) == 2
    assert group.total_known_stored_bytes == 300
    assert group.incomplete_history_count == 1
    assert group.last_activity == _DATE
    assert group.activity_incomplete
    stale = (
        CatalogueIndex(SessionCatalogue((root, child), (), source_changed=True))
        .query(CatalogueQuery())
        .groups[0]
    )
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
        {"order_target": "unknown"},
        {"order_target": "work", "sort": "stored_bytes"},
        {"order_target": "history", "sort": "total"},
        {"order_target": "history", "sort": "own"},
        {"order_target": "history", "sort": "agents"},
        {"direction": "unknown"},
        {"direction": None},
        {"archive_state": "unknown_value"},
        {"cwd": True},
    ],
)
def test_invalid_queries_refuse_without_widening(query: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CatalogueQuery(**query)  # type: ignore[arg-type]


def test_equal_known_amounts_use_exactness_then_stable_identity() -> None:
    incomplete = replace(_entry(_A, 100), stored_bytes=None, physical_complete=False)
    exact = _entry(_C, 100)
    index = CatalogueIndex(SessionCatalogue((incomplete, exact), (), source_changed=False))
    assert [group.root.id for group in index.query(CatalogueQuery()).groups] == [_C, _A]
    assert {group.root.id for group in index.query(CatalogueQuery(min_bytes=100)).groups} == {
        _A,
        _C,
    }
    assert [group.root.id for group in index.query(CatalogueQuery(max_bytes=100)).groups] == [_C]


def _mixed_index() -> CatalogueIndex:
    return CatalogueIndex(
        SessionCatalogue(
            (
                replace(_entry(_A, 100), cwd=_CWD, archive_state=ArchiveState.NOT_ARCHIVED),
                replace(_entry(_B, 300, _A), cwd=_CWD),
                replace(_entry(_C, 200), cwd=_CWD, source=SessionSource.GUARDIAN),
                replace(_entry(_D, 500), cwd=_CWD + "/child"),
                replace(
                    _entry(_E, 50),
                    cwd=_CWD,
                    archive_state=ArchiveState.UNKNOWN,
                    stored_bytes=None,
                    source=SessionSource.UNKNOWN,
                    root_id=None,
                ),
            ),
            (),
            source_changed=False,
        )
    )


def _matches(index: CatalogueIndex, query: CatalogueQuery) -> set[ThreadId]:
    return {entry.id for entry in index.query(query).matching_histories}


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (CatalogueScope.ALL, {_A, _B, _C, _E}),
        (CatalogueScope.MAIN, {_A}),
        (CatalogueScope.AGENTS, {_B, _C}),
        (CatalogueScope.ORDINARY_AGENTS, {_B}),
        (CatalogueScope.GUARDIANS, {_C}),
    ],
)
def test_source_scopes_and_exact_directory_use_stored_classification(
    scope: CatalogueScope, expected: set[ThreadId]
) -> None:
    assert _matches(_mixed_index(), CatalogueQuery(cwd=_CWD, scope=scope)) == expected


@pytest.mark.parametrize(
    ("archive", "expected"),
    [
        (ArchiveState.ARCHIVED, {_B, _C}),
        (ArchiveState.NOT_ARCHIVED, {_A}),
        (ArchiveState.UNKNOWN, {_E}),
    ],
)
def test_archive_predicate_is_per_history_and_keeps_full_context(
    archive: ArchiveState, expected: set[ThreadId]
) -> None:
    index = _mixed_index()
    assert _matches(index, CatalogueQuery(cwd=_CWD, archive_state=archive)) == expected
    (group,) = index.query(
        CatalogueQuery(cwd=_CWD, archive_state=ArchiveState.ARCHIVED, scope="ordinary_agents")
    ).groups
    assert group.root.id == _A
    assert group.matching_ids == (_B,)
    assert {entry.id for entry in group.entries} == {_A, _B}
    assert group.total_stored_bytes == 400
    assert group.own_stored_bytes == 100


def test_exact_cwd_is_literal_case_sensitive_and_never_normalized() -> None:
    index = _mixed_index()
    assert _matches(index, CatalogueQuery(text="[alpha]")) == {_A, _B, _C, _D, _E}
    assert _matches(index, CatalogueQuery(cwd=_CWD)) == {_A, _B, _C, _E}
    assert not index.query(CatalogueQuery(cwd=_CWD.lower())).groups
    assert not index.query(CatalogueQuery(cwd=_CWD + "/")).groups
    assert not index.query(CatalogueQuery(cwd="/project/./[Alpha] + draft")).groups
    assert not index.query(CatalogueQuery(cwd="/project/Alpha + draft")).groups


def test_exact_filters_preserve_incomplete_byte_bounds_and_query_detachment() -> None:
    index = _mixed_index()
    query = CatalogueQuery(cwd=_CWD, archive_state=ArchiveState.UNKNOWN, min_bytes=50)
    (group,) = index.query(query).groups
    assert group.matching_ids == (_E,)
    assert group.own_known_stored_bytes == 50
    assert group.own_stored_bytes is None
    assert not group.selectable
    assert not index.query(replace(query, max_bytes=50)).groups
    assert _matches(index, query) == {_E}
    assert _matches(index, CatalogueQuery(cwd=_CWD, scope="agents", min_bytes=200)) == {_B, _C}
