# Copyright (c) 2026

"""Detached physical rollout metadata for selected-work observation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.domain import ArchiveState, RolloutId, ThreadId


@dataclass(frozen=True, slots=True)
class RolloutObservation:
    """One physically distinct declared rollout file."""

    path: Path
    rollout_id: RolloutId | None
    thread_id: ThreadId | None
    archive_state: ArchiveState
    stored_bytes: int | None
    allocated_bytes: int | None
    metadata_origin: str | None
    metadata_spawn_parent: ThreadId | None
    history_provider_rollout_id: RolloutId | None
    metadata_guardian_parent: ThreadId | None = None
