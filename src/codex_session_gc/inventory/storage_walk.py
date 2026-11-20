# Copyright (c) 2026

"""Bounded, descriptor-relative observation of one Linux mount."""

from __future__ import annotations

import os
import stat
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class MountIdentity:
    """Identity of an inode on a particular mount."""

    dev: int
    ino: int
    mount_id: int


@dataclass(frozen=True)
class StorageEntry:
    """One observed directory entry."""

    parts: tuple[str, ...]
    stat: os.stat_result
    identity: MountIdentity


@dataclass(frozen=True)
class StorageGap:
    """Aggregated reason for incomplete observation."""

    reason: str
    count: int


@dataclass(frozen=True)
class StorageScan:
    """Immutable result of one filesystem observation."""

    home: Path
    home_identity: MountIdentity
    entries: tuple[StorageEntry, ...]
    root_complete: bool
    gaps: tuple[StorageGap, ...]


class StorageScanError(ValueError):
    """The root could not be safely observed."""


class StorageScanCancelled(ValueError):  # noqa: N818 - public API name
    """The caller cancelled the observation."""


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_LEAF_FLAGS = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC


def identity_for_fd(fd: int, info: os.stat_result | None = None) -> MountIdentity:
    """Read the inode and Linux mount identity for an open descriptor."""
    info = info if info is not None else os.fstat(fd)
    with Path(f"/proc/self/fdinfo/{fd}").open(encoding="ascii") as stream:
        for line in stream:
            if line.startswith("mnt_id:"):
                return MountIdentity(info.st_dev, info.st_ino, int(line.split()[1]))
    msg = "mount identity unavailable"
    raise ValueError(msg)


def open_root(home: Path) -> int:
    """Open an absolute root without following links in any component.

    The caller owns the returned descriptor. Errors may contain OS details and
    must be sanitized at a public interface.
    """
    if not home.is_absolute():
        msg = "storage root must be absolute"
        raise ValueError(msg)
    current = os.open("/", _DIR_FLAGS)
    try:
        for component in home.parts[1:]:
            child = os.open(component, _DIR_FLAGS, dir_fd=current)
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    else:
        return current


def _stamp(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def scan_storage(  # noqa: C901, PLR0912, PLR0915
    home: Path, *, cancelled: Callable[[], bool], max_operations: int, max_path_bytes: int
) -> StorageScan:
    """Observe entries without following links or crossing mount boundaries.

    The path budget conservatively charges the full relative path for every
    retained record and queued directory, including repeated shared prefixes.
    """
    home = Path(os.path.abspath(os.fspath(home)))  # noqa: PTH100 - do not resolve symlinks
    gaps: Counter[str] = Counter()
    entries: list[StorageEntry] = []
    pending: deque[tuple[tuple[str, ...], tuple[MountIdentity, ...], int]] = deque()
    used = 0
    operations = 0
    root_complete = False

    def check() -> None:
        if cancelled():
            msg = "storage scan cancelled"
            raise StorageScanCancelled(msg)

    def operation() -> bool:
        nonlocal operations
        check()
        if operations >= max_operations:
            return False
        operations += 1
        return True

    def cost(parts: tuple[str, ...]) -> int:
        # Count separators as well as names, and give empty/root paths a cost.
        return max(1, sum(len(os.fsencode(part)) + 1 for part in parts))

    check()
    try:
        root_fd = open_root(home)
    except OSError:
        msg = "storage root unavailable"
        raise StorageScanError(msg) from None
    try:
        try:
            root_stat = os.fstat(root_fd)
            root_identity = identity_for_fd(root_fd, root_stat)
        except OSError, ValueError, IndexError:
            msg = "storage root identity unavailable"
            raise StorageScanError(msg) from None
        entries.append(StorageEntry((), root_stat, root_identity))
        pending.append(((), (), 0))
        visited = {root_identity}
        exhausted = False
        while pending:
            check()
            parts, identities, queue_cost = pending.popleft()
            used -= queue_cost
            try:
                current = os.dup(root_fd)
            except OSError:
                gaps["directory_unavailable"] += 1
                continue
            try:
                valid = True
                for name, expected in zip(parts, identities, strict=True):
                    if not operation():
                        gaps["operation_limit"] += 1
                        exhausted = True
                        valid = False
                        break
                    try:
                        child = os.open(name, _DIR_FLAGS, dir_fd=current)
                        try:
                            actual = identity_for_fd(child)
                        except BaseException:
                            os.close(child)
                            raise
                    except OSError, ValueError, IndexError:
                        gaps["directory_unavailable"] += 1
                        valid = False
                        break
                    os.close(current)
                    current = child
                    if actual != expected:
                        gaps["directory_changed"] += 1
                        valid = False
                        break
                if not valid:
                    if exhausted:
                        if pending:
                            gaps["operation_limit"] += len(pending)
                        break
                    continue
                try:
                    before = os.fstat(current)
                    with os.scandir(current) as iterator:
                        complete = True
                        while True:
                            check()
                            try:
                                item = next(iterator)
                            except StopIteration:
                                break
                            if not operation():
                                gaps["operation_limit"] += 1
                                exhausted = True
                                complete = False
                                break
                            root_candidate = (
                                not parts
                                and item.name.startswith("state_")
                                and item.name.endswith(".sqlite")
                            )
                            child_parts = (*parts, item.name)
                            size = cost(child_parts)
                            if used + size > max_path_bytes:
                                gaps["path_limit"] += 1
                                if root_candidate:
                                    complete = False
                                continue
                            try:
                                leaf = os.open(item.name, _LEAF_FLAGS, dir_fd=current)
                                try:
                                    info = os.fstat(leaf)
                                    identity = identity_for_fd(leaf, info)
                                finally:
                                    os.close(leaf)
                            except OSError, ValueError, IndexError:
                                gaps["entry_unavailable"] += 1
                                if root_candidate:
                                    complete = False
                                continue
                            if identity.mount_id != root_identity.mount_id:
                                gaps["mount_boundary"] += 1
                                if root_candidate:
                                    complete = False
                                continue
                            entries.append(StorageEntry(child_parts, info, identity))
                            used += size
                            if stat.S_ISDIR(info.st_mode):
                                if identity in visited:
                                    gaps["directory_repeated"] += 1
                                elif used + size > max_path_bytes:
                                    gaps["path_limit"] += 1
                                else:
                                    visited.add(identity)
                                    pending.append((child_parts, (*identities, identity), size))
                                    used += size
                        after = os.fstat(current)
                        if _stamp(before) != _stamp(after):
                            gaps["directory_changed"] += 1
                            complete = False
                        if not parts:
                            root_complete = complete
                except OSError:
                    gaps["directory_unavailable"] += 1
                    if not parts:
                        root_complete = False
            finally:
                os.close(current)
            if exhausted:
                if pending:
                    gaps["operation_limit"] += len(pending)
                break
        check()
        try:
            reopened = open_root(home)
            try:
                same = identity_for_fd(reopened) == root_identity
            finally:
                os.close(reopened)
        except OSError, ValueError, IndexError:
            msg = "storage root changed or unavailable"
            raise StorageScanError(msg) from None
        if not same:
            msg = "storage root changed or unavailable"
            raise StorageScanError(msg)
        return StorageScan(
            home,
            root_identity,
            tuple(entries),
            root_complete,
            tuple(StorageGap(reason, count) for reason, count in sorted(gaps.items())),
        )
    finally:
        os.close(root_fd)
