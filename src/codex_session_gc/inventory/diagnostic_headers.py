# Copyright (c) 2026
# ruff: noqa: C901, PLR0912, PLR0915, PLR2004, TRY003, TRY301, EM101, FBT003

"""Bounded, descriptor-verified metadata observations of frozen scan entries."""

from __future__ import annotations

import json
import os
import stat
import time
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING

from codex_session_gc.domain import HistoryMode, PhysicalRepresentation
from codex_session_gc.domain.physical import ownership_from_path
from codex_session_gc.execution.bounded_header import (
    HeaderChangedError,
    HeaderLimitError,
    HeaderReadError,
    HeaderReadLimits,
    read_bounded_header,
)
from codex_session_gc.execution.closed_work_request import HeaderObservation
from codex_session_gc.execution.physical_inventory import _metadata
from codex_session_gc.inventory.storage_walk import identity_for_fd, open_root

if TYPE_CHECKING:
    from collections.abc import Callable

    from codex_session_gc.domain import (
        Attachment,
        HistoryPrefix,
        LogicalForkBoundary,
        OwnershipEdge,
        RolloutId,
    )
    from codex_session_gc.inventory.storage_walk import StorageEntry, StorageScan


class HeaderReason(StrEnum):
    """Finite reasons that can be exposed without filesystem or parser details."""

    OBSERVED = "observed"
    INVALID_LOCATOR = "invalid_locator"
    AMBIGUOUS = "ambiguous"
    CHANGED = "changed"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    MALFORMED = "malformed"
    LIMIT = "limit"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class HeaderFacts:
    """Validated provenance without raw metadata."""

    ownership: OwnershipEdge
    history_mode: HistoryMode
    history_prefix: HistoryPrefix | None
    logical_fork: LogicalForkBoundary | None
    attachment: Attachment | None


@dataclass(frozen=True, slots=True)
class HeaderResult:
    """One finite outcome for one frozen entry."""

    reason: HeaderReason
    facts: HeaderFacts | None = None
    locator_verified: bool = False


@dataclass(frozen=True, slots=True)
class HeaderLimits:
    """Caller limits, bounded by the admitted profile."""

    max_files: int = 16384
    max_physical_bytes: int = 128 * 1024 * 1024
    max_decoded_bytes: int = 128 * 1024 * 1024
    max_operations: int = 250000
    deadline_seconds: float = 180.0

    def __post_init__(self) -> None:
        """Reject invalid or excessive caller limits."""
        if (
            any(
                type(v) is not int or v < 1
                for v in (
                    self.max_files,
                    self.max_physical_bytes,
                    self.max_decoded_bytes,
                    self.max_operations,
                )
            )
            or not 0 < self.deadline_seconds <= 180
        ):
            raise ValueError("Invalid header limits.")
        if (
            self.max_files > 16384
            or self.max_physical_bytes > 128 * 1024 * 1024
            or self.max_decoded_bytes > 128 * 1024 * 1024
            or self.max_operations < 2
            or self.max_operations > 250000
        ):
            raise ValueError("Header limits exceed the admitted profile.")


@dataclass(frozen=True, slots=True)
class HeaderBatch:
    """Observations, root validity, and actual resource use."""

    results: tuple[HeaderResult, ...]
    root_valid: bool
    operations: int
    physical_bytes: int
    decoded_bytes: int


class _StopError(Exception):
    def __init__(self, reason: HeaderReason) -> None:
        self.reason = reason


DEFAULT_HEADER_LIMITS = HeaderLimits()


