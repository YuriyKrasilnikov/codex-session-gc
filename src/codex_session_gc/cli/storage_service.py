# Copyright (c) 2026

"""Lifetime ownership for interactive storage observations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from codex_session_gc.inventory.storage import PAGE_LIMIT, observe_storage

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.inventory.storage import StorageSnapshot


def _observe(home: Path, stop: threading.Event) -> StorageSnapshot:
    return observe_storage(home, cancelled=stop.is_set)


@dataclass(slots=True)
class _Flight:
    task: asyncio.Task[StorageSnapshot]
    stop: threading.Event
    waiters: int = 0


async def _settle(task: asyncio.Task[StorageSnapshot]) -> None:
    await asyncio.gather(asyncio.shield(task), return_exceptions=True)


class StorageService:
    """Own one in-flight cooperative observation and one published snapshot."""

    def __init__(self, home: Path) -> None:
        """Bind observations to the home selected by the application."""
        self.home = home
        self.snapshot: StorageSnapshot | None = None
        self._flight: _Flight | None = None
        self._closed = False

    async def _collect(self, stop: threading.Event) -> StorageSnapshot:
        result = await asyncio.to_thread(_observe, self.home, stop)
        if not self._closed and not stop.is_set():
            self.snapshot = result
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
            flight.waiters += 1
            try:
                return (await asyncio.shield(flight.task)).summary()
            finally:
                flight.waiters -= 1
                if flight.waiters == 0 and not flight.task.done():
                    flight.stop.set()

    def objects(
        self,
        storage_snapshot_id: str,
        *,
        association: str = "all",
        offset: int = 0,
        limit: int = PAGE_LIMIT,
    ) -> dict[str, Any]:
        """Read a page belonging to this service's active snapshot."""
        snapshot = self.snapshot
        if snapshot is None or storage_snapshot_id != snapshot.storage_snapshot_id:
            msg = "The storage snapshot has expired; request the current overview."
            raise ValueError(msg)
        return snapshot.page(association=association, offset=offset, limit=limit)

    async def wait_idle(self) -> None:
        """Join the observation still owned by the service, if any."""
        flight = self._flight
        if flight is not None:
            await _settle(flight.task)

    async def close(self) -> None:
        """Stop observation and settle outstanding worker ownership."""
        self._closed = True
        self.snapshot = None
        flight = self._flight
        if flight is not None:
            flight.stop.set()
            await _settle(flight.task)
