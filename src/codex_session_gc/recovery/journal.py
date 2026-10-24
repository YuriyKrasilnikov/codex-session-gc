# Copyright (c) 2026
# ruff: noqa: C901, E501, EM101, PLR0912, PLR0913, PLR0915, PLR0917, TC003, TRY003

"""Append-only recovery evidence for declared requests and owned launches.

Execution adapters record intent before dispatch and supply delivery, lifecycle
and effect observations. This module stores and validates those relations;
the adapters own native processes.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Final
from urllib.parse import quote
from uuid import UUID

APPLICATION_ID: Final = 0x43534752
SCHEMA_VERSION: Final = 2
APPLICATION_NAME: Final = "codex-session-gc.recovery"
DIGEST_BYTES: Final = 32


class RecoveryError(Exception):
    """Base class for recovery receipt failures."""


class ReceiptMissingError(RecoveryError):
    """Raised when inspection is requested for a receipt that does not exist."""


class ReceiptIdentityError(RecoveryError):
    """Raised when a path is not a supported CLI-owned receipt."""


class ReceiptRecoveryRequiredError(RecoveryError):
    """Raised when a read-only open detects SQLite recovery work."""


class TransitionError(RecoveryError):
    """Raised for an invalid or unsafe state transition."""


@dataclass(frozen=True, slots=True)
class AttemptId:
    """A stable identity for one recovery attempt."""

    value: UUID

    def __post_init__(self) -> None:
        """Require a real UUID rather than UUID-shaped text."""
        if type(self.value) is not UUID:
            msg = "An attempt identifier requires a UUID."
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class UnitId:
    """A stable identity for one ordered unit of an attempt."""

    value: UUID

    def __post_init__(self) -> None:
        """Require a real UUID rather than UUID-shaped text."""
        if type(self.value) is not UUID:
            msg = "A unit identifier requires a UUID."
            raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class LaunchId:
    """A cryptographically random identity for one blocked native launch."""

    value: UUID

    def __post_init__(self) -> None:
        """Require a real UUID rather than UUID-shaped text."""
        if type(self.value) is not UUID:
            raise TypeError("A launch identifier requires a UUID.")


@dataclass(frozen=True, slots=True)
class Payload:
    """Exact supplied bytes with a caller-declared type and format version."""

    content_type: str
    version: int
    data: bytes

    def __post_init__(self) -> None:
        """Validate immutable payload metadata and bytes."""
        if not self.content_type or type(self.content_type) is not str:
            msg = "A payload requires a nonempty content type."
            raise TypeError(msg)
        if type(self.version) is not int or self.version < 1:
            msg = "A payload version must be a positive integer."
            raise ValueError(msg)
        if type(self.data) is not bytes:
            msg = "A payload requires immutable bytes."
            raise TypeError(msg)

    @property
    def digest(self) -> bytes:
        """Return the SHA-256 digest of the exact supplied bytes."""
        return sha256(self.data).digest()


@dataclass(frozen=True, slots=True)
class StartEnvelope:
    """Supplied inputs recorded before a future startup call."""

    attempt_id: AttemptId
    profile: Payload
    evidence: Payload
    plan: Payload
    process: Payload


@dataclass(frozen=True, slots=True)
class Unit:
    """One sequential unit declared when its attempt starts."""

    unit_id: UnitId
    sequence: int
    evidence: Payload

    def __post_init__(self) -> None:
        """Require a nonnegative order and typed unit identity."""
        if type(self.unit_id) is not UnitId:
            msg = "A unit requires a unit identifier."
            raise TypeError(msg)
        if type(self.sequence) is not int or self.sequence < 0:
            msg = "A unit sequence must be a nonnegative integer."
            raise ValueError(msg)


class TerminalKind(StrEnum):
    """Labels supplied by an observer; they are not claims of native truth."""

    CONFIRMED = "confirmed"
    REFUSED = "refused"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class LaunchPurpose(StrEnum):
    """Why a coordinator created one native-capable launch."""

    APPLY = "apply"
    RECONCILE = "reconcile"


class LaunchStatus(StrEnum):
    """The durable state of a launch ledger member."""

    PENDING = "pending"
    REGISTERED = "registered"
    CLOSED = "closed"
    OBSERVED_ABSENT = "observed_absent"
    FENCED = "fenced"


@dataclass(frozen=True, slots=True)
class LaunchIntent:
    """Durable intent written before a launcher process is spawned."""

    launch_id: LaunchId
    generation: int
    purpose: LaunchPurpose
    context: Payload

    def __post_init__(self) -> None:
        """Require a typed identity, purpose, context, and positive generation."""
        if type(self.launch_id) is not LaunchId or type(self.generation) is not int:
            raise TypeError("A launch intent requires a launch identity and integer generation.")
        if self.generation < 1 or type(self.purpose) is not LaunchPurpose:
            raise ValueError("A launch intent has an invalid generation or purpose.")


@dataclass(frozen=True, slots=True)
class LaunchRegistration:
    """Exact launcher incarnation persisted before the parent grants exec."""

    launch_id: LaunchId
    generation: int
    incarnation: Payload

    def __post_init__(self) -> None:
        """Require a typed identity, incarnation, and positive generation."""
        if type(self.launch_id) is not LaunchId or type(self.generation) is not int:
            raise TypeError("A launch registration requires an identity and integer generation.")
        if self.generation < 1:
            raise ValueError("A launch registration generation must be positive.")


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """Read-only reconstruction of one append-only launch relation."""

    intent: LaunchIntent
    registration: LaunchRegistration | None
    status: LaunchStatus
    terminal: Payload | None
    terminal_kind: TerminalKind | None


@dataclass(frozen=True, slots=True)
class UnitStatus:
    """Reconstructed state of an ordered unit."""

    unit: Unit
    request: Payload | None
    dispatch_evidence: Payload | None
    process: Payload | None
    terminal: Payload | None
    terminal_kind: TerminalKind | None
    resolution: Payload | None
    resolution_kind: TerminalKind | None

    @property
    def effective_terminal_kind(self) -> TerminalKind | None:
        """Return the definitive reconciliation outcome when one exists."""
        if self.resolution_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}:
            return self.resolution_kind
        return self.terminal_kind

    @property
    def unresolved(self) -> bool:
        """Whether reconciliation is needed before later units may be intended."""
        return (
            self.request is not None and self.effective_terminal_kind is None
        ) or self.effective_terminal_kind in {
            TerminalKind.PARTIAL,
            TerminalKind.UNKNOWN,
        }


@dataclass(frozen=True, slots=True)
class AttemptStatus:
    """A read-only reconstruction of one recovery attempt."""

    start: StartEnvelope
    units: tuple[UnitStatus, ...]
    shutdown: Payload | None
    launches: tuple[LaunchRecord, ...] = ()
    lifecycle: Payload | None = None
    lifecycle_kind: TerminalKind | None = None
    lifecycle_generation: int | None = None

    @property
    def startup_unresolved(self) -> bool:
        """Whether no clean owned-process shutdown was recorded."""
        return self.shutdown is None

    @property
    def unresolved_unit(self) -> UnitStatus | None:
        """Return the first unit that prevents safe later dispatch."""
        return next((unit for unit in self.units if unit.unresolved), None)

    @property
    def launch_generation(self) -> int:
        """Return the generation of the current all-launch ledger."""
        return len(self.launches)

    @property
    def lifecycle_unresolved(self) -> bool:
        """Whether independent lifecycle evidence still requires reconciliation."""
        if not self.launches:
            return False
        return self.lifecycle_kind not in {TerminalKind.CONFIRMED, TerminalKind.REFUSED} or (
            self.lifecycle_generation != self.launch_generation
        )


def create(path: Path, start: StartEnvelope, units: tuple[Unit, ...]) -> None:
    """Exclusively create a receipt and atomically record its start envelope."""
    _validate_units(units)
    path = _absolute(path)
    if not path.parent.is_dir():
        msg = "Recovery receipt parent directory must already exist."
        raise ReceiptIdentityError(msg)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError as exc:
        msg = f"Recovery receipt already exists: {path}"
        raise ReceiptIdentityError(msg) from exc
    os.close(descriptor)
    connection = _writable(path)
    try:
        _initialize(connection)
        _record_start(connection, start, units)
    finally:
        connection.close()
    _sync_directory(path.parent)


def inspect(path: Path) -> tuple[AttemptStatus, ...]:
    """Read receipt state without creating, recovering, or repairing anything."""
    path = _existing_regular_file(path)
    if _has_recovery_sidecar(path):
        msg = "Recovery receipt requires writable recovery."
        raise ReceiptRecoveryRequiredError(msg)
    connection = _readonly(path)
    try:
        _validate_identity(connection)
        return _read_attempts(connection)
    except RecoveryError:
        raise
    except sqlite3.OperationalError as exc:
        if "readonly" in str(exc).lower() or "locked" in str(exc).lower():
            raise ReceiptRecoveryRequiredError(
                "Recovery receipt requires writable recovery."
            ) from exc
        raise ReceiptIdentityError("Recovery receipt is corrupt or unreadable.") from exc
    except (TypeError, ValueError, sqlite3.Error) as exc:
        raise ReceiptIdentityError("Recovery receipt is malformed.") from exc
    finally:
        connection.close()


def recover(path: Path) -> None:
    """Allow SQLite recovery only after immutable validation of this receipt format."""
    path = _existing_regular_file(path)
    identity = _validate_owned_readonly(path)
    connection = _writable(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _revalidate_open_file(path, identity)
        schema_version = _validate_identity(connection)
        connection.execute(f"PRAGMA user_version = {schema_version}")
        connection.commit()
    except (TypeError, ValueError, sqlite3.Error) as exc:
        connection.rollback()
        raise ReceiptIdentityError("Recovery receipt cannot be recovered safely.") from exc
    finally:
        connection.close()


def record_dispatch_intent(
    path: Path,
    attempt_id: AttemptId,
    unit_id: UnitId,
    request: Payload,
    evidence: Payload,
    process: Payload,
) -> None:
    """Atomically record request intent before the adapter delivers the request."""
    with _owned_writable(path) as connection:
        status = _one_attempt(connection, attempt_id)
        _require_dispatchable(status, unit_id)
        connection.execute(
            """INSERT INTO events
               (attempt_id, unit_id, kind, payload_type, payload_version, payload, digest,
                evidence_type, evidence_version, evidence, evidence_digest,
                process_type, process_version, process, process_digest, terminal_kind)
               VALUES (?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                _uuid_bytes(attempt_id.value),
                _uuid_bytes(unit_id.value),
                request.content_type,
                request.version,
                request.data,
                request.digest,
                evidence.content_type,
                evidence.version,
                evidence.data,
                evidence.digest,
                process.content_type,
                process.version,
                process.data,
                process.digest,
            ),
        )


