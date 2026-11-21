# Copyright (c) 2026

"""Request-local monotonic observations, independent of operation authority."""

from __future__ import annotations

import time
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class Phase(StrEnum):
    """Stable order and names of observed application boundaries."""

    EXECUTOR_WAIT = "executor_wait"
    ADMISSION_LOCK_WAIT = "admission_lock_wait"
    PREVIEW_WORKER_WAIT = "preview_worker_wait"
    SQL_PROJECTION = "sql_projection"
    HEADER_INVENTORY = "header_inventory"
    SELECTION_MANIFEST = "selection_manifest"
    CONFIRMATION = "confirmation"
    RECEIPT_CREATE = "receipt_create"
    ROOT_REQUEST_PREPARE = "root_request_prepare"
    DISPATCH_ROUND_TRIP = "dispatch_round_trip"
    FINAL_RECEIPT_OBSERVE = "final_receipt_observe"
    WORKER_CLEANUP = "worker_cleanup"


class Count(StrEnum):
    """Bounded numeric facts observed at existing execution boundaries."""

    ROOTS_REQUESTED = "roots_requested"
    SELECTED_REQUESTED = "selected_requested"
    EXPLICIT_SELECTION = "explicit_selection"
    SQL_SCOPE_ROWS = "sql_scope_rows"
    SQL_CURRENT_ROWS = "sql_current_rows"
    INVENTORY_ENTRIES = "inventory_entries"
    HEADERS_READ = "headers_read"
    INVENTORY_BUDGET_BYTES = "inventory_budget_bytes"
    ROOTS = "roots"
    CANDIDATES = "candidates"
    ROOTS_STARTED = "roots_started"
    DISPATCH_ENTERED = "dispatch_entered"
    RESULTS_RETURNED = "results_returned"


@dataclass
class _Measurement:
    elapsed_ns: int = 0
    calls: int = 0
    interrupted: bool = False


class TimingObserver:
    """Own one bounded set of aggregate measurements; never call consumer code."""

    def __init__(self, scope: str, clock: Callable[[], int] = time.monotonic_ns) -> None:
        """Start one observation with an injectable monotonic clock."""
        if scope not in {"preview", "apply"}:
            message = "Invalid timing scope"
            raise ValueError(message)
        self.scope = scope
        self._clock = clock
        self._started = clock()
        self._lock = Lock()
        self._measurements: dict[Phase, _Measurement] = {}
        self._active: list[Phase] = []
        self._counts: dict[Count, int] = {}
        self._interrupted = False
        self._finished_ns: int | None = None

    @contextmanager
    def phase(self, name: Phase) -> Iterator[None]:
        """Aggregate repeated intervals and preserve interrupted observations."""
        started = self._clock()
        with self._lock:
            self._active.append(name)
        interrupted = True
        try:
            yield
            interrupted = False
        finally:
            elapsed = max(0, self._clock() - started)
            with self._lock:
                self._active.remove(name)
                item = self._measurements.setdefault(name, _Measurement())
                item.elapsed_ns += elapsed
                item.calls += 1
                item.interrupted |= interrupted

    def count(self, name: Count, amount: int = 1) -> None:
        """Accumulate a numeric observation without storing source values."""
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + amount

    def finish(self, *, interrupted: bool = False) -> None:
        """Record whether the invocation stopped before a complete result."""
        finished = self._clock()
        with self._lock:
            self._interrupted |= interrupted
            if self._finished_ns is None:
                self._finished_ns = finished

    def snapshot(self) -> dict[str, object]:
        """Copy the latest observation for a consumer without exposing source data."""
        now = self._clock()
        with self._lock:
            end = self._finished_ns if self._finished_ns is not None else now
            elapsed = max(0, end - self._started)
            return {
                "scope": self.scope,
                "caller_elapsed_ns": elapsed,
                "worker_elapsed_ns": None,
                "current_stage": self._active[-1].value if self._active else None,
                "phases": [
                    {
                        "name": phase.value,
                        "clock_domain": "caller",
                        "elapsed_ns": item.elapsed_ns,
                        "calls": item.calls,
                        "completion": "interrupted" if item.interrupted else "complete",
                    }
                    for phase in Phase
                    if (item := self._measurements.get(phase)) is not None
                ],
                "counts": dict(self._counts),
                "counts_completion": (
                    "interrupted"
                    if self._interrupted
                    or any(item.interrupted for item in self._measurements.values())
                    else "complete"
                ),
                "missing_phases": [
                    phase.value for phase in Phase if phase not in self._measurements
                ],
            }


_current: ContextVar[TimingObserver | None] = ContextVar("timing_observer", default=None)


@contextmanager
def observe(observer: TimingObserver) -> Iterator[None]:
    """Bind an observer to this invocation and restore the previous context."""
    token = _current.set(observer)
    try:
        yield
    finally:
        _current.reset(token)


def interval(name: Phase) -> AbstractContextManager[None]:
    """Return a no-op when this invocation did not opt in."""
    observer = _current.get()
    return observer.phase(name) if observer is not None else nullcontext()


def count(name: Count, amount: int = 1) -> None:
    """Record a count only for an observed invocation."""
    observer = _current.get()
    if observer is not None:
        observer.count(name, amount)
