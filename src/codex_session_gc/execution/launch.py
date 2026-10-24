# Copyright (c) 2026

"""Receipt identity for one owned native-capable launch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.recovery import AttemptId, LaunchId, LaunchPurpose


@dataclass(frozen=True, slots=True)
class LaunchBinding:
    """Bind a process incarnation to its durable attempt and launch generation."""

    receipt: Path
    attempt_id: AttemptId
    launch_id: LaunchId
    generation: int
    purpose: LaunchPurpose
