# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Strict saved predicates and detached, read-only catalogue evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from codex_session_gc.domain import ThreadId
from codex_session_gc.inventory.catalogue import SessionSource
from codex_session_gc.inventory.query import CatalogueQuery, CatalogueScope

if TYPE_CHECKING:
    from codex_session_gc.inventory.query import CatalogueIndex


@dataclass(frozen=True, slots=True)
class SelectionRule:
    """A validated conjunction of predicates, independent of observations."""

    cwd: str | None = None
    work_roots: tuple[ThreadId, ...] = ()
    history_kind: CatalogueScope | None = None
    agent_status: str | None = None
    before: datetime | None = None
    older_than_days: int | None = None

    def to_mapping(self) -> dict[str, object]:
        """Serialize the predicate document for display and transport."""
        result: dict[str, object] = {"version": 1}
        if self.cwd is not None:
            result["cwd"] = self.cwd
        if self.work_roots:
            result["work_roots"] = [str(root.value) for root in self.work_roots]
        if self.history_kind is not None:
            result["history_kind"] = self.history_kind.value
        if self.agent_status is not None:
            result["agent_status"] = self.agent_status
        if self.before is not None:
            result["before"] = self.before.isoformat()
        if self.older_than_days is not None:
            result["older_than_days"] = self.older_than_days
        return result

    @classmethod
    def from_mapping(cls, value: object) -> SelectionRule:
        """Validate a structured rule identically for every interface."""
        if not isinstance(value, dict):
            return _invalid_rule()
        allowed = {
            "version",
            "cwd",
            "work_roots",
            "history_kind",
            "agent_status",
            "before",
            "older_than_days",
        }
        if set(value) - allowed or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError("Unknown rule fields or version.")
        if len(value) == 1:
            raise ValueError("A rule must explicitly specify a predicate.")
        cwd = value.get("cwd")
        if "cwd" in value and (not isinstance(cwd, str) or not cwd):
            raise ValueError("cwd must be nonempty text.")
        roots_value = value.get("work_roots", [])
        if not isinstance(roots_value, list) or ("work_roots" in value and not roots_value):
            raise ValueError("work_roots must be a nonempty array.")
        if any(not isinstance(item, str) for item in roots_value):
            raise ValueError("work_roots must contain UUID strings.")
        roots = tuple(ThreadId.parse(item) for item in roots_value)
        if len(set(roots)) != len(roots):
            raise ValueError("Duplicate work root.")
        kind, status = _parse_kind_status(value)
        before, days = _parse_age(value)
        return cls(cwd, roots, kind, status, before, days)


def _invalid_rule() -> SelectionRule:
    raise ValueError("A rule must be an object.")


def _parse_kind_status(value: dict[str, Any]) -> tuple[CatalogueScope | None, str | None]:
    if "history_kind" in value and not isinstance(value["history_kind"], str):
        raise ValueError("Unknown history kind.")
    kind = CatalogueScope(value["history_kind"]) if "history_kind" in value else None
    status = value.get("agent_status")
    if "agent_status" in value and status not in ("closed", "open", "active"):
        raise ValueError("Unknown agent status.")
    if status is not None and kind not in (None, CatalogueScope.ORDINARY_AGENTS):
        raise ValueError("Agent status requires ordinary agents.")
    return kind, status


def _as_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("Timestamp cannot be represented in UTC.") from error


def _parse_age(value: dict[str, Any]) -> tuple[datetime | None, int | None]:
    if "before" in value and "older_than_days" in value:
        raise ValueError("Choose one age predicate.")
    before = None
    if "before" in value:
        if not isinstance(value["before"], str):
            raise ValueError("before must be an aware ISO timestamp.")
        before = datetime.fromisoformat(value["before"])
        if before.utcoffset() is None:
            raise ValueError("before must have a timezone.")
        before = _as_utc(before)
    days = value.get("older_than_days")
    if "older_than_days" in value and (type(days) is not int or days <= 0):
        raise ValueError("older_than_days must be a positive integer.")
    return before, days


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


def parse_rule_json(data: str) -> SelectionRule:
    """Reject duplicate keys and nonstandard JSON constants."""

    def reject_constant(_value: str) -> None:
        raise ValueError("Nonstandard JSON constant.")

    return SelectionRule.from_mapping(
        json.loads(data, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    )


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """A finite set matched at one observation and its fixed time boundary."""

    rule: SelectionRule
    evaluated_at: datetime
    cutoff: datetime | None
    rule_matches: tuple[ThreadId, ...]
    removed_ids: tuple[ThreadId, ...] = ()
    matched_roots: tuple[ThreadId, ...] = ()
    unresolved_ids: tuple[ThreadId, ...] = ()


def evaluate_rule(
    index: CatalogueIndex,
    rule: SelectionRule,
    evaluated_at: datetime,
    *,
    prior_ids: frozenset[ThreadId] | None = None,
    prior_cutoff: datetime | None = None,
) -> RuleEvaluation:
    """Evaluate current observations; a carried review can only narrow."""
    if evaluated_at.utcoffset() is None:
        raise ValueError("Evaluation time must be timezone-aware.")
    evaluated_at = _as_utc(evaluated_at)
    if (
        prior_ids is not None
        and (rule.older_than_days is not None or rule.before is not None)
        and prior_cutoff is None
    ):
        raise ValueError("A carried age rule requires its resolved cutoff.")
    if prior_cutoff is not None and prior_ids is None:
        raise ValueError("A fixed cutoff requires a prior review.")
    if prior_cutoff is not None and prior_cutoff.utcoffset() is None:
        raise ValueError("A fixed cutoff requires a timezone.")
    previous = _as_utc(prior_cutoff) if prior_cutoff is not None else None
    cutoff: datetime | None
    if rule.older_than_days is not None:
        if previous is not None:
            cutoff = previous
        else:
            try:
                cutoff = evaluated_at - timedelta(days=rule.older_than_days)
            except OverflowError as error:
                raise ValueError("Age boundary cannot be represented.") from error
    else:
        cutoff = rule.before
        if previous is not None:
            cutoff = min(cutoff, previous) if cutoff is not None else previous
    # Catalogue compares microseconds; preview compares floored epoch milliseconds.
    query_cutoff = (
        cutoff.replace(microsecond=(cutoff.microsecond // 1000) * 1000) if cutoff else None
    )
    scope = rule.history_kind or (
        CatalogueScope.ORDINARY_AGENTS if rule.agent_status else CatalogueScope.ALL
    )
    matches = index.query(
        CatalogueQuery(cwd=rule.cwd, roots=rule.work_roots, scope=scope, before=query_cutoff)
    ).matching_histories
    ids = {
        entry.id
        for entry in matches
        if (not rule.work_roots or entry.root_id in rule.work_roots)
        and (
            rule.agent_status is None
            or (
                entry.source is SessionSource.AGENT
                and entry.issue != "invalid_spawn_topology"
                and entry.agent_status == rule.agent_status
            )
        )
    }
    removed = set() if prior_ids is None else prior_ids - ids
    if prior_ids is not None:
        ids &= prior_ids
    roots = {index.entries[item].root_id for item in ids}
    valid_roots = {
        root for root in roots if root in index.entries and index.entries[root].root_id == root
    }
    unresolved = {item for item in ids if index.entries[item].root_id not in valid_roots}
    return RuleEvaluation(
        rule,
        evaluated_at,
        cutoff,
        tuple(sorted(ids)),
        tuple(sorted(removed)),
        tuple(sorted(valid_roots)),
        tuple(sorted(unresolved)),
    )
