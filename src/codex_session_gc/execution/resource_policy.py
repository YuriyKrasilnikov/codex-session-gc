# Copyright (c) 2026

"""Shared resource bounds for selected-work observation and native dispatch."""

from codex_session_gc.execution.closed_work_apply import DispatchLimits
from codex_session_gc.execution.closed_work_request import CollectionLimits

READ_BYTES = 256 * 1024**3
TIMEOUT_SECONDS = 6 * 60 * 60
REQUEST_BYTES = 16 * 1024**2
HEADER_BYTES = 1024**2
OUTPUT_BYTES = 1024**2
CLEANUP_SECONDS = 10


def collection_limits(
    maximum_bytes: int = READ_BYTES, timeout_seconds: float = TIMEOUT_SECONDS
) -> CollectionLimits:
    """Bound one observation without enlarging a caller-supplied limit."""
    return CollectionLimits(
        maximum_bytes,
        maximum_bytes,
        maximum_bytes,
        HEADER_BYTES,
        REQUEST_BYTES,
        timeout_seconds,
    )


def dispatch_limits(timeout_seconds: float = TIMEOUT_SECONDS) -> DispatchLimits:
    """Bound one native invocation and its owned shutdown grace period."""
    return DispatchLimits(timeout_seconds, REQUEST_BYTES, OUTPUT_BYTES, CLEANUP_SECONDS)
