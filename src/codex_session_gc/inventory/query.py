# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Shared in-memory catalogue filtering; matches never authorize cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from codex_session_gc.inventory.catalogue import SessionSource

if TYPE_CHECKING:
    from datetime import datetime

    from codex_session_gc.domain import ThreadId
    from codex_session_gc.inventory.catalogue import SessionCatalogue, SessionEntry, StoredRollout


@dataclass(frozen=True, slots=True)
class CatalogueQuery:
    """Conjunctive per-history filters; roots restrict contextual work groups."""

    text: str = ""
    before: datetime | None = None
    min_bytes: int | None = None
    max_bytes: int | None = None
    scope: Literal["all", "main", "agents"] = "all"
    sort: Literal["total", "own", "agents", "activity"] = "total"
    roots: tuple[ThreadId, ...] = ()

    def __post_init__(self) -> None:
        """Reject ambiguous instants and invalid bounds rather than widening queries."""
        if self.before is not None and self.before.utcoffset() is None:
            raise ValueError("The activity cutoff requires a timezone.")
        if self.scope not in {"all", "main", "agents"} or self.sort not in {
            "total",
            "own",
            "agents",
            "activity",
        }:
            raise ValueError("Unknown catalogue query option.")
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


def _matches(entry: SessionEntry, query: CatalogueQuery) -> bool:
    if query.scope == "main" and entry.source is not SessionSource.MAIN:
        return False
    if query.scope == "agents" and entry.source not in {
        SessionSource.AGENT,
        SessionSource.GUARDIAN,
    }:
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

    def query(self, query: CatalogueQuery) -> tuple[CatalogueGroup, ...]:
        """Return complete group context alongside stable exact matching histories."""
        result = []
        for root, entries in self._groups:
            if query.roots and root.id not in query.roots:
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

        def order(group: CatalogueGroup) -> tuple[float, bool, str]:
            if query.sort == "activity":
                value = (
                    group.last_activity.timestamp()
                    if group.last_activity is not None
                    else float("-inf")
                )
            else:
                value = {
                    "total": group.total_known_stored_bytes,
                    "own": group.own_known_stored_bytes,
                    "agents": group.agent_known_stored_bytes,
                }[query.sort]
            incomplete = {
                "total": group.total_stored_bytes is None,
                "own": group.own_stored_bytes is None,
                "agents": group.agent_stored_bytes is None,
                "activity": False,
            }[query.sort]
            return -value, incomplete, str(group.root.id.value)

        return tuple(sorted(result, key=order))
