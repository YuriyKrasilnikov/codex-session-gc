# Copyright (c) 2026

"""Observe installed artifact and storage compatibility without cleanup authority."""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING

from codex_session_gc.compatibility.observation import (
    BundledSupport,
    SourceSupportObservation,
    SupportSelection,
    observe_bundled_support,
    observe_record,
    observe_unavailable_record,
)
from codex_session_gc.compatibility.store import (
    SupportStoreError,
    read_selected_support,
    recorded_origin,
)
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
from codex_session_gc.installation.activation import ActivationError
from codex_session_gc.installation.helper_observation import (
    HelperObservation,
    InvalidHelperError,
    observe_helper,
)
from codex_session_gc.installation.read_budget import ReadBudget
from codex_session_gc.installation.transaction import InstallationError
from codex_session_gc.sqlite_read import DirectReadError, read_transaction

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.compatibility.record import ProducerObservation
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
    source_support: SourceSupportObservation
    recorded_verification: dict[str, object] | None = None
    selected_support: dict[str, str] | None = None
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


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        message = "Observation exceeded --timeout-seconds; retry with an appropriate bound."
        raise DirectReadError(message)


def _helper(
    path: Path | None, expected: bytes, budget: ReadBudget, resources: ExitStack
) -> tuple[Observation, HelperObservation | None]:
    if path is None:
        return Observation(
            "helper",
            None,
            Status.UNKNOWN,
            "Supply --helper to check the qualified executable.",
            expected_sha256=expected.hex(),
        ), None
    try:
        held = resources.enter_context(observe_helper(path, budget))
        actual = held.digest
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
        ), held
    except InvalidHelperError as error:
        return Observation(
            "helper", str(path), Status.MISMATCH, str(error), expected_sha256=expected.hex()
        ), None
    except (OSError, ValueError) as error:
        return Observation(
            "helper", str(path), Status.UNKNOWN, str(error), expected_sha256=expected.hex()
        ), None


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


def _support_observation(
    selection: SupportSelection,
    profile_raw: bytes | None,
    producer: ProducerObservation | None,
    budget: ReadBudget,
    helper_observation: HelperObservation | None = None,
) -> tuple[SourceSupportObservation, dict[str, object] | None]:
    helper_sha256 = None if helper_observation is None else helper_observation.digest.hex()
    if isinstance(selection, BundledSupport):
        return (
            observe_bundled_support(
                profile_raw=profile_raw, helper_sha256=helper_sha256, producer=producer
            ),
            None,
        )
    try:
        stored = read_selected_support(selection, budget, helper_observation)
        budget.check()
        observation = observe_record(
            stored.raw, profile_raw=profile_raw, helper_sha256=helper_sha256, producer=producer
        )
        budget.check()
        return observation, recorded_origin(stored.receipt)
    except OSError, ValueError, SupportStoreError, ActivationError, InstallationError:
        return observe_unavailable_record(
            selection, profile_raw=profile_raw, producer=producer
        ), None


def _selected_support(selection: SupportSelection) -> dict[str, str] | None:
    if isinstance(selection, BundledSupport):
        return None
    return {
        "installation_id": str(selection.installation_id),
        "record_sha256": selection.record_sha256,
        "context_sha256": selection.context_sha256,
    }


@dataclass(frozen=True, slots=True)
class DiagnosticOptions:
    """Local observation limits and optional caller-reported producer identity."""

    maximum_read_bytes: int = READ_BYTES
    timeout_seconds: float = TIMEOUT_SECONDS
    producer: ProducerObservation | None = None


def inspect_compatibility(
    home: Path,
    helper: Path | None,
    *,
    maximum_read_bytes: int = READ_BYTES,
    timeout_seconds: float = TIMEOUT_SECONDS,
    producer: ProducerObservation | None = None,
) -> DoctorReport:
    """Observe only profile/helper identities and the declared database schemas.

    SQLite may create or update WAL/SHM coordination files and take its normal
    read locks. User tables and histories are not changed or copied.
    Byte bounds apply to each observed file. The deadline covers the observation
    sequence. No helper execution, history reads, writer locks or receipts occur.
    """
    return inspect_selected_compatibility(
        home,
        helper,
        BundledSupport(),
        options=DiagnosticOptions(maximum_read_bytes, timeout_seconds, producer),
    )


def inspect_selected_compatibility(
    home: Path,
    helper: Path | None,
    selection: SupportSelection,
    *,
    options: DiagnosticOptions | None = None,
) -> DoctorReport:
    """Observe one support selection under the shared cooperative read limits.

    An imported selection also reads its installation receipt and selected
    release files. It does not read or create cleanup operation receipts.
    """
    options = options or DiagnosticOptions()
    maximum_read_bytes = options.maximum_read_bytes
    timeout_seconds = options.timeout_seconds
    producer = options.producer
    if not home.is_absolute() or (helper is not None and not helper.is_absolute()):
        message = "Doctor requires absolute home and helper paths."
        raise ValueError(message)
    if maximum_read_bytes < 1 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        message = "Observation byte and time bounds must be positive and finite."
        raise ValueError(message)
    deadline = time.monotonic() + timeout_seconds
    budget = ReadBudget(maximum_read_bytes, deadline)
    try:
        contract = load_closed_work_contract()
    except ContractRefused as error:
        support, recorded = _support_observation(selection, None, producer, budget)
        return DoctorReport(
            Observation("profile", None, Status.UNKNOWN, str(error)),
            Observation(
                "helper",
                str(helper) if helper else None,
                Status.UNKNOWN,
                "A valid installed profile is required before comparing helper bytes.",
            ),
            (),
            support,
            recorded,
            _selected_support(selection),
        )
    profile_observation = Observation(
        "profile",
        None,
        Status.LOADED,
        contract.source_identity,
        sha256(contract.evidence.data).hexdigest(),
    )
    with ExitStack() as resources:
        helper_observation, held = _helper(helper, contract.binary_sha256, budget, resources)
        database_observations = tuple(
            _database(home / item.kind.value, item, maximum_read_bytes, deadline)
            for item in contract.databases
        )
        support, recorded = _support_observation(
            selection, contract.evidence.data, producer, budget, held
        )
    return DoctorReport(
        profile_observation,
        helper_observation,
        database_observations,
        support,
        recorded,
        _selected_support(selection),
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
    support = report.source_support
    lines.append(f"source support: {support.availability} — {support.detail}")
    lines.append(f"  Origin: {support.origin}")
    if report.recorded_verification is not None:
        lines.append("  Verification origin: verification_recorded_not_reauthenticated")
    if support.record is not None:
        record = support.record
        lines.append(f"  Claimed status: {record.status}")
        lines.append(f"  Scope: {record.source_origin.scope}")
        lines.append(
            f"  Target source: {record.target.repository} "
            f"{record.target.source.ref} {record.target.source.commit}"
        )
        lines.append(f"  Source report SHA-256: {record.report_sha256}")
        lines.extend(f"  Condition: {condition.description}" for condition in record.conditions)
    if support.bindings is not None:
        lines.append(f"  Identity binding: {support.bindings.state}")
        lines.extend(f"    {check.field}: {check.state}" for check in support.bindings.checks)
    if support.profile_declared_native_inputs_sha256 is not None:
        lines.append(
            "  Profile-declared native inputs SHA-256 "
            "(not an independent source observation): "
            f"{support.profile_declared_native_inputs_sha256}"
        )
    return "\n".join(lines)
