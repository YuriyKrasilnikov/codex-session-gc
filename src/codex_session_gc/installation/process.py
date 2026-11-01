# Copyright (c) 2026

"""Bounded, owned subprocess lifecycle for verification and installation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class ProcessFailure(StrEnum):
    """A child could not complete within its owned execution boundary."""

    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    TIMEOUT = "timeout"
    OUTPUT = "output"
    STORAGE = "storage"


class OwnedProcessError(Exception):
    """A bounded child failed or exceeded its output/time budget."""

    def __init__(self, reason: ProcessFailure) -> None:
        """Retain the specific process failure."""
        self.reason = reason
        super().__init__(reason.value)


def _wake(ready: asyncio.Future[None]) -> None:
    if not ready.done():
        ready.set_result(None)


async def _read_pipe(fd: int, limit: int) -> bytes:
    loop = asyncio.get_running_loop()
    output = bytearray()
    while True:
        ready: asyncio.Future[None] = loop.create_future()
        loop.add_reader(fd, _wake, ready)
        try:
            await ready
        finally:
            loop.remove_reader(fd)
        try:
            block = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if not block:
            return bytes(output)
        output.extend(block)
        if len(output) > limit:
            raise OwnedProcessError(ProcessFailure.OUTPUT)


async def _cleanup(
    creation: asyncio.Task[asyncio.subprocess.Process] | None,
    process: asyncio.subprocess.Process | None,
    read_fd: int,
    write_fd: int,
) -> None:
    try:
        if process is None and creation is not None:
            with contextlib.suppress(OSError):
                process = await creation
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
    finally:
        try:
            os.close(read_fd)
        finally:
            if write_fd >= 0:
                os.close(write_fd)


async def _await_cleanup(cleanup: asyncio.Task[None]) -> None:
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


def _tree_usage(root: Path) -> tuple[int, int]:
    """Observe logical and allocated bytes without following filesystem aliases."""
    pending = [os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    logical = allocated = 0
    try:
        while pending:
            directory = pending.pop()
            try:
                info = os.fstat(directory)
                allocated += info.st_blocks * 512
                with contextlib.suppress(FileNotFoundError), os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISDIR(info.st_mode):
                                child = os.open(
                                    entry.name,
                                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=directory,
                                )
                                pending.append(child)
                            else:
                                logical += info.st_size
                                allocated += info.st_blocks * 512
                        except FileNotFoundError:
                            continue
            finally:
                os.close(directory)
    finally:
        for directory in pending:
            os.close(directory)
    return logical, allocated


@dataclass(frozen=True, slots=True)
class StorageBudget:
    """Observe cumulative owned-tree growth and a free-space floor."""

    root: Path
    baseline: tuple[int, int]
    growth_bytes: int
    free_floor_bytes: int

    def check(self) -> None:
        """Refuse an exceeded budget or an unobservable filesystem."""
        try:
            logical, allocated = _tree_usage(self.root)
            volume = os.statvfs(self.root)
        except OSError as error:
            raise OwnedProcessError(ProcessFailure.STORAGE) from error
        if (
            max(logical - self.baseline[0], allocated - self.baseline[1]) > self.growth_bytes
            or volume.f_bavail * volume.f_frsize < self.free_floor_bytes
        ):
            raise OwnedProcessError(ProcessFailure.STORAGE)

    @classmethod
    def capture(cls, root: Path, growth_bytes: int, free_floor_bytes: int) -> StorageBudget:
        """Bind the growth baseline before starting the first environment subprocess."""
        if (
            type(growth_bytes) is not int
            or growth_bytes < 0
            or type(free_floor_bytes) is not int
            or free_floor_bytes < 0
        ):
            raise OwnedProcessError(ProcessFailure.STORAGE)
        try:
            baseline = _tree_usage(root)
        except OSError as error:
            raise OwnedProcessError(ProcessFailure.STORAGE) from error
        budget = cls(root, baseline, growth_bytes, free_floor_bytes)
        budget.check()
        return budget


async def _watch_storage(budget: StorageBudget) -> None:
    while True:
        budget.check()
        await asyncio.sleep(0.05)


async def _collect_child(
    process: asyncio.subprocess.Process, read_fd: int, limits: ProcessLimits
) -> bytes:
    output = await _read_pipe(read_fd, limits.output_bytes)
    await process.wait()
    if process.returncode != 0:
        raise OwnedProcessError(ProcessFailure.FAILED)
    return output


async def _observe_child(
    process: asyncio.subprocess.Process, read_fd: int, limits: ProcessLimits
) -> bytes:
    if limits.storage is None:
        return await _collect_child(process, read_fd, limits)
    work = asyncio.create_task(_collect_child(process, read_fd, limits))
    watch = asyncio.create_task(_watch_storage(limits.storage))
    try:
        done, _ = await asyncio.wait((work, watch), return_when=asyncio.FIRST_COMPLETED)
        if watch in done:
            await watch
        return await work
    finally:
        for task in (work, watch):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watch, return_exceptions=True)


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    """Bound the owned child and its captured output."""

    seconds: float
    output_bytes: int
    storage: StorageBudget | None = None


async def run_owned(
    command: list[str],
    *,
    cwd: Path | str,
    environment: dict[str, str],
    limits: ProcessLimits,
    pass_fds: tuple[int, ...] = (),
) -> bytes:
    """Run one child in an owned process group and reap it before returning or cancelling."""
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    os.set_blocking(read_fd, False)
    process: asyncio.subprocess.Process | None = None
    creation: asyncio.Task[asyncio.subprocess.Process] | None = None
    try:
        creation = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=write_fd,
                stderr=asyncio.subprocess.DEVNULL,
                pass_fds=pass_fds,
                start_new_session=True,
            )
        )
        try:
            async with asyncio.timeout(limits.seconds):
                process = await asyncio.shield(creation)
                os.close(write_fd)
                write_fd = -1
                output = await _observe_child(process, read_fd, limits)
        except TimeoutError as exc:
            raise OwnedProcessError(ProcessFailure.TIMEOUT) from exc
    except OSError as exc:
        raise OwnedProcessError(ProcessFailure.UNAVAILABLE) from exc
    finally:
        cleanup = asyncio.create_task(_cleanup(creation, process, read_fd, write_fd))
        await _await_cleanup(cleanup)
    if limits.storage is not None:
        limits.storage.check()
    return output
