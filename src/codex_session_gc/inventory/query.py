# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Shared in-memory catalogue filtering; matches never authorize cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from codex_session_gc.domain import ArchiveState
from codex_session_gc.inventory.catalogue import SessionSource

if TYPE_CHECKING:
    from codex_session_gc.domain import ThreadId
    from codex_session_gc.inventory.catalogue import SessionCatalogue, SessionEntry, StoredRollout


class CatalogueScope(StrEnum):
    """Select source classifications without treating Guardian association as spawn ownership."""

    ALL = "all"
    MAIN = "main"
    AGENTS = "agents"
    ORDINARY_AGENTS = "ordinary_agents"
    GUARDIANS = "guardians"


@dataclass(frozen=True, slots=True)
class CatalogueQuery:
    """Conjunctive per-history filters; roots restrict contextual work groups."""

    text: str = ""
    before: datetime | None = None
    min_bytes: int | None = None
    max_bytes: int | None = None
    scope: CatalogueScope | str = CatalogueScope.ALL
    sort: Literal["total", "own", "agents", "stored_bytes", "activity"] = "total"
    roots: tuple[ThreadId, ...] = ()
    cwd: str | None = None
    archive_state: ArchiveState | str | None = None
    order_target: Literal["work", "history"] = "work"
    direction: Literal["asc", "desc"] = "desc"

    def __post_init__(self) -> None:
        """Reject ambiguous instants and invalid bounds rather than widening queries."""
        object.__setattr__(self, "scope", CatalogueScope(self.scope))
        if self.archive_state is not None:
            object.__setattr__(self, "archive_state", ArchiveState(self.archive_state))
        if self.cwd is not None and not isinstance(self.cwd, str):
            raise ValueError("The exact stored cwd must be text.")
        if self.before is not None and self.before.utcoffset() is None:
            raise ValueError("The activity cutoff requires a timezone.")
        if self.order_target not in ("work", "history"):
            raise ValueError("Unknown catalogue order target.")
        fields = (
            ("total", "own", "agents", "activity")
            if self.order_target == "work"
            else ("stored_bytes", "activity")
        )
        if self.sort not in fields:
            raise ValueError("The sort field does not belong to this catalogue order target.")
        if self.direction not in ("asc", "desc"):
            raise ValueError("Unknown catalogue order direction.")
        if any(
            value is not None and (type(value) is not int or value < 0)
            for value in (self.min_bytes, self.max_bytes)
        ):
            raise ValueError("File size bounds must be nonnegative integers.")
        if (
            self.min_bytes is not None
            and self.max_bytes is not None
            and self.min_bytes > self.max_bytes
        ):
            raise ValueError("File size bounds are reversed.")


@dataclass(frozen=True, slots=True)
class CatalogueGroup:
    """Full contextual work, known stored subtotals and separately nullable exact totals."""

    root: SessionEntry
    entries: tuple[SessionEntry, ...]
    matching_ids: tuple[ThreadId, ...]
    own_known_stored_bytes: int
    agent_known_stored_bytes: int
    total_known_stored_bytes: int
    own_incomplete: int
    agent_incomplete: int
    incomplete_history_count: int
    last_activity: datetime | None
    activity_incomplete: bool
    selectable: bool
    own_stored_bytes: int | None = None
    agent_stored_bytes: int | None = None
    total_stored_bytes: int | None = None
    shared_retained_bytes: int | None = None
    conditional_reclaim_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class CatalogueResult:
    """Contextual groups and the complete authoritative order of matching histories."""

    groups: tuple[CatalogueGroup, ...]
    matching_histories: tuple[SessionEntry, ...]


def _order(
    value: int | None,
    *,
    incomplete: bool,
    identifier: ThreadId,
    direction: Literal["asc", "desc"],
) -> tuple[bool, int, bool, str]:
    """Keep unknowns last and ties stable, reversing only the known metric."""
    metric = 0 if value is None else value
    return (
        value is None,
        -metric if direction == "desc" else metric,
        incomplete,
        str(identifier.value),
    )


def _activity(value: datetime | None) -> int | None:
    """Retain exact microseconds instead of rounding distant instants through floats."""
    if value is None:
        return None
    elapsed = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds


def _group_order(
    group: CatalogueGroup,
    field: str,
    direction: Literal["asc", "desc"],
) -> tuple[bool, int, bool, str]:
    if field == "activity":
        value = _activity(group.last_activity)
        incomplete = group.activity_incomplete
    else:
        known, exact = {
            "total": (group.total_known_stored_bytes, group.total_stored_bytes),
            "own": (group.own_known_stored_bytes, group.own_stored_bytes),
            "agents": (group.agent_known_stored_bytes, group.agent_stored_bytes),
        }[field]
        incomplete = exact is None
        value = None if incomplete and known == 0 else known
    return _order(value, incomplete=incomplete, identifier=group.root.id, direction=direction)


def _history_order(
    entry: SessionEntry,
    field: str,
    direction: Literal["asc", "desc"],
) -> tuple[bool, int, bool, str]:
    if field == "activity":
        value = _activity(entry.last_activity)
        incomplete = entry.last_activity is None
    else:
        incomplete = entry.stored_bytes is None
        value = None if incomplete and entry.known_stored_bytes == 0 else entry.known_stored_bytes
    return _order(value, incomplete=incomplete, identifier=entry.id, direction=direction)


