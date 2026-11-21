# Copyright (c) 2026

"""Lifetime ownership for interactive storage observations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from codex_session_gc.inventory.storage import PAGE_LIMIT, observe_storage
from codex_session_gc.inventory.storage_diagnostics import observe_diagnostics

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.inventory.storage import StorageSnapshot
    from codex_session_gc.inventory.storage_diagnostics import DiagnosticObservation, Direction


def _observe(home: Path, stop: threading.Event) -> StorageSnapshot:
    return observe_storage(home, cancelled=stop.is_set)


@dataclass(slots=True)
class _Flight[T]:
    task: asyncio.Task[T]
    stop: threading.Event
    waiters: int = 0


async def _settle(task: asyncio.Task[object]) -> None:
    await asyncio.gather(asyncio.shield(task), return_exceptions=True)


async def _joined[T](flight: _Flight[T]) -> T:
    flight.waiters += 1
    try:
        return await asyncio.shield(flight.task)
    finally:
        flight.waiters -= 1
        if flight.waiters == 0 and not flight.task.done():
            flight.stop.set()


class StorageService:
    """Own storage and diagnostic generations and settle their workers on close."""

    def __init__(self, home: Path) -> None:
        """Bind observations to the home selected by the application."""
        self.home = home
        self.snapshot: StorageSnapshot | None = None
        self._flight: _Flight[StorageSnapshot] | None = None
        self._diagnostic: tuple[bool, DiagnosticObservation] | None = None
        self._diagnostic_flight: tuple[bool, _Flight[DiagnosticObservation]] | None = None
        self._closed = False

    async def _collect(self, stop: threading.Event) -> StorageSnapshot:
        result = await asyncio.to_thread(_observe, self.home, stop)
        if not self._closed and not stop.is_set():
            self.snapshot = result
            self._diagnostic = None
            if self._diagnostic_flight is not None:
                self._diagnostic_flight[1].stop.set()
        return result

    def _finish(self, task: asyncio.Task[StorageSnapshot]) -> None:
        if self._flight is not None and self._flight.task is task:
            self._flight = None
        if not task.cancelled():
            task.exception()

    async def overview(self, *, refresh: bool = False) -> dict[str, Any]:
        """Join an active observation or return the current summary."""
        while True:
            if self._closed:
                msg = "Storage service is closed."
                raise RuntimeError(msg)
            if not refresh and self.snapshot is not None:
                return self.snapshot.summary()
            flight = self._flight
            if flight is None:
                stop = threading.Event()
                task = asyncio.create_task(self._collect(stop))
                task.add_done_callback(self._finish)
                flight = self._flight = _Flight(task, stop)
            if flight.stop.is_set():
                await _settle(flight.task)
                continue
            return (await _joined(flight)).summary()

    def _snapshot(self, storage_snapshot_id: str) -> StorageSnapshot:
        snapshot = self.snapshot
        if snapshot is None or storage_snapshot_id != snapshot.storage_snapshot_id:
            msg = "The storage snapshot has expired; request the current overview."
            raise ValueError(msg)
        return snapshot

    def objects(
        self,
        storage_snapshot_id: str,
        *,
        association: str = "all",
        offset: int = 0,
        limit: int = PAGE_LIMIT,
    ) -> dict[str, Any]:
        """Read a page belonging to this service's active snapshot."""
        return self._snapshot(storage_snapshot_id).page(
            association=association, offset=offset, limit=limit
        )

    async def _collect_diagnostic(
        self, snapshot: StorageSnapshot, stop: threading.Event, *, providers: bool
    ) -> DiagnosticObservation:
        result = await asyncio.to_thread(
            observe_diagnostics, self.home, snapshot, cancelled=stop.is_set, providers=providers
        )
        if self.snapshot is not snapshot or self._closed or stop.is_set():
            msg = "The diagnostic observation stopped before publication."
            raise ValueError(msg)
        self._diagnostic = (providers, result)
        return result

    def _finish_diagnostic(self, task: asyncio.Task[DiagnosticObservation]) -> None:
        if self._diagnostic_flight is not None and self._diagnostic_flight[1].task is task:
            self._diagnostic_flight = None
        if not task.cancelled():
            task.exception()

    async def diagnostics(
        self, storage_snapshot_id: str, *, refresh: bool = False, providers: bool = False
    ) -> dict[str, Any]:
        """Join a diagnostic worker and publish only for the current storage generation."""
        while True:
            snapshot = self._snapshot(storage_snapshot_id)
            diagnostic = self._diagnostic
            if not refresh and diagnostic is not None and diagnostic[0] == providers:
                return diagnostic[1].summary()
            active = self._diagnostic_flight
            if active is None:
                stop = threading.Event()
                task = asyncio.create_task(
                    self._collect_diagnostic(snapshot, stop, providers=providers)
                )
                task.add_done_callback(self._finish_diagnostic)
                active = self._diagnostic_flight = (providers, _Flight(task, stop))
            mode, flight = active
            if flight.stop.is_set() or mode != providers:
                await _settle(flight.task)
                continue
            result = await _joined(flight)
            self._snapshot(storage_snapshot_id)
            return result.summary()

    def _current_diagnostic(self, diagnostic_id: str) -> DiagnosticObservation:
        saved = self._diagnostic
        if saved is None or saved[1].diagnostic_id != diagnostic_id:
            msg = "The diagnostic observation has expired; request current diagnostics."
            raise ValueError(msg)
        return saved[1]

    def diagnostic_subjects(
        self, diagnostic_id: str, *, reason: str, offset: int, limit: int
    ) -> dict[str, Any]:
        """Read a bounded page of diagnostic subjects without reobserving storage."""
        return self._current_diagnostic(diagnostic_id).subjects(
            reason=reason, offset=offset, limit=limit
        )

    def explain(
        self, diagnostic_id: str, subject_id: str, *, direction: Direction, offset: int, limit: int
    ) -> dict[str, Any]:
        """Explain one subject and page through its evidence from the same observation."""
        return self._current_diagnostic(diagnostic_id).explain(
            subject_id, direction=direction, offset=offset, limit=limit
        )

    async def wait_idle(self) -> None:
        """Join the observation still owned by the service, if any."""
        flight = self._flight
        if flight is not None:
            await _settle(flight.task)
        diagnostic_flight = self._diagnostic_flight
        if diagnostic_flight is not None:
            await _settle(diagnostic_flight[1].task)

    async def close(self) -> None:
        """Stop observation and settle outstanding worker ownership."""
        self._closed = True
        self.snapshot = None
        self._diagnostic = None
        diagnostic_flight = self._diagnostic_flight
        for flight in (self._flight, diagnostic_flight[1] if diagnostic_flight else None):
            if flight is not None:
                flight.stop.set()
        await self.wait_idle()