def record_terminal_evidence(
    path: Path,
    attempt_id: AttemptId,
    unit_id: UnitId,
    request_digest: bytes,
    kind: TerminalKind,
    evidence: Payload,
) -> None:
    """Atomically append supplied terminal evidence for an already intended unit."""
    if type(request_digest) is not bytes or len(request_digest) != DIGEST_BYTES:
        msg = "Terminal evidence requires a SHA-256 request digest."
        raise TypeError(msg)
    if type(kind) is not TerminalKind:
        msg = "Terminal evidence requires a terminal kind."
        raise TypeError(msg)
    with _owned_writable(path) as connection:
        status = _one_attempt(connection, attempt_id)
        unit = _find_unit(status, unit_id)
        if status.shutdown is not None:
            msg = "Terminal evidence cannot follow a clean shutdown observation."
            raise TransitionError(msg)
        if (
            unit.request is None
            or unit.terminal_kind is not None
            or unit.request.digest != request_digest
        ):
            msg = "Terminal evidence does not match one pending dispatch intent."
            raise TransitionError(msg)
        connection.execute(
            """INSERT INTO events
               (attempt_id, unit_id, kind, payload_type, payload_version, payload, digest,
                evidence_type, evidence_version, evidence, evidence_digest,
                process_type, process_version, process, process_digest, terminal_kind)
               VALUES (?, ?, 'terminal', ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL, ?)""",
            (
                _uuid_bytes(attempt_id.value),
                _uuid_bytes(unit_id.value),
                evidence.content_type,
                evidence.version,
                evidence.data,
                evidence.digest,
                kind.value,
            ),
        )


