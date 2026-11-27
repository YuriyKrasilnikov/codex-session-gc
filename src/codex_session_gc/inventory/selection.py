# Copyright (c) 2026

"""Projection of an exact selection over a detached catalogue observation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from codex_session_gc.domain.physical import InventoryCompleteness
from codex_session_gc.inventory.query import Totals

if TYPE_CHECKING:
    from codex_session_gc.domain import GraphSnapshot, PhysicalInventory, RolloutMetadata, ThreadId
    from codex_session_gc.inventory.catalogue import SessionEntry, StoredRollout
    from codex_session_gc.inventory.query import CatalogueIndex


@dataclass(frozen=True, slots=True)
class SelectionProjection:
    """Selected rows and totals; visibility is unknown for an invalid filter."""

    rows: tuple[tuple[ThreadId, SessionEntry | None], ...]
    totals: Totals
    visible: int | None
    hidden: int | None
    missing: int
    unavailable_ids: frozenset[ThreadId]

    def summary(self) -> dict[str, object]:
        """Return a transport-friendly summary without expanding the selection."""
        return {
            **asdict(self.totals),
            "visible": self.visible,
            "hidden": self.hidden,
            "missing": self.missing,
        }


def project_selection(
    index: CatalogueIndex,
    selected: frozenset[ThreadId],
    visible: frozenset[ThreadId] | None = None,
) -> SelectionProjection:
    """Project exact IDs without accessing storage or adding contextual relatives."""
    rows = tuple((identifier, index.entries.get(identifier)) for identifier in sorted(selected))
    unavailable = frozenset(
        identifier
        for identifier, entry in rows
        if entry is None or entry.root_id is None or entry.root_id not in index.entries
    )
    known: dict[tuple[int, int], StoredRollout] = {}
    conflicts: set[tuple[int, int]] = set()
    incomplete = set(unavailable)
    incomplete.update(
        identifier for identifier, entry in rows if entry is not None and entry.stored_bytes is None
    )
    for identifier, entry in rows:
        if entry is None:
            continue
        for item in entry.physical_rollouts:
            key = item.stat.dev, item.stat.ino
            prior = known.get(key)
            if prior is not None and prior != item:
                conflicts.add(key)
                incomplete.add(identifier)
                if prior.ownership.thread_id in selected:
                    incomplete.add(prior.ownership.thread_id)
            known[key] = item
    size = sum(item.stat.stored_bytes for key, item in known.items() if key not in conflicts)
    missing = len(unavailable)
    found = selected - unavailable
    visible_count = len(found & visible) if visible is not None else None
    hidden = len(found) - visible_count if visible_count is not None else None
    return SelectionProjection(
        rows,
        Totals(len(rows), size, len(incomplete), size if not incomplete else None),
        visible_count,
        hidden,
        missing,
        unavailable,
    )


def project_preview_selection(
    snapshot: GraphSnapshot, inventory: PhysicalInventory, selected: frozenset[ThreadId]
) -> dict[str, object]:
    """Summarize the preview's existing scoped physical observation without another scan."""
    found = selected & {thread.id for thread in snapshot.threads}
    missing = len(selected - found)
    by_owner: dict[ThreadId, list[RolloutMetadata]] = {}
    for item in inventory.rollouts:
        if item.ownership.thread_id in selected:
            by_owner.setdefault(item.ownership.thread_id, []).append(item)
    pointers = {pointer.ownership.thread_id: pointer for pointer in inventory.current_pointers}
    incomplete = set(selected - found)
    physical_bytes: dict[tuple[int, int], int] = {}
    for identifier in found:
        items = by_owner.get(identifier, [])
        for item in items:
            if item.physical is not None:
                stat = item.physical.stat
                physical_bytes[stat.dev, stat.ino] = stat.stored_bytes
        pointer = pointers.get(identifier)
        if (
            inventory.completeness is not InventoryCompleteness.COMPLETE
            or pointer is None
            or not any(item.ownership == pointer.ownership and item.physical for item in items)
            or any(item.physical is None for item in items)
        ):
            incomplete.add(identifier)
    known = sum(physical_bytes.values())
    totals = Totals(len(selected), known, len(incomplete), known if not incomplete else None)
    return {**asdict(totals), "visible": None, "hidden": None, "missing": missing}
