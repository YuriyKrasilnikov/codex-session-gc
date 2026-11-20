# Copyright (c) 2026
# ruff: noqa: ANN001, ANN003, ANN201, ANN202, ANN204, ASYNC110, D101, D102, D103, D107, EM101, PLR2004, PT011, S101, TC003, TRY003
"""Storage service generation and cancellation boundaries."""

import asyncio
import threading
from pathlib import Path

import pytest

from codex_session_gc.cli import storage_service


class Snapshot:
    def __init__(self, identity):
        self.storage_snapshot_id = identity

    def summary(self):
        return {"storage_snapshot_id": self.storage_snapshot_id}

    def page(self, **kwargs):
        return {"storage_snapshot_id": self.storage_snapshot_id, **kwargs}


def test_shared_generation_stale_reads_and_failed_refresh(monkeypatch, tmp_path: Path):
    async def scenario():
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def observe(_home, stop):
            calls.append(stop)
            if len(calls) == 1:
                entered.set()
                release.wait(5)
                return Snapshot("storage/one")
            raise RuntimeError("failed refresh")

        monkeypatch.setattr(storage_service, "_observe", observe)
        service = storage_service.StorageService(tmp_path)
        first = asyncio.create_task(service.overview())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        second = asyncio.create_task(service.overview(refresh=True))
        await asyncio.sleep(0)
        release.set()
        assert await first == await second == {"storage_snapshot_id": "storage/one"}
        assert len(calls) == 1
        with pytest.raises(RuntimeError, match="failed refresh"):
            await service.overview(refresh=True)
        assert service.objects("storage/one")["storage_snapshot_id"] == "storage/one"
        with pytest.raises(ValueError):
            service.objects("storage/old")
        await service.close()

    asyncio.run(scenario())


def test_last_cancelled_subscriber_is_joined_before_new_scan(monkeypatch, tmp_path: Path):
    async def scenario():
        entered = threading.Event()
        stopped = threading.Event()
        release = threading.Event()
        calls = []

        def observe(_home, stop):
            calls.append(stop)
            if len(calls) == 1:
                entered.set()
                stop.wait(5)
                stopped.set()
                release.wait(5)
                return Snapshot("storage/abandoned")
            return Snapshot("storage/new")

        monkeypatch.setattr(storage_service, "_observe", observe)
        service = storage_service.StorageService(tmp_path)
        first = asyncio.create_task(service.overview())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        while not stopped.is_set():
            await asyncio.sleep(0.01)
        second = asyncio.create_task(service.overview())
        await asyncio.sleep(0.02)
        assert len(calls) == 1
        release.set()
        assert (await second)["storage_snapshot_id"] == "storage/new"
        assert len(calls) == 2
        await service.close()

    asyncio.run(scenario())
