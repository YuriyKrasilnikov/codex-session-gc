# Copyright (c) 2026
"""Bounded labels and human-readable stored history totals."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codex_session_gc.inventory.query import Totals


def cwd_label(cwd: str, limit: int = 50) -> tuple[str, bool]:
    """Escape control characters before bounding the stored cwd preview."""
    if not cwd:
        return "cwd not specified", False
    escaped = "".join(c if c.isprintable() else c.encode("unicode_escape").decode() for c in cwd)
    return escaped[:limit], len(escaped) > limit


def totals_label(total: Totals) -> str:
    """Show counts and distinguish exact totals from lower bounds."""
    size = (
        str(total.stored_bytes)
        if total.stored_bytes is not None
        else f">={total.known_stored_bytes}"
    )
    return f"{total.history_count} histories, {size} B, {total.incomplete_history_count} incomplete"
