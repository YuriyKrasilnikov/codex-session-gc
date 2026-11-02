# Copyright (c) 2026

"""Cooperative per-file limits for bounded local observations."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReadBudget:
    """One maximum file size and one deadline shared by an observation sequence."""

    maximum_bytes: int
    deadline: float

    def __post_init__(self) -> None:
        """Reject invalid resource limits before reading files."""
        if (
            type(self.maximum_bytes) is not int
            or self.maximum_bytes <= 0
            or type(self.deadline) not in (int, float)
            or not math.isfinite(self.deadline)
        ):
            message = "Read bounds must be finite and positive."
            raise ValueError(message)

    def check(self, size: int | None = None) -> None:
        """Check the shared deadline and an optional file's declared size."""
        if time.monotonic() >= self.deadline:
            message = "Observation exceeded its deadline."
            raise ValueError(message)
        if size is not None and (size < 0 or size > self.maximum_bytes):
            message = "Observed file exceeds its byte bound."
            raise ValueError(message)
