# Copyright (c) 2026

"""Observe installed artifact and storage compatibility without cleanup authority."""

from __future__ import annotations

import math
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING

from codex_session_gc.execution.closed_work_profile import (
    ContractRefused,
    load_closed_work_contract,
)
from codex_session_gc.execution.closed_work_request import (
    DatabaseKind,
    PreparationRefused,
    validate_database_schema,
)
from codex_session_gc.execution.resource_policy import READ_BYTES, TIMEOUT_SECONDS
from codex_session_gc.sqlite_read import DirectReadError, read_transaction

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.execution.closed_work_request import DatabaseContract


class Status(StrEnum):
    """An observation outcome, never a cleanup permission."""

    LOADED = "loaded"
    MATCHED = "matched"
    MISMATCH = "mismatch"
    MISSING_REQUIRED = "missing_required"
    ABSENT_OPTIONAL = "absent_optional"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Observation:
    """One artifact or database observation and its actionable explanation."""

    name: str
    path: str | None
    status: Status
    detail: str
    sha256: str | None = None
    expected_sha256: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Separate observations; no global snapshot or deletion eligibility claim."""

    profile: Observation
    helper: Observation
    databases: tuple[Observation, ...]
    kind: str = "compatibility_observations"
    boundary: str = (
        "Separate read-only observations, not a global snapshot or permission to delete. "
        "Native ownership and fresh execution checks remain required. "
        "SQLite may create or update WAL/SHM files and use ordinary coordination locks; "
        "user tables and histories are not changed or copied."
    )

    @property
    def exit_code(self) -> int:
        """Distinguish known incompatibility from incomplete observations."""
        statuses = {item.status for item in (self.profile, self.helper, *self.databases)}
        if statuses & {Status.MISMATCH, Status.MISSING_REQUIRED}:
            return 1
        return 3 if Status.UNKNOWN in statuses else 0


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        message = "Observation exceeded --timeout-seconds; retry with an appropriate bound."
        raise DirectReadError(message)


def _helper_digest(path: Path, maximum: int, deadline: float) -> bytes:
    _check_deadline(deadline)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111:
            message = "Use the regular executable from the matching release bundle."
            raise ValueError(message)
        if before.st_size > maximum:
            message = "Helper exceeds --max-read-bytes; increase the observation bound."
            raise DirectReadError(message)
        digest = sha256()
        total = 0
        while block := stream.read(min(1024 * 1024, maximum - total + 1)):
            total += len(block)
            if total > maximum or time.monotonic() >= deadline:
                message = "Helper observation exceeded its byte or time bound."
                raise DirectReadError(message)
            digest.update(block)
        if _identity(before) != _identity(os.fstat(stream.fileno())) or _identity(
            before
        ) != _identity(path.stat(follow_symlinks=False)):
            message = "Helper changed during observation; retry with a stable release file."
            raise DirectReadError(message)
    _check_deadline(deadline)
    return digest.digest()


def _helper(path: Path | None, expected: bytes, maximum: int, deadline: float) -> Observation:
    if path is None:
        return Observation(
            "helper",
            None,
            Status.UNKNOWN,
            "Supply --helper to check the qualified executable.",
            expected_sha256=expected.hex(),
        )
    try:
        actual = _helper_digest(path, maximum, deadline)
        matched = actual == expected
        return Observation(
            "helper",
            str(path),
            Status.MATCHED if matched else Status.MISMATCH,
            "Exact qualified helper bytes observed."
            if matched
            else "Digest differs; use the helper from the release matching this installed package.",
            actual.hex(),
            expected.hex(),
        )
    except (OSError, DirectReadError) as error:
        return Observation(
            "helper", str(path), Status.UNKNOWN, str(error), expected_sha256=expected.hex()
        )
    except ValueError as error:
        return Observation(
            "helper", str(path), Status.MISMATCH, str(error), expected_sha256=expected.hex()
        )


def _database(path: Path, contract: DatabaseContract, maximum: int, deadline: float) -> Observation:
    try:
        path.lstat()
    except FileNotFoundError:
        required = contract.kind is DatabaseKind.STATE
        return Observation(
            contract.kind.value,
            str(path),
            Status.MISSING_REQUIRED if required else Status.ABSENT_OPTIONAL,
            "Authoritative state database is absent; check --home."
            if required
            else "Optional database is absent; no storage was created.",
        )
    except OSError as error:
        return Observation(contract.kind.value, str(path), Status.UNKNOWN, str(error))
    try:
        _check_deadline(deadline)
        with read_transaction(path, maximum_bytes=maximum, deadline=deadline) as connection:
            validate_database_schema(connection, contract)
        _check_deadline(deadline)
        return Observation(
            contract.kind.value,
            str(path),
            Status.MATCHED,
            "Exact schema and migration identities observed.",
        )
    except PreparationRefused as error:
        return Observation(
            contract.kind.value,
            str(path),
            Status.MISMATCH,
            f"{error} See the compatibility procedure before cleanup.",
        )
    except (OSError, sqlite3.Error, DirectReadError, ValueError, TypeError) as error:
        return Observation(
            contract.kind.value, str(path), Status.UNKNOWN, f"Observation unavailable: {error}"
        )


def inspect_compatibility(
    home: Path,
    helper: Path | None,
    *,
    maximum_read_bytes: int = READ_BYTES,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> DoctorReport:
    """Observe only profile/helper identities and the declared database schemas.

    SQLite may create or update WAL/SHM coordination files and take its normal
    read locks. User tables and histories are not changed or copied.
    Byte bounds apply to each observed file. The deadline covers the observation
    sequence. No helper execution, history reads, writer locks or receipts occur.
    """
    if not home.is_absolute() or (helper is not None and not helper.is_absolute()):
        message = "Doctor requires absolute home and helper paths."
        raise ValueError(message)
    if maximum_read_bytes < 1 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        message = "Observation byte and time bounds must be positive and finite."
        raise ValueError(message)
    deadline = time.monotonic() + timeout_seconds
    try:
        contract = load_closed_work_contract()
    except ContractRefused as error:
        return DoctorReport(
            Observation("profile", None, Status.UNKNOWN, str(error)),
            Observation(
                "helper",
                str(helper) if helper else None,
                Status.UNKNOWN,
                "A valid installed profile is required before comparing helper bytes.",
            ),
            (),
        )
    return DoctorReport(
        Observation(
            "profile",
            None,
            Status.LOADED,
            contract.source_identity,
            sha256(contract.evidence.data).hexdigest(),
        ),
        _helper(helper, contract.binary_sha256, maximum_read_bytes, deadline),
        tuple(
            _database(home / item.kind.value, item, maximum_read_bytes, deadline)
            for item in contract.databases
        ),
    )


def terminal(report: DoctorReport) -> str:
    """Render actionable observations without treating a match as authority."""
    lines = [report.boundary]
    for item in (report.profile, report.helper, *report.databases):
        lines.append(f"{item.name}: {item.status} — {item.detail}")
        if item.path is not None:
            lines.append(f"  Path: {item.path}")
        if item.sha256 is not None:
            lines.append(f"  SHA-256: {item.sha256}")
        if item.expected_sha256 is not None:
            lines.append(f"  Expected SHA-256: {item.expected_sha256}")
    return "\n".join(lines)
