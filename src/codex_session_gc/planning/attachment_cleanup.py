# Copyright (c) 2026

"""Bounded advisory attachment relationships, separate from deletion selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from codex_session_gc.domain import ThreadId, ThreadOrigin

if TYPE_CHECKING:
    from codex_session_gc.domain import GraphSnapshot


ADVISORY_PROJECTION_LIMIT = 1000


@dataclass(slots=True)
class AdvisoryAncestry:
    """Bounded SQL ancestry and selected-header conflict evidence."""

    rows: tuple[tuple[str, ThreadOrigin, ThreadId | None], ...] = ()
    edges: tuple[tuple[str, str], ...] = ()
    conflicts: set[str] = field(default_factory=set)
    selected_attachments: dict[ThreadId, ThreadId] = field(default_factory=dict)
    truncated: bool = False


def attachment_cleanup(  # noqa: C901
    snapshot: GraphSnapshot,
    selected: frozenset[ThreadId] | None,
    ancestry: AdvisoryAncestry,
    home_identity: tuple[int, int],
) -> dict[str, object]:
    """Describe observed blockers without authorizing or extending a selection."""
    truncated = ancestry.truncated
    parents: dict[str, set[str]] = {}
    for parent_id, child_id in ancestry.edges:
        parents.setdefault(child_id, set()).add(parent_id)
    records = {identifier: (origin, parent) for identifier, origin, parent in ancestry.rows}

    def root(identifier: ThreadId) -> str | None:
        if truncated:
            return None
        current = str(identifier.value)
        visited: set[str] = set()
        while current not in visited and current in records and current not in ancestry.conflicts:
            visited.add(current)
            origin, declared_parent = records[current]
            incoming = parents.get(current, set())
            if origin is ThreadOrigin.MAIN and not incoming:
                return current
            if origin is not ThreadOrigin.SUBAGENT or declared_parent is None:
                return None
            declared = str(declared_parent.value)
            if incoming != {declared}:
                return None
            current = declared
        return None

    sources: dict[ThreadId, set[ThreadId]] = {}
    for item in snapshot.attachments:
        if selected is not None and item.source_thread_id in selected:
            sources.setdefault(item.source_thread_id, set()).add(item.child_thread_id)
    pairs = [(source, child) for source in sorted(sources) for child in sorted(sources[source])]
    if len(pairs) > ADVISORY_PROJECTION_LIMIT:
        pairs = pairs[:ADVISORY_PROJECTION_LIMIT]
        truncated = True
    result: dict[ThreadId, dict[str, object]] = {}
    child_lists: dict[ThreadId, list[dict[str, object]]] = {}
    for source, child in pairs:
        children = child_lists.setdefault(source, [])
        result.setdefault(
            source, {"id": str(source.value), "root_id": root(source), "children": children}
        )
        children.append(
            {
                "id": str(child.value),
                "root_id": root(child),
                "selected": selected is not None and child in selected,
            }
        )
    return {
        "home_identity": list(home_identity),
        "sources": list(result.values()),
        "truncated": truncated,
    }