def _source_matches(source: SessionSource, scope: CatalogueScope | str) -> bool:
    if scope is CatalogueScope.MAIN:
        return source is SessionSource.MAIN
    if scope is CatalogueScope.AGENTS:
        return source in {SessionSource.AGENT, SessionSource.GUARDIAN}
    if scope is CatalogueScope.ORDINARY_AGENTS:
        return source is SessionSource.AGENT
    if scope is CatalogueScope.GUARDIANS:
        return source is SessionSource.GUARDIAN
    return scope is CatalogueScope.ALL


def _matches(entry: SessionEntry, query: CatalogueQuery) -> bool:
    if (
        not _source_matches(entry.source, query.scope)
        or (query.cwd is not None and entry.cwd != query.cwd)
        or (query.archive_state is not None and entry.archive_state is not query.archive_state)
    ):
        return False
    if query.text.casefold() not in f"{entry.title}\n{entry.cwd}\n{entry.id.value}".casefold():
        return False
    if query.before is not None and (
        entry.last_activity is None or entry.last_activity >= query.before
    ):
        return False
    return not (
        (query.min_bytes is not None and (entry.known_stored_bytes < query.min_bytes))
        or (
            query.max_bytes is not None
            and (entry.stored_bytes is None or entry.stored_bytes > query.max_bytes)
        )
    )


def _group_sizes(
    root: ThreadId, entries: tuple[SessionEntry, ...]
) -> tuple[int, int, set[ThreadId]]:
    """Attribute each observed object once, excluding conflicting physical ownership."""
    objects: dict[tuple[int, int], StoredRollout] = {}
    ambiguous: set[ThreadId] = set()
    excluded: set[tuple[int, int]] = set()
    for entry in entries:
        for item in entry.physical_rollouts:
            key = item.stat.dev, item.stat.ino
            previous = objects.get(key)
            if previous is not None and previous != item:
                ambiguous.update((previous.ownership.thread_id, item.ownership.thread_id))
                excluded.add(key)
            objects[key] = item
    own = agents = 0
    for key, item in objects.items():
        if key in excluded:
            continue
        if item.ownership.thread_id == root:
            own += item.stat.stored_bytes
        else:
            agents += item.stat.stored_bytes
    return own, agents, ambiguous


class CatalogueIndex:
    """Pre-group one detached catalogue; filtering never rereads storage."""

    def __init__(self, catalogue: SessionCatalogue) -> None:
        """Index identities and membership once for this observation."""
        self.catalogue = catalogue
        self.entries = {entry.id: entry for entry in catalogue.entries}
        grouped: dict[ThreadId, list[SessionEntry]] = {}
        for entry in self.entries.values():
            root = entry.root_id if entry.root_id in self.entries else entry.id
            grouped.setdefault(root, []).append(entry)
        self._groups = tuple(
            (
                self.entries[root],
                tuple(
                    sorted(
                        entries,
                        key=lambda entry: (
                            -entry.known_stored_bytes,
                            entry.stored_bytes is None,
                            str(entry.id.value),
                        ),
                    )
                ),
            )
            for root, entries in grouped.items()
        )

    def query(self, query: CatalogueQuery) -> CatalogueResult:
        """Return complete group context alongside stable exact matching histories."""
        result = []
        roots = frozenset(query.roots)
        for root, entries in self._groups:
            if roots and root.id not in roots:
                continue
            matches = tuple(entry.id for entry in entries if _matches(entry, query))
            if not matches:
                continue
            own, agents, ambiguous = _group_sizes(root.id, entries)
            own_incomplete = int(root.stored_bytes is None or root.id in ambiguous)
            agent_incomplete = sum(
                entry.stored_bytes is None or entry.id in ambiguous
                for entry in entries
                if entry.id != root.id
            )
            dates = [entry.last_activity for entry in entries if entry.last_activity is not None]
            result.append(
                CatalogueGroup(
                    root,
                    entries,
                    matches,
                    own,
                    agents,
                    own + agents,
                    own_incomplete,
                    agent_incomplete,
                    own_incomplete + agent_incomplete,
                    max(dates) if dates else None,
                    len(dates) != len(entries),
                    root.root_id == root.id and all(entry.root_id == root.id for entry in entries),
                    own if not own_incomplete else None,
                    agents if not agent_incomplete else None,
                    own + agents if not own_incomplete and not agent_incomplete else None,
                )
            )

        group_field = query.sort if query.order_target == "work" else "total"
        group_direction = query.direction if query.order_target == "work" else "desc"
        groups = tuple(
            sorted(result, key=lambda group: _group_order(group, group_field, group_direction))
        )
        histories: list[SessionEntry] = []
        for group in groups:
            matching_ids = frozenset(group.matching_ids)
            histories.extend(entry for entry in group.entries if entry.id in matching_ids)
        if query.order_target == "history":
            histories.sort(key=lambda entry: _history_order(entry, query.sort, query.direction))
        return CatalogueResult(groups, tuple(histories))