def record_clean_shutdown(path: Path, attempt_id: AttemptId, evidence: Payload) -> None:
    """Append a clean owned-process shutdown observation without resolving units."""
    with _owned_writable(path) as connection:
        status = _one_attempt(connection, attempt_id)
        if status.shutdown is not None:
            msg = "A clean shutdown is already recorded for this attempt."
            raise TransitionError(msg)
        connection.execute(
            """INSERT INTO events
               (attempt_id, unit_id, kind, payload_type, payload_version, payload, digest,
                evidence_type, evidence_version, evidence, evidence_digest,
                process_type, process_version, process, process_digest, terminal_kind)
               VALUES (?, NULL, 'shutdown', ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL, NULL)""",
            (
                _uuid_bytes(attempt_id.value),
                evidence.content_type,
                evidence.version,
                evidence.data,
                evidence.digest,
            ),
        )


def record_reconciliation_evidence(
    path: Path,
    attempt_id: AttemptId,
    unit_id: UnitId,
    request_digest: bytes,
    kind: TerminalKind,
    evidence: Payload,
) -> None:
    """Append one digest-bound reconciliation observation without replacing delivery evidence."""
    if type(request_digest) is not bytes or len(request_digest) != DIGEST_BYTES:
        msg = "Reconciliation evidence requires a SHA-256 request digest."
        raise TypeError(msg)
    if type(kind) is not TerminalKind:
        msg = "Reconciliation evidence requires a terminal kind."
        raise TypeError(msg)
    with _owned_writable(path) as connection:
        status = _one_attempt(connection, attempt_id)
        unit = _find_unit(status, unit_id)
        if unit.request is None or unit.request.digest != request_digest or not unit.unresolved:
            msg = "Reconciliation evidence does not match one unresolved dispatch intent."
            raise TransitionError(msg)
        if unit.resolution_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}:
            msg = "A definitive reconciliation is already recorded for this unit."
            raise TransitionError(msg)
        connection.execute(
            """INSERT INTO resolutions
               (attempt_id, unit_id, request_digest, terminal_kind, evidence_type,
                evidence_version, evidence, evidence_digest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _uuid_bytes(attempt_id.value),
                _uuid_bytes(unit_id.value),
                request_digest,
                kind.value,
                evidence.content_type,
                evidence.version,
                evidence.data,
                evidence.digest,
            ),
        )


def record_launch_intent(path: Path, attempt_id: AttemptId, intent: LaunchIntent) -> None:
    """Append intent before spawning a blocked launcher; it never starts a process."""
    with _owned_writable(path) as connection:
        _validate_identity(connection)
        status = _one_attempt(connection, attempt_id)
        if status.lifecycle_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED} and not (
            intent.purpose is LaunchPurpose.RECONCILE and status.unresolved_unit is not None
        ):
            raise TransitionError(
                "Definitive lifecycle resolution closes an attempt to new launch intent."
            )
        if status.lifecycle is not None and (
            intent.purpose is not LaunchPurpose.RECONCILE
            or (status.unresolved_unit is None and not status.startup_unresolved)
        ):
            raise TransitionError(
                "Only explicit reconciliation may extend uncertain lifecycle evidence."
            )
        if intent.generation != status.launch_generation + 1:
            raise TransitionError("Launch intent generation does not extend the current ledger.")
        if any(launch.intent.launch_id == intent.launch_id for launch in status.launches):
            raise TransitionError("A launch identity may appear only once in one attempt.")
        if _has_active_launch_predecessor(status, intent.generation):
            raise TransitionError("An active earlier launch must be fenced or accounted for first.")
        connection.execute(
            "INSERT INTO launch_events (attempt_id, launch_id, generation, kind, purpose, "
            "context_type, context_version, context, context_digest) VALUES (?, ?, ?, 'intent', ?, ?, ?, ?, ?)",
            (
                _uuid_bytes(attempt_id.value),
                _uuid_bytes(intent.launch_id.value),
                intent.generation,
                intent.purpose.value,
                intent.context.content_type,
                intent.context.version,
                intent.context.data,
                intent.context.digest,
            ),
        )


def register_launch(path: Path, attempt_id: AttemptId, registration: LaunchRegistration) -> None:
    """Persist an exact incarnation only for its still-pending matching intent."""
    with _owned_writable(path) as connection:
        _validate_identity(connection)
        status = _one_attempt(connection, attempt_id)
        launch = _find_launch(status, registration.launch_id, registration.generation)
        if launch.status is not LaunchStatus.PENDING:
            raise TransitionError("Only one unfenced pending launch intent may register.")
        if _has_active_launch_predecessor(status, registration.generation):
            raise TransitionError("An active earlier launch must be fenced or accounted for first.")
        connection.execute(
            "INSERT INTO launch_events (attempt_id, launch_id, generation, kind, incarnation_type, "
            "incarnation_version, incarnation, incarnation_digest) VALUES (?, ?, ?, 'registration', ?, ?, ?, ?)",
            (
                _uuid_bytes(attempt_id.value),
                _uuid_bytes(registration.launch_id.value),
                registration.generation,
                registration.incarnation.content_type,
                registration.incarnation.version,
                registration.incarnation.data,
                registration.incarnation.digest,
            ),
        )


def fence_launch(
    path: Path, attempt_id: AttemptId, launch_id: LaunchId, generation: int, evidence: Payload
) -> None:
    """Fence a pending launcher transactionally so it can never register later."""
    with _owned_writable(path) as connection:
        _validate_identity(connection)
        status = _one_attempt(connection, attempt_id)
        launch = _find_launch(status, launch_id, generation)
        if launch.status is not LaunchStatus.PENDING:
            raise TransitionError("Only a pending launch intent may be fenced.")
        _append_launch_terminal(
            connection, attempt_id, launch_id, generation, "fence", evidence, None
        )


def fence_pending_launches(path: Path, attempt_id: AttemptId, evidence: Payload) -> None:
    """Atomically fence every still-pending launch before a replacement is considered."""
    with _owned_writable(path) as connection:
        _validate_identity(connection)
        status = _one_attempt(connection, attempt_id)
        for launch in status.launches:
            if launch.status is LaunchStatus.PENDING:
                _append_launch_terminal(
                    connection,
                    attempt_id,
                    launch.intent.launch_id,
                    launch.intent.generation,
                    "fence",
                    evidence,
                    None,
                )


def record_launch_closure(
    path: Path,
    attempt_id: AttemptId,
    launch_id: LaunchId,
    generation: int,
    kind: TerminalKind,
    evidence: Payload,
) -> None:
    """Append typed launcher cleanup evidence for a registered launch."""
    if kind not in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}:
        raise ValueError("Launch closure requires confirmed or refused observer evidence.")
    with _owned_writable(path) as connection:
        _validate_identity(connection)
        status = _one_attempt(connection, attempt_id)
        launch = _find_launch(status, launch_id, generation)
        if launch.status is not LaunchStatus.REGISTERED:
            raise TransitionError("Only a registered launch may be closed.")
        _append_launch_terminal(
            connection, attempt_id, launch_id, generation, "closure", evidence, kind
        )


def record_launch_absence(
    path: Path,
    attempt_id: AttemptId,
    launch_id: LaunchId,
    generation: int,
    evidence: Payload,
) -> None:
    """Append supplied observer evidence; the journal itself proves no OS absence."""
    with _owned_writable(path) as connection:
        _validate_identity(connection)
        status = _one_attempt(connection, attempt_id)
        launch = _find_launch(status, launch_id, generation)
        if launch.status is not LaunchStatus.REGISTERED:
            raise TransitionError("Only a registered launch may receive absence evidence.")
        _append_launch_terminal(
            connection, attempt_id, launch_id, generation, "absence", evidence, None
        )


def record_lifecycle_resolution(
    path: Path,
    attempt_id: AttemptId,
    generation: int,
    kind: TerminalKind,
    evidence: Payload,
) -> None:
    """Append independent lifecycle evidence bound to the exact complete launch ledger."""
    with _owned_writable(path) as connection:
        _validate_identity(connection)
        status = _one_attempt(connection, attempt_id)
        if generation != status.launch_generation or generation < 1:
            raise TransitionError(
                "Lifecycle resolution must name the exact current launch generation."
            )
        if any(
            launch.status
            not in {LaunchStatus.CLOSED, LaunchStatus.OBSERVED_ABSENT, LaunchStatus.FENCED}
            for launch in status.launches
        ):
            raise TransitionError(
                "Every current launch must be fenced, closed, or observed absent."
            )
        if (
            status.lifecycle_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}
            and status.lifecycle_generation == generation
        ):
            raise TransitionError("A definitive lifecycle resolution is already recorded.")
        connection.execute(
            "INSERT INTO lifecycle_resolutions (attempt_id, generation, terminal_kind, evidence_type, "
            "evidence_version, evidence, evidence_digest) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _uuid_bytes(attempt_id.value),
                generation,
                kind.value,
                evidence.content_type,
                evidence.version,
                evidence.data,
                evidence.digest,
            ),
        )


def _absolute(path: Path) -> Path:
    return path.absolute()


def _existing_regular_file(path: Path) -> Path:
    path = _absolute(path)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        msg = f"Recovery receipt does not exist: {path}"
        raise ReceiptMissingError(msg) from exc
    if not path.is_file() or path.is_symlink() or not stat.S_ISREG(mode):
        msg = "Recovery receipt must be an existing regular non-symlink file."
        raise ReceiptIdentityError(msg)
    return path


def _uri(path: Path, suffix: str) -> str:
    return f"file:{quote(str(path))}?{suffix}"


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_uri(path, "mode=ro"), uri=True, isolation_level=None)


def _immutable_read(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_uri(path, "mode=ro&immutable=1"), uri=True, isolation_level=None)


def _writable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


@contextmanager
def _owned_writable(path: Path) -> Generator[sqlite3.Connection]:
    """Yield an exclusively locked, identity-revalidated owned receipt."""
    path = _existing_regular_file(path)
    identity = _validate_owned_readonly(path)
    connection = _writable(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _revalidate_open_file(path, identity)
        _validate_identity(connection)
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int


def _validate_owned_readonly(path: Path) -> _FileIdentity:
    """Validate ownership before SQLite receives writable access to the path."""
    identity = _file_identity(path)
    connection = _immutable_read(path)
    try:
        _validate_identity(connection)
        _read_attempts(connection)
    except (TypeError, ValueError, sqlite3.Error) as exc:
        raise ReceiptIdentityError("Recovery receipt identity cannot be established.") from exc
    finally:
        connection.close()
    return identity


def _file_identity(path: Path) -> _FileIdentity:
    details = path.stat()
    return _FileIdentity(details.st_dev, details.st_ino)


def _revalidate_open_file(path: Path, expected: _FileIdentity) -> None:
    if _file_identity(path) != expected:
        msg = "Recovery receipt changed while acquiring its transaction."
        raise ReceiptIdentityError(msg)


def _initialize(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.executescript(
        """CREATE TABLE identity (name TEXT NOT NULL PRIMARY KEY, schema_version INTEGER NOT NULL);
        CREATE TABLE attempts (
            attempt_id BLOB NOT NULL PRIMARY KEY,
            profile_type TEXT NOT NULL, profile_version INTEGER NOT NULL, profile BLOB NOT NULL,
            profile_digest BLOB NOT NULL, evidence_type TEXT NOT NULL, evidence_version INTEGER NOT NULL,
            evidence BLOB NOT NULL, evidence_digest BLOB NOT NULL, plan_type TEXT NOT NULL,
            plan_version INTEGER NOT NULL, plan BLOB NOT NULL, plan_digest BLOB NOT NULL,
            process_type TEXT NOT NULL, process_version INTEGER NOT NULL, process BLOB NOT NULL,
            process_digest BLOB NOT NULL
        );
        CREATE TABLE units (
            attempt_id BLOB NOT NULL REFERENCES attempts(attempt_id), unit_id BLOB NOT NULL,
            sequence INTEGER NOT NULL, evidence_type TEXT NOT NULL, evidence_version INTEGER NOT NULL,
            evidence BLOB NOT NULL, evidence_digest BLOB NOT NULL,
            PRIMARY KEY (attempt_id, unit_id), UNIQUE (attempt_id, sequence)
        );
        CREATE TABLE events (
            ordinal INTEGER PRIMARY KEY, attempt_id BLOB NOT NULL REFERENCES attempts(attempt_id),
            unit_id BLOB, kind TEXT NOT NULL, payload_type TEXT, payload_version INTEGER, payload BLOB,
            digest BLOB, evidence_type TEXT, evidence_version INTEGER, evidence BLOB, evidence_digest BLOB,
            process_type TEXT, process_version INTEGER, process BLOB, process_digest BLOB, terminal_kind TEXT
        );
        CREATE TABLE resolutions (
            ordinal INTEGER PRIMARY KEY, attempt_id BLOB NOT NULL REFERENCES attempts(attempt_id),
            unit_id BLOB NOT NULL, request_digest BLOB NOT NULL, terminal_kind TEXT NOT NULL,
            evidence_type TEXT NOT NULL, evidence_version INTEGER NOT NULL, evidence BLOB NOT NULL,
            evidence_digest BLOB NOT NULL
        );
        CREATE TABLE launch_events (
            ordinal INTEGER PRIMARY KEY, attempt_id BLOB NOT NULL REFERENCES attempts(attempt_id),
            launch_id BLOB NOT NULL, generation INTEGER NOT NULL, kind TEXT NOT NULL,
            purpose TEXT, context_type TEXT, context_version INTEGER, context BLOB, context_digest BLOB,
            incarnation_type TEXT, incarnation_version INTEGER, incarnation BLOB, incarnation_digest BLOB,
            terminal_kind TEXT, evidence_type TEXT, evidence_version INTEGER, evidence BLOB,
            evidence_digest BLOB
        );
        CREATE TABLE lifecycle_resolutions (
            ordinal INTEGER PRIMARY KEY, attempt_id BLOB NOT NULL REFERENCES attempts(attempt_id),
            generation INTEGER NOT NULL, terminal_kind TEXT NOT NULL, evidence_type TEXT NOT NULL,
            evidence_version INTEGER NOT NULL, evidence BLOB NOT NULL, evidence_digest BLOB NOT NULL
        );
        INSERT INTO identity VALUES ('codex-session-gc.recovery', 2);"""
    )


def _record_start(
    connection: sqlite3.Connection, start: StartEnvelope, units: tuple[Unit, ...]
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _uuid_bytes(start.attempt_id.value),
                start.profile.content_type,
                start.profile.version,
                start.profile.data,
                start.profile.digest,
                start.evidence.content_type,
                start.evidence.version,
                start.evidence.data,
                start.evidence.digest,
                start.plan.content_type,
                start.plan.version,
                start.plan.data,
                start.plan.digest,
                start.process.content_type,
                start.process.version,
                start.process.data,
                start.process.digest,
            ),
        )
        connection.executemany(
            "INSERT INTO units VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    _uuid_bytes(start.attempt_id.value),
                    _uuid_bytes(unit.unit_id.value),
                    unit.sequence,
                    unit.evidence.content_type,
                    unit.evidence.version,
                    unit.evidence.data,
                    unit.evidence.digest,
                )
                for unit in units
            ],
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def _validate_identity(connection: sqlite3.Connection) -> int:
    application = connection.execute("PRAGMA application_id").fetchone()
    version = connection.execute("PRAGMA user_version").fetchone()
    if application != (APPLICATION_ID,) or version != (SCHEMA_VERSION,):
        raise ReceiptIdentityError(
            "Recovery receipt has an unsupported application or schema identity."
        )
    try:
        identity = connection.execute("SELECT name, schema_version FROM identity").fetchall()
    except sqlite3.Error as exc:
        raise ReceiptIdentityError("Recovery receipt schema is unsupported.") from exc
    schema_version = _as_int(version[0])
    if identity != [(APPLICATION_NAME, schema_version)]:
        raise ReceiptIdentityError("Recovery receipt identity table is unsupported.")
    table_names = {
        _as_str(table[0])
        for table in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    expected_tables = {"identity", "attempts", "units", "events"}
    expected_tables |= {"resolutions", "launch_events", "lifecycle_resolutions"}
    if table_names != expected_tables:
        raise ReceiptIdentityError("Recovery receipt schema tables are unsupported.")
    try:
        connection.execute("SELECT ordinal FROM resolutions LIMIT 1")
        connection.execute("SELECT ordinal FROM launch_events LIMIT 1")
        connection.execute("SELECT ordinal FROM lifecycle_resolutions LIMIT 1")
    except sqlite3.Error as exc:
        raise ReceiptIdentityError("Recovery receipt schema is unsupported.") from exc
    return schema_version


def _read_attempts(connection: sqlite3.Connection) -> tuple[AttemptStatus, ...]:
    _validate_identity(connection)
    rows = connection.execute("SELECT * FROM attempts ORDER BY attempt_id").fetchall()
    if not rows:
        msg = "Recovery receipt does not contain a committed start envelope."
        raise ReceiptIdentityError(msg)
    attempt_ids = {_as_bytes(row[0]) for row in rows}
    event_attempts = connection.execute("SELECT DISTINCT attempt_id FROM events").fetchall()
    for (event_attempt_id,) in event_attempts:
        if type(event_attempt_id) is not bytes or event_attempt_id not in attempt_ids:
            msg = "Recovery receipt has an event for an undeclared attempt."
            raise ReceiptIdentityError(msg)
    unit_rows = connection.execute("SELECT attempt_id, unit_id FROM units").fetchall()
    declared_units = {(_as_bytes(attempt), _as_bytes(unit)) for attempt, unit in unit_rows}
    resolutions = connection.execute("SELECT attempt_id, unit_id FROM resolutions").fetchall()
    for attempt_id, unit_id in resolutions:
        if (
            type(attempt_id) is not bytes
            or type(unit_id) is not bytes
            or (_as_bytes(attempt_id), _as_bytes(unit_id)) not in declared_units
        ):
            raise ReceiptIdentityError("Recovery receipt has an orphan reconciliation.")
    launch_attempts = connection.execute("SELECT DISTINCT attempt_id FROM launch_events").fetchall()
    lifecycle_attempts = connection.execute(
        "SELECT DISTINCT attempt_id FROM lifecycle_resolutions"
    ).fetchall()
    for (launch_attempt,) in [*launch_attempts, *lifecycle_attempts]:
        if type(launch_attempt) is not bytes or launch_attempt not in attempt_ids:
            raise ReceiptIdentityError("Recovery receipt has an orphan launch relation.")
    return tuple(_read_attempt(connection, row) for row in rows)


def _one_attempt(connection: sqlite3.Connection, attempt_id: AttemptId) -> AttemptStatus:
    _validate_identity(connection)
    row = connection.execute(
        "SELECT * FROM attempts WHERE attempt_id = ?", (_uuid_bytes(attempt_id.value),)
    ).fetchone()
    if row is None:
        raise TransitionError("Recovery attempt does not exist.")
    return _read_attempt(connection, row)


def _read_attempt(connection: sqlite3.Connection, row: tuple[object, ...]) -> AttemptStatus:
    attempt = AttemptId(UUID(bytes=_as_bytes(row[0])))
    start = StartEnvelope(
        attempt, _payload(row, 1), _payload(row, 5), _payload(row, 9), _payload(row, 13)
    )
    unit_rows = connection.execute(
        "SELECT unit_id, sequence, evidence_type, evidence_version, evidence, evidence_digest "
        "FROM units WHERE attempt_id = ? ORDER BY sequence",
        (row[0],),
    ).fetchall()
    units_by_id = {_as_bytes(unit_row[0]): unit_row for unit_row in unit_rows}
    events = connection.execute(
        "SELECT ordinal, unit_id, kind, payload_type, payload_version, payload, digest, "
        "evidence_type, evidence_version, evidence, evidence_digest, process_type, "
        "process_version, process, process_digest, terminal_kind FROM events "
        "WHERE attempt_id = ? ORDER BY ordinal",
        (row[0],),
    ).fetchall()
    unit_events: dict[bytes, list[tuple[object, ...]]] = {unit_id: [] for unit_id in units_by_id}
    unit_states: dict[bytes, TerminalKind | str | None] = dict.fromkeys(units_by_id)
    unit_sequences = {unit_id: _as_int(unit_row[1]) for unit_id, unit_row in units_by_id.items()}
    shutdown: Payload | None = None
    saw_shutdown = False
    for event in events:
        unit_id = event[1]
        kind = _as_str(event[2])
        if saw_shutdown:
            raise ReceiptIdentityError("Recovery receipt has events after clean shutdown.")
        if kind == "shutdown":
            if unit_id is not None or shutdown is not None:
                raise ReceiptIdentityError("Recovery receipt has an invalid clean shutdown event.")
            shutdown = _payload(event, 3)
            saw_shutdown = True
        elif kind in {"intent", "terminal"}:
            if type(unit_id) is not bytes or unit_id not in unit_events:
                raise ReceiptIdentityError("Recovery receipt has an orphan unit event.")
            state = unit_states[unit_id]
            if kind == "intent":
                if state is not None:
                    raise ReceiptIdentityError("Recovery receipt has duplicate unit intent.")
                for prior_id, prior_state in unit_states.items():
                    if unit_sequences[prior_id] < unit_sequences[unit_id] and prior_state not in {
                        TerminalKind.CONFIRMED,
                        TerminalKind.REFUSED,
                    }:
                        raise ReceiptIdentityError(
                            "Recovery receipt has an out-of-order unit intent."
                        )
                unit_states[unit_id] = "intent"
            elif state != "intent":
                raise ReceiptIdentityError(
                    "Recovery receipt has terminal evidence without one intent."
                )
            else:
                unit_states[unit_id] = TerminalKind(_as_str(event[15]))
            unit_events[unit_id].append(event)
        else:
            raise ReceiptIdentityError("Recovery receipt has an unsupported event kind.")
    unit_statuses: list[UnitStatus] = []
    for unit_row in unit_rows:
        unit = Unit(
            UnitId(UUID(bytes=_as_bytes(unit_row[0]))), _as_int(unit_row[1]), _payload(unit_row, 2)
        )
        request: Payload | None = None
        dispatch_evidence: Payload | None = None
        process: Payload | None = None
        terminal: Payload | None = None
        terminal_kind: TerminalKind | None = None
        resolution: Payload | None = None
        resolution_kind: TerminalKind | None = None
        for event in unit_events[_as_bytes(unit_row[0])]:
            kind = _as_str(event[2])
            if kind == "intent" and request is None:
                request = _payload(event, 3)
                dispatch_evidence = _payload(event, 7)
                process = _payload(event, 11)
            elif kind == "terminal" and request is not None and terminal is None:
                terminal = _payload(event, 3)
                terminal_kind = TerminalKind(_as_str(event[15]))
            else:
                raise ReceiptIdentityError("Recovery receipt contains invalid event ordering.")
        resolutions = connection.execute(
            "SELECT request_digest, terminal_kind, evidence_type, evidence_version, evidence, "
            "evidence_digest FROM resolutions WHERE attempt_id = ? AND unit_id = ? ORDER BY ordinal",
            (row[0], unit_row[0]),
        ).fetchall()
        for resolution_row in resolutions:
            if request is None or _as_bytes(resolution_row[0]) != request.digest:
                raise ReceiptIdentityError(
                    "Recovery receipt has a mismatched reconciliation request."
                )
            if terminal_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}:
                raise ReceiptIdentityError(
                    "Recovery receipt resolves an already definitive delivery terminal."
                )
            next_kind = TerminalKind(_as_str(resolution_row[1]))
            if resolution_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}:
                raise ReceiptIdentityError(
                    "Recovery receipt has resolution after a definitive result."
                )
            resolution = _payload(resolution_row, 2)
            resolution_kind = next_kind
        unit_statuses.append(
            UnitStatus(
                unit,
                request,
                dispatch_evidence,
                process,
                terminal,
                terminal_kind,
                resolution,
                resolution_kind,
            )
        )
    launches: tuple[LaunchRecord, ...] = ()
    lifecycle: Payload | None = None
    lifecycle_kind: TerminalKind | None = None
    lifecycle_generation: int | None = None
    launches = _read_launches(connection, row[0])
    lifecycle_rows = connection.execute(
        "SELECT generation, terminal_kind, evidence_type, evidence_version, evidence, evidence_digest "
        "FROM lifecycle_resolutions WHERE attempt_id = ? ORDER BY ordinal",
        (row[0],),
    ).fetchall()
    for lifecycle_row in lifecycle_rows:
        generation = _as_int(lifecycle_row[0])
        next_kind = TerminalKind(_as_str(lifecycle_row[1]))
        if generation > len(launches) or generation < 1:
            raise ReceiptIdentityError(
                "Lifecycle resolution has a stale or invalid launch generation."
            )
        if lifecycle_generation is not None and generation < lifecycle_generation:
            raise ReceiptIdentityError("Lifecycle resolution generation moves backward.")
        if (
            lifecycle_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}
            and lifecycle_generation == generation
        ):
            raise ReceiptIdentityError(
                "Recovery receipt has lifecycle evidence after a definitive result."
            )
        if any(
            launch.status
            not in {LaunchStatus.CLOSED, LaunchStatus.OBSERVED_ABSENT, LaunchStatus.FENCED}
            for launch in launches[:generation]
        ):
            raise ReceiptIdentityError(
                "Lifecycle resolution leaves a current launch unaccounted for."
            )
        lifecycle = _payload(lifecycle_row, 2)
        lifecycle_kind = next_kind
        lifecycle_generation = generation
    return AttemptStatus(
        start,
        tuple(unit_statuses),
        shutdown,
        launches,
        lifecycle,
        lifecycle_kind,
        lifecycle_generation,
    )


def _read_launches(
    connection: sqlite3.Connection, attempt_bytes: object
) -> tuple[LaunchRecord, ...]:
    rows = connection.execute(
        "SELECT launch_id, generation, kind, purpose, context_type, context_version, context, "
        "context_digest, incarnation_type, incarnation_version, incarnation, incarnation_digest, "
        "terminal_kind, evidence_type, evidence_version, evidence, evidence_digest FROM launch_events "
        "WHERE attempt_id = ? ORDER BY ordinal",
        (attempt_bytes,),
    ).fetchall()
    records: dict[bytes, LaunchRecord] = {}
    for event in rows:
        launch_bytes = _as_bytes(event[0])
        generation = _as_int(event[1])
        kind = _as_str(event[2])
        if kind == "intent":
            if launch_bytes in records or generation != len(records) + 1:
                raise ReceiptIdentityError(
                    "Recovery receipt has duplicate or out-of-order launch intent."
                )
            intent = LaunchIntent(
                LaunchId(UUID(bytes=launch_bytes)),
                generation,
                LaunchPurpose(_as_str(event[3])),
                _payload(event, 4),
            )
            records[launch_bytes] = LaunchRecord(intent, None, LaunchStatus.PENDING, None, None)
            continue
        record = records.get(launch_bytes)
        if record is None or record.intent.generation != generation:
            raise ReceiptIdentityError("Recovery receipt has an orphan or mismatched launch event.")
        if kind == "registration":
            if record.status is not LaunchStatus.PENDING:
                raise ReceiptIdentityError(
                    "Recovery receipt has duplicate or late launch registration."
                )
            registration = LaunchRegistration(
                record.intent.launch_id, generation, _payload(event, 8)
            )
            records[launch_bytes] = LaunchRecord(
                record.intent, registration, LaunchStatus.REGISTERED, None, None
            )
        elif kind == "fence":
            if record.status is not LaunchStatus.PENDING:
                raise ReceiptIdentityError("Recovery receipt fences a non-pending launch.")
            records[launch_bytes] = LaunchRecord(
                record.intent, None, LaunchStatus.FENCED, _payload(event, 13), None
            )
        elif kind in {"closure", "absence"}:
            if record.status is not LaunchStatus.REGISTERED:
                raise ReceiptIdentityError("Recovery receipt closes an unregistered launch.")
            terminal_kind = TerminalKind(_as_str(event[12])) if kind == "closure" else None
            if kind == "closure" and terminal_kind not in {
                TerminalKind.CONFIRMED,
                TerminalKind.REFUSED,
            }:
                raise ReceiptIdentityError("Recovery receipt has an invalid launch closure kind.")
            status = LaunchStatus.CLOSED if kind == "closure" else LaunchStatus.OBSERVED_ABSENT
            records[launch_bytes] = LaunchRecord(
                record.intent, record.registration, status, _payload(event, 13), terminal_kind
            )
        else:
            raise ReceiptIdentityError("Recovery receipt has an unsupported launch event kind.")
    return tuple(records.values())


def _payload(row: tuple[object, ...], offset: int) -> Payload:
    payload = Payload(_as_str(row[offset]), _as_int(row[offset + 1]), _as_bytes(row[offset + 2]))
    if payload.digest != _as_bytes(row[offset + 3]):
        raise ReceiptIdentityError("Recovery receipt payload digest does not match its bytes.")
    return payload


def _as_bytes(value: object) -> bytes:
    if type(value) is not bytes:
        raise ReceiptIdentityError("Recovery receipt has an invalid bytes field.")
    return value


def _as_int(value: object) -> int:
    if type(value) is not int:
        raise ReceiptIdentityError("Recovery receipt has an invalid integer field.")
    return value


def _as_str(value: object) -> str:
    if type(value) is not str:
        raise ReceiptIdentityError("Recovery receipt has an invalid text field.")
    return value


def _find_unit(status: AttemptStatus, unit_id: UnitId) -> UnitStatus:
    for unit in status.units:
        if unit.unit.unit_id == unit_id:
            return unit
    raise TransitionError("Recovery unit does not belong to this attempt.")


def _find_launch(status: AttemptStatus, launch_id: LaunchId, generation: int) -> LaunchRecord:
    if type(generation) is not int:
        raise TypeError("A launch generation must be an integer.")
    for launch in status.launches:
        if launch.intent.launch_id == launch_id and launch.intent.generation == generation:
            return launch
    raise TransitionError("Launch identity does not belong to this attempt generation.")


def _has_active_launch_predecessor(status: AttemptStatus, generation: int) -> bool:
    """Whether an earlier launch remains able to acquire or hold an execution grant."""
    return any(
        launch.intent.generation < generation
        and launch.status in {LaunchStatus.PENDING, LaunchStatus.REGISTERED}
        for launch in status.launches
    )


def _append_launch_terminal(
    connection: sqlite3.Connection,
    attempt_id: AttemptId,
    launch_id: LaunchId,
    generation: int,
    event_kind: str,
    evidence: Payload,
    kind: TerminalKind | None,
) -> None:
    connection.execute(
        "INSERT INTO launch_events (attempt_id, launch_id, generation, kind, terminal_kind, "
        "evidence_type, evidence_version, evidence, evidence_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _uuid_bytes(attempt_id.value),
            _uuid_bytes(launch_id.value),
            generation,
            event_kind,
            kind.value if kind is not None else None,
            evidence.content_type,
            evidence.version,
            evidence.data,
            evidence.digest,
        ),
    )


def _require_dispatchable(status: AttemptStatus, unit_id: UnitId) -> None:
    unit = _find_unit(status, unit_id)
    if status.shutdown is not None:
        msg = "A clean shutdown observation closes this attempt to new dispatch intent."
        raise TransitionError(msg)
    if status.lifecycle is not None:
        raise TransitionError(
            "Lifecycle-resolution activity closes this attempt to new dispatch intent."
        )
    if unit.request is not None:
        raise TransitionError("A dispatch intent is already recorded for this unit.")
    for prior in status.units:
        if prior.unit.sequence >= unit.unit.sequence:
            break
        if prior.terminal_kind not in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}:
            raise TransitionError("An earlier unit is incomplete, partial, or unknown.")


def _validate_units(units: tuple[Unit, ...]) -> None:
    if type(units) is not tuple or not units:
        msg = "An attempt requires a nonempty tuple of units."
        raise TypeError(msg)
    if {unit.unit_id for unit in units}.__len__() != len(units) or {
        unit.sequence for unit in units
    } != set(range(len(units))):
        msg = "Attempt units require unique identifiers and contiguous sequences from zero."
        raise ValueError(msg)


def _uuid_bytes(value: UUID) -> bytes:
    return value.bytes


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _has_recovery_sidecar(path: Path) -> bool:
    return path.with_name(f"{path.name}-journal").exists()