def observe_headers(
    scan: StorageScan,
    entries: tuple[StorageEntry, ...],
    *,
    cancelled: Callable[[], bool],
    limits: HeaderLimits = DEFAULT_HEADER_LIMITS,
) -> HeaderBatch:
    """Read only named frozen entries; callers must discard results when root_valid is false."""
    deadline = time.monotonic() + limits.deadline_seconds
    operations = physical = decoded = 0
    known = {entry.parts: entry for entry in scan.entries}
    results: list[HeaderResult] = []
    rollout_ids: list[RolloutId | None] = []
    for entry in entries:
        try:
            representation = (
                PhysicalRepresentation.ZSTD
                if entry.parts[-1].endswith(".jsonl.zst")
                else PhysicalRepresentation.PLAIN
            )
            rollout_ids.append(
                ownership_from_path(scan.home.joinpath(*entry.parts), representation).rollout_id
            )
        except ValueError, IndexError:
            rollout_ids.append(None)
    rollout_counts = Counter(identifier for identifier in rollout_ids if identifier is not None)

    def check() -> None:
        if cancelled():
            raise _StopError(HeaderReason.CANCELLED)
        if time.monotonic() >= deadline:
            raise _StopError(HeaderReason.LIMIT)

    def operation(*, final: bool = False) -> None:
        nonlocal operations
        check()
        if operations >= limits.max_operations - int(not final):
            raise _StopError(HeaderReason.LIMIT)
        operations += 1

    def charge_physical(n: int) -> None:
        nonlocal physical
        physical += n
        check()

    def charge_decoded(n: int) -> None:
        nonlocal decoded
        decoded += n
        check()

    def root_matches() -> bool:
        operation(final=True)
        fd = open_root(scan.home)
        try:
            return identity_for_fd(fd) == scan.home_identity
        finally:
            os.close(fd)

    root = -1
    root_valid = False
    try:
        operation()
        root = open_root(scan.home)
        if identity_for_fd(root) != scan.home_identity:
            return HeaderBatch((), False, operations, physical, decoded)
        for index, entry in enumerate(entries):
            current = leaf = data = -1
            locator_verified = False
            try:
                check()
                if (
                    index >= limits.max_files
                    or physical >= limits.max_physical_bytes
                    or decoded >= limits.max_decoded_bytes
                ):
                    raise _StopError(HeaderReason.LIMIT)
                if (identifier := rollout_ids[index]) is not None and rollout_counts[
                    identifier
                ] > 1:
                    raise _StopError(HeaderReason.AMBIGUOUS)
                parts = entry.parts
                if (
                    not parts
                    or known.get(parts) != entry
                    or any(not p or p in (".", "..") or "/" in p for p in parts)
                ):
                    results.append(HeaderResult(HeaderReason.INVALID_LOCATOR))
                    continue
                representation = (
                    PhysicalRepresentation.ZSTD
                    if parts[-1].endswith(".jsonl.zst")
                    else PhysicalRepresentation.PLAIN
                )
                path = scan.home.joinpath(*parts)
                ownership_from_path(path, representation)
                current = os.dup(root)
                for i, name in enumerate(parts[:-1], 1):
                    expected = known.get(parts[:i])
                    if expected is None or not stat.S_ISDIR(expected.stat.st_mode):
                        raise _StopError(HeaderReason.INVALID_LOCATOR)
                    operation()
                    child = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=current,
                    )
                    os.close(current)
                    current = child
                    if identity_for_fd(current) != expected.identity:
                        raise _StopError(HeaderReason.CHANGED)
                operation()
                leaf = os.open(parts[-1], os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
                info = os.fstat(leaf)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _StopError(HeaderReason.UNSUPPORTED)
                if identity_for_fd(leaf, info) != entry.identity or _stamp(info) != _stamp(
                    entry.stat
                ):
                    raise _StopError(HeaderReason.CHANGED)
                operation()
                data = os.open(f"/proc/self/fd/{leaf}", os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
                actual = os.fstat(data)
                if identity_for_fd(data, actual) != entry.identity or _stamp(actual) != _stamp(
                    info
                ):
                    raise _StopError(HeaderReason.CHANGED)
                read_error: HeaderReadError | None = None
                captured = None
                try:
                    captured = read_bounded_header(
                        data,
                        representation,
                        limits=HeaderReadLimits(
                            limits.max_decoded_bytes - decoded,
                            limits.max_physical_bytes - physical,
                            True,
                        ),
                        charge=charge_physical,
                        charge_decoded=charge_decoded,
                    )
                except HeaderReadError as error:
                    read_error = error
                if _stamp(os.fstat(data)) != _stamp(actual):
                    raise _StopError(HeaderReason.CHANGED)
                operation()
                named = os.open(parts[-1], os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
                try:
                    if identity_for_fd(named) != entry.identity or _stamp(
                        os.fstat(named)
                    ) != _stamp(info):
                        raise _StopError(HeaderReason.CHANGED)
                finally:
                    os.close(named)
                # Reopen the complete ancestor chain after reading to detect renamed parents.
                verify = os.dup(root)
                try:
                    for i, name in enumerate(parts[:-1], 1):
                        operation()
                        child = os.open(
                            name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=verify,
                        )
                        os.close(verify)
                        verify = child
                        if identity_for_fd(verify) != known[parts[:i]].identity:
                            raise _StopError(HeaderReason.CHANGED)
                finally:
                    os.close(verify)
                locator_verified = True
                if read_error is not None:
                    raise read_error
                if captured is None:
                    raise HeaderReadError("Header unavailable.")
                envelope = json.loads(captured.line)
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("type") != "session_meta"
                    or not isinstance(envelope.get("payload"), dict)
                ):
                    raise ValueError("invalid envelope")
                header = HeaderObservation(
                    path, envelope["payload"], sha256(captured.line).digest(), _stamp(actual)[:5]
                )
                metadata = _metadata(header, representation)
                if metadata.history_mode is HistoryMode.UNKNOWN:
                    msg = "Unknown history mode."
                    raise ValueError(msg)
                facts = HeaderFacts(
                    metadata.ownership,
                    metadata.history_mode,
                    metadata.history_prefix,
                    metadata.logical_fork,
                    metadata.attachment,
                )
                results.append(HeaderResult(HeaderReason.OBSERVED, facts, locator_verified=True))
            except _StopError as error:
                results.append(HeaderResult(error.reason))
            except OSError, IndexError:
                results.append(HeaderResult(HeaderReason.UNAVAILABLE))
            except HeaderChangedError:
                results.append(HeaderResult(HeaderReason.CHANGED))
            except HeaderLimitError:
                results.append(HeaderResult(HeaderReason.LIMIT, locator_verified=locator_verified))
            except HeaderReadError:
                results.append(
                    HeaderResult(HeaderReason.MALFORMED, locator_verified=locator_verified)
                )
            except ValueError, TypeError, KeyError, RecursionError:
                results.append(
                    HeaderResult(HeaderReason.MALFORMED, locator_verified=locator_verified)
                )
            finally:
                for fd in (data, leaf, current):
                    if fd >= 0:
                        os.close(fd)
        root_valid = root_matches()
    except _StopError, OSError, ValueError, IndexError:
        root_valid = False
    finally:
        if root >= 0:
            os.close(root)
    return HeaderBatch(tuple(results), root_valid, operations, physical, decoded)


def _stamp(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink
