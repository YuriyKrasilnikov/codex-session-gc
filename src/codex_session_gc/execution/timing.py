# Copyright (c) 2026

"""Request-local monotonic observations, independent of operation authority."""

from __future__ import annotations

import asyncio
import time
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, ParamSpec, TypeVar

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
        self._worker: dict[str, object] | None = None

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

    def accept_worker(self, value: object) -> bool:  # noqa: C901, PLR0911
        """Accept only a bounded summary from the invocation's private worker."""
        if not isinstance(value, dict) or value.get("scope") != "preview":
            return False
        elapsed, phases, counts = (
            value.get("caller_elapsed_ns"),
            value.get("phases"),
            value.get("counts"),
        )
        if type(elapsed) is not int or elapsed < 0 or not isinstance(phases, list):
            return False
        if len(phases) > len(Phase) or not isinstance(counts, dict) or len(counts) > len(Count):
            return False
        accepted = []
        for item in phases:
            if not isinstance(item, dict):
                return False
            name = item.get("name")
            if not isinstance(name, str):
                return False
            try:
                phase = Phase(name)
            except ValueError, TypeError:
                return False
            duration, calls = item.get("elapsed_ns"), item.get("calls")
            if (
                type(duration) is not int
                or duration < 0
                or type(calls) is not int
                or calls < 1
                or item.get("completion") not in {"complete", "interrupted"}
            ):
                return False
            accepted.append(
                {
                    "name": phase.value,
                    "clock_domain": "worker",
                    "elapsed_ns": duration,
                    "calls": calls,
                    "completion": item["completion"],
                }
            )
        accepted_counts: dict[str, int] = {}
        for name, amount in counts.items():
            try:
                key = Count(name)
            except ValueError, TypeError:
                return False
            if type(amount) is not int or amount < 0:
                return False
            accepted_counts[key.value] = amount
        completion = value.get("counts_completion")
        if completion not in {"complete", "interrupted"}:
            return False
        with self._lock:
            self._worker = {
                "elapsed": elapsed,
                "phases": accepted,
                "counts": accepted_counts,
                "counts_completion": completion,
            }
        return True

    def snapshot(self) -> dict[str, object]:
        """Copy the latest observation for a consumer without exposing source data."""
        now = self._clock()
        with self._lock:
            end = self._finished_ns if self._finished_ns is not None else now
            elapsed = max(0, end - self._started)
            phases: list[dict[str, object]] = [
                {
                    "name": phase.value,
                    "clock_domain": "caller",
                    "elapsed_ns": item.elapsed_ns,
                    "calls": item.calls,
                    "completion": "interrupted" if item.interrupted else "complete",
                }
                for phase in Phase
                if (item := self._measurements.get(phase)) is not None
            ]
            worker_phases = self._worker["phases"] if self._worker else []
            if isinstance(worker_phases, list):
                phases.extend(worker_phases)
            order = {phase.value: index for index, phase in enumerate(Phase)}
            phases.sort(key=lambda item: order[str(item["name"])])
            return {
                "scope": self.scope,
                "caller_elapsed_ns": elapsed,
                "worker_elapsed_ns": self._worker["elapsed"] if self._worker else None,
                "current_stage": self._active[-1].value if self._active else None,
                "phases": phases,
                "worker_counts": self._worker["counts"] if self._worker else {},
                "worker_counts_completion": (
                    self._worker["counts_completion"] if self._worker else None
                ),
                "counts": dict(self._counts),
                "counts_completion": (
                    "interrupted"
                    if self._interrupted
                    or any(item.interrupted for item in self._measurements.values())
                    else "complete"
                ),
                "missing_phases": [
                    phase.value
                    for phase in Phase
                    if not any(item["name"] == phase.value for item in phases)
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


def current() -> TimingObserver | None:
    """Return the observer belonging to the current async or thread context."""
    return _current.get()


_P = ParamSpec("_P")
_R = TypeVar("_R")


async def measured_to_thread(  # noqa: UP047
    function: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs
) -> _R:
    """Measure submission to thread entry, including submission failure exactly once."""
    waiting = interval(Phase.EXECUTOR_WAIT)
    waiting.__enter__()
    guard = Lock()
    closed = False

    def close_wait(error: BaseException | None = None) -> None:
        nonlocal closed
        with guard:
            if not closed:
                closed = True
                if error is None:
                    waiting.__exit__(None, None, None)
                else:
                    waiting.__exit__(type(error), error, error.__traceback__)

    def run() -> _R:
        close_wait()
        return function(*args, **kwargs)

    try:
        return await asyncio.to_thread(run)
    except BaseException as error:
        close_wait(error)
        raise
    finally:
        close_wait()


_worker_signal: ContextVar[asyncio.Event | None] = ContextVar("worker_signal", default=None)


@contextmanager
def worker_wakeup(event: asyncio.Event) -> Iterator[None]:
    """Bind one request-owned event to the async caller context."""
    token = _worker_signal.set(event)
    try:
        yield
    finally:
        _worker_signal.reset(token)


def signal_worker_summary() -> None:
    """Wake the current request's consumer from its event-loop task."""
    event = _worker_signal.get()
    if event is not None:
        event.set()
