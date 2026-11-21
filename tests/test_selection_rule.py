# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PT011

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from codex_session_gc.inventory.catalogue import SessionCatalogue
from codex_session_gc.inventory.query import CatalogueIndex
from codex_session_gc.inventory.selection_rule import SelectionRule, evaluate_rule, parse_rule_json
from tests.test_catalogue_query import _A, _B, _C, _DATE, _entry, _index


def test_rejects_ambiguous_or_invalid_rules() -> None:
    for text in (
        "{}",
        '{"version":1}',
        '{"version":1,"cwd":"x","cwd":"y"}',
        '{"version":true,"history_kind":"all"}',
        '{"version":1,"work_roots":[]}',
        '{"version":1,"history_kind":"main","agent_status":"closed"}',
        '{"version":1,"before":"2026-01-01T00:00:00"}',
        '{"version":1,"older_than_days":true}',
        '{"version":1,"other":1}',
        '{"version":1,"work_roots":["11111111-1111-4111-8111-111111111111","11111111-1111-4111-8111-111111111111"]}',
    ):
        with pytest.raises(ValueError):
            parse_rule_json(text)


def test_conjunction_and_valid_status() -> None:
    rule = SelectionRule.from_mapping(
        {"version": 1, "cwd": "/project", "agent_status": "closed", "work_roots": [str(_A.value)]}
    )
    assert evaluate_rule(_index(), rule, _DATE).rule_matches == (_B,)
    entry = replace(_entry(_B, 300, _A), issue="invalid_spawn_topology")
    index = CatalogueIndex(SessionCatalogue((_entry(_A, 100), entry), (), source_changed=False))
    assert evaluate_rule(index, rule, _DATE).rule_matches == ()


def test_moving_cutoff_and_carried_review() -> None:
    now = datetime(2026, 4, 1, 0, 0, 0, 123456, tzinfo=UTC)
    cutoff = now - timedelta(days=90)
    index = CatalogueIndex(
        SessionCatalogue(
            (
                replace(_entry(_A, 1), last_activity=cutoff.replace(microsecond=122999)),
                replace(_entry(_C, 1), last_activity=cutoff.replace(microsecond=123000)),
            ),
            (),
            source_changed=False,
        )
    )
    rule = SelectionRule.from_mapping({"version": 1, "older_than_days": 90})
    first = evaluate_rule(index, rule, now)
    assert first.rule_matches == (_A,)
    assert first.cutoff == cutoff
    later = evaluate_rule(index, rule, now + timedelta(days=1))
    assert later.rule_matches == (_A, _C)
    carried = evaluate_rule(
        index,
        rule,
        now + timedelta(days=1),
        prior_ids=frozenset(first.rule_matches),
        prior_cutoff=first.cutoff,
    )
    assert carried.rule_matches == (_A,)
    assert carried.cutoff == cutoff


def test_refresh_drops_changed_status() -> None:
    rule = SelectionRule.from_mapping({"version": 1, "agent_status": "closed"})
    first = evaluate_rule(_index(), rule, _DATE)
    changed = CatalogueIndex(
        SessionCatalogue(
            (_entry(_A, 100), replace(_entry(_B, 300, _A), agent_status="open")),
            (),
            source_changed=False,
        )
    )
    refreshed = evaluate_rule(changed, rule, _DATE, prior_ids=frozenset(first.rule_matches))
    assert refreshed.rule_matches == ()
    assert refreshed.removed_ids == (_B,)


def test_unrepresentable_utc_and_age_boundaries_are_rejected() -> None:
    with pytest.raises(ValueError):
        SelectionRule.from_mapping({"version": 1, "before": "0001-01-01T00:00:00+01:00"})
    rule = SelectionRule.from_mapping({"version": 1, "older_than_days": 10**20})
    with pytest.raises(ValueError):
        evaluate_rule(_index(), rule, _DATE)
    from datetime import timezone  # noqa: PLC0415

    overflow = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    with pytest.raises(ValueError):
        evaluate_rule(_index(), rule, overflow)
    with pytest.raises(ValueError):
        evaluate_rule(
            _index(),
            SelectionRule.from_mapping({"version": 1, "history_kind": "all"}),
            _DATE,
            prior_ids=frozenset(),
            prior_cutoff=overflow,
        )


def test_carried_relative_age_requires_and_preserves_frozen_cutoff() -> None:
    rule = SelectionRule.from_mapping({"version": 1, "older_than_days": 90})
    first = evaluate_rule(_index(), rule, _DATE + timedelta(days=100))
    with pytest.raises(ValueError):
        evaluate_rule(_index(), rule, _DATE, prior_ids=frozenset())
    carried = evaluate_rule(
        _index(), rule, _DATE, prior_ids=frozenset(first.rule_matches), prior_cutoff=first.cutoff
    )
    assert carried.cutoff == first.cutoff
    assert carried.rule_matches == first.rule_matches
    with pytest.raises(ValueError):
        evaluate_rule(_index(), rule, _DATE, prior_cutoff=first.cutoff)
