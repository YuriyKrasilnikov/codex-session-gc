# Copyright (c) 2026

"""Immutable validated domain inputs for inventory and retention."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Self
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Iterable


class DomainValidationError(ValueError):
    """Base class for invalid domain inputs."""


class InvalidIdentifierError(DomainValidationError):
    """Raised when an identifier is missing or malformed."""


class InvalidTimestampError(DomainValidationError):
    """Raised when a known activity timestamp lacks timezone information."""


class InvalidRecordError(DomainValidationError):
    """Raised when a record contains an unsupported observable fact."""


class InvalidGraphError(DomainValidationError):
    """Raised when graph records or relations are inconsistent."""


class ThreadOrigin(StrEnum):
    """Observed origin classification for a thread."""

    MAIN = "main"
    USER_FORK = "user_fork"
    SUBAGENT = "subagent"
    GUARDIAN = "guardian"
    UNKNOWN = "unknown"


class ActivityState(StrEnum):
    """Current activity observation independent from timestamp age."""

    INACTIVE = "inactive"
    ACTIVE = "active"
    IDLE_LOADED = "idle_loaded"
    UNKNOWN = "unknown"


class SpawnEdgeStatus(StrEnum):
    """Observed spawn-edge lifecycle, independent from thread activity."""

    UNKNOWN = "unknown"
    ACTIVE = "active"
    OPEN = "open"
    CLOSED = "closed"


class ArchiveState(StrEnum):
    """Archive observation for a thread or physical rollout."""

    ARCHIVED = "archived"
    NOT_ARCHIVED = "not_archived"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, order=True)
class ThreadId:
    """A UUID-backed semantic identity for a thread."""

    value: UUID

    def __post_init__(self) -> None:
        """Reject non-UUID direct constructor values."""
        if type(self.value) is not UUID:
            msg = "A thread identifier must contain a UUID."
            raise InvalidIdentifierError(msg)

    @classmethod
    def parse(cls, value: object) -> ThreadId:
        """Create a thread identity from a UUID value or its text form."""
        return cls(_parse_uuid(value, "thread"))


@dataclass(frozen=True, slots=True, order=True)
class RolloutId:
    """A UUID-backed semantic identity for a physical rollout."""

    value: UUID

    def __post_init__(self) -> None:
        """Reject non-UUID direct constructor values."""
        if type(self.value) is not UUID:
            msg = "A rollout identifier must contain a UUID."
            raise InvalidIdentifierError(msg)

    @classmethod
    def parse(cls, value: object) -> RolloutId:
        """Create a rollout identity from a UUID value or its text form."""
        return cls(_parse_uuid(value, "rollout"))


@dataclass(frozen=True, slots=True)
class LastActivity:
    """A known timezone-aware activity timestamp or an explicit unknown."""

    timestamp: datetime | None

    def __post_init__(self) -> None:
        """Reject naive known timestamps without inferring a timezone."""
        if self.timestamp is not None and type(self.timestamp) is not datetime:
            msg = "Last activity must be a datetime or explicit unknown."
            raise InvalidTimestampError(msg)
        if self.timestamp is not None and (
            self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None
        ):
            msg = "Known last activity must include timezone information."
            raise InvalidTimestampError(msg)
        if self.timestamp is not None:
            object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """Immutable observable facts about one thread."""

    id: ThreadId
    origin: ThreadOrigin
    activity: ActivityState
    archive_state: ArchiveState
    last_activity: LastActivity

    def __post_init__(self) -> None:
        """Validate all explicitly observed thread facts."""
        if type(self.id) is not ThreadId:
            msg = "A thread record requires a thread identifier."
            raise InvalidRecordError(msg)
        if type(self.origin) is not ThreadOrigin:
            msg = "A thread record requires an explicit origin classification."
            raise InvalidRecordError(msg)
        if type(self.activity) is not ActivityState:
            msg = "A thread record requires an explicit activity classification."
            raise InvalidRecordError(msg)
        if type(self.archive_state) is not ArchiveState:
            msg = "A thread record requires an explicit archive observation."
            raise InvalidRecordError(msg)
        if type(self.last_activity) is not LastActivity:
            msg = "A thread record requires explicit last activity state."
            raise InvalidRecordError(msg)


@dataclass(frozen=True, slots=True)
class RolloutRecord:
    """Immutable observable facts about one physical rollout."""

    id: RolloutId
    archive_state: ArchiveState

    def __post_init__(self) -> None:
        """Validate all explicitly observed rollout facts."""
        if type(self.id) is not RolloutId:
            msg = "A rollout record requires a rollout identifier."
            raise InvalidRecordError(msg)
        if type(self.archive_state) is not ArchiveState:
            msg = "A rollout record requires an explicit archive observation."
            raise InvalidRecordError(msg)


@dataclass(frozen=True, slots=True)
class SpawnEdge:
    """A typed relation from a spawning thread to its subagent thread."""

    parent_thread_id: ThreadId
    child_thread_id: ThreadId
    status: SpawnEdgeStatus = SpawnEdgeStatus.UNKNOWN

    def __post_init__(self) -> None:
        """Require thread identities at both ends of a spawn relation."""
        if (
            type(self.parent_thread_id) is not ThreadId
            or type(self.child_thread_id) is not ThreadId
        ):
            msg = "A spawn edge requires thread identifiers."
            raise InvalidRecordError(msg)
        if type(self.status) is not SpawnEdgeStatus:
            msg = "A spawn edge requires a typed lifecycle observation."
            raise InvalidRecordError(msg)


@dataclass(frozen=True, slots=True)
class GuardianAssociation:
    """Historical reviewer association, independent of spawn ownership or closure."""

    guardian_thread_id: ThreadId
    associated_parent_thread_id: ThreadId

    def __post_init__(self) -> None:
        """Require distinct typed identities without requiring a live parent record."""
        if (
            type(self.guardian_thread_id) is not ThreadId
            or type(self.associated_parent_thread_id) is not ThreadId
            or self.guardian_thread_id == self.associated_parent_thread_id
        ):
            msg = "A Guardian association requires distinct thread identifiers."
            raise InvalidRecordError(msg)


@dataclass(frozen=True, slots=True)
class OwnershipEdge:
    """A typed relation from a thread to its physical rollout."""

    thread_id: ThreadId
    rollout_id: RolloutId

    def __post_init__(self) -> None:
        """Require distinct semantic identity types for ownership."""
        if type(self.thread_id) is not ThreadId or type(self.rollout_id) is not RolloutId:
            msg = "An ownership edge requires thread and rollout identifiers."
            raise InvalidRecordError(msg)


@dataclass(frozen=True, slots=True)
class HistoryProviderReference:
    """A typed relation from a rollout to its physical history provider."""

    rollout_id: RolloutId
    provider_rollout_id: RolloutId

    def __post_init__(self) -> None:
        """Require rollout and physical provider identities."""
        if (
            type(self.rollout_id) is not RolloutId
            or type(self.provider_rollout_id) is not RolloutId
        ):
            msg = "A history reference requires rollout identifiers."
            raise InvalidRecordError(msg)


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """A detached validated set of records and typed relations."""

    threads: tuple[ThreadRecord, ...]
    rollouts: tuple[RolloutRecord, ...]
    spawn_edges: tuple[SpawnEdge, ...] = ()
    ownership_edges: tuple[OwnershipEdge, ...] = ()
    history_provider_references: tuple[HistoryProviderReference, ...] = ()
    guardian_associations: tuple[GuardianAssociation, ...] = ()

    @classmethod
    def from_records(
        cls,
        *,
        threads: Iterable[ThreadRecord],
        rollouts: Iterable[RolloutRecord],
        spawn_edges: Iterable[SpawnEdge] = (),
        ownership_edges: Iterable[OwnershipEdge] = (),
        history_provider_references: Iterable[HistoryProviderReference] = (),
    ) -> Self:
        """Create a detached snapshot from caller-owned collections."""
        return cls(
            threads=_freeze_threads(threads),
            rollouts=_freeze_rollouts(rollouts),
            spawn_edges=_freeze_spawn_edges(spawn_edges),
            ownership_edges=_freeze_ownership_edges(ownership_edges),
            history_provider_references=_freeze_history_references(history_provider_references),
        )

    def __post_init__(self) -> None:
        """Detach caller containers and validate graph consistency."""
        object.__setattr__(self, "threads", _freeze_threads(self.threads))
        object.__setattr__(self, "rollouts", _freeze_rollouts(self.rollouts))
        object.__setattr__(self, "spawn_edges", _freeze_spawn_edges(self.spawn_edges))
        object.__setattr__(self, "ownership_edges", _freeze_ownership_edges(self.ownership_edges))
        object.__setattr__(
            self, "guardian_associations", _freeze_guardian_associations(self.guardian_associations)
        )
        object.__setattr__(
            self,
            "history_provider_references",
            _freeze_history_references(self.history_provider_references),
        )
        _validate_snapshot(self)


def _parse_uuid(value: object, label: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        msg = f"A {label} identifier must be a UUID or UUID text."
        raise InvalidIdentifierError(msg)
    try:
        return UUID(value)
    except ValueError as exc:
        msg = f"A {label} identifier must be valid UUID text."
        raise InvalidIdentifierError(msg) from exc


def _freeze_threads(value: Iterable[ThreadRecord]) -> tuple[ThreadRecord, ...]:
    try:
        return tuple(value)
    except TypeError as exc:
        msg = "Threads must be an iterable of thread records."
        raise InvalidGraphError(msg) from exc


def _freeze_rollouts(value: Iterable[RolloutRecord]) -> tuple[RolloutRecord, ...]:
    try:
        return tuple(value)
    except TypeError as exc:
        msg = "Rollouts must be an iterable of rollout records."
        raise InvalidGraphError(msg) from exc


def _freeze_spawn_edges(value: Iterable[SpawnEdge]) -> tuple[SpawnEdge, ...]:
    try:
        return tuple(value)
    except TypeError as exc:
        msg = "Spawn edges must be an iterable of spawn edges."
        raise InvalidGraphError(msg) from exc


def _freeze_ownership_edges(value: Iterable[OwnershipEdge]) -> tuple[OwnershipEdge, ...]:
    try:
        return tuple(value)
    except TypeError as exc:
        msg = "Ownership edges must be an iterable of ownership edges."
        raise InvalidGraphError(msg) from exc


def _freeze_guardian_associations(
    value: Iterable[GuardianAssociation],
) -> tuple[GuardianAssociation, ...]:
    try:
        return tuple(value)
    except TypeError as exc:
        msg = "Guardian associations must be an iterable of typed associations."
        raise InvalidGraphError(msg) from exc


def _freeze_history_references(
    value: Iterable[HistoryProviderReference],
) -> tuple[HistoryProviderReference, ...]:
    try:
        return tuple(value)
    except TypeError as exc:
        msg = "History references must be an iterable of history references."
        raise InvalidGraphError(msg) from exc


def _validate_snapshot(snapshot: GraphSnapshot) -> None:
    _require_record_types(snapshot)
    thread_ids = {record.id for record in snapshot.threads}
    rollout_ids = {record.id for record in snapshot.rollouts}
    if len(thread_ids) != len(snapshot.threads):
        msg = "A snapshot cannot contain duplicate thread record identifiers."
        raise InvalidGraphError(msg)
    if len(rollout_ids) != len(snapshot.rollouts):
        msg = "A snapshot cannot contain duplicate rollout record identifiers."
        raise InvalidGraphError(msg)
    _validate_spawn_edges(snapshot.spawn_edges, thread_ids)
    _validate_ownership_edges(snapshot.ownership_edges, thread_ids, rollout_ids)
    _validate_history_references(snapshot.history_provider_references, rollout_ids)
    _validate_guardian_associations(snapshot.guardian_associations, snapshot.threads)


def _require_record_types(snapshot: GraphSnapshot) -> None:
    if not all(isinstance(item, GuardianAssociation) for item in snapshot.guardian_associations):
        msg = "A snapshot must contain typed Guardian associations."
        raise InvalidGraphError(msg)
    if not all(isinstance(record, ThreadRecord) for record in snapshot.threads):
        msg = "A snapshot must contain thread records."
        raise InvalidGraphError(msg)
    if not all(isinstance(record, RolloutRecord) for record in snapshot.rollouts):
        msg = "A snapshot must contain rollout records."
        raise InvalidGraphError(msg)
    if not all(isinstance(edge, SpawnEdge) for edge in snapshot.spawn_edges):
        msg = "A snapshot must contain spawn edges."
        raise InvalidGraphError(msg)
    if not all(isinstance(edge, OwnershipEdge) for edge in snapshot.ownership_edges):
        msg = "A snapshot must contain ownership edges."
        raise InvalidGraphError(msg)
    if not all(
        isinstance(reference, HistoryProviderReference)
        for reference in snapshot.history_provider_references
    ):
        msg = "A snapshot must contain history provider references."
        raise InvalidGraphError(msg)


def _validate_spawn_edges(edges: tuple[SpawnEdge, ...], thread_ids: set[ThreadId]) -> None:
    identities = {(edge.parent_thread_id, edge.child_thread_id) for edge in edges}
    if len(identities) != len(edges):
        msg = "A snapshot cannot contain duplicate spawn edges."
        raise InvalidGraphError(msg)
    for edge in edges:
        if edge.parent_thread_id not in thread_ids or edge.child_thread_id not in thread_ids:
            msg = "A spawn edge cannot reference an unknown thread."
            raise InvalidGraphError(msg)


def _validate_guardian_associations(
    associations: tuple[GuardianAssociation, ...], records: tuple[ThreadRecord, ...]
) -> None:
    guardians = {record.id for record in records if record.origin is ThreadOrigin.GUARDIAN}
    seen = set()
    for association in associations:
        if (
            association.guardian_thread_id not in guardians
            or association.guardian_thread_id in seen
        ):
            msg = "A Guardian requires its own record and at most one historical association."
            raise InvalidGraphError(msg)
        seen.add(association.guardian_thread_id)


def _validate_ownership_edges(
    edges: tuple[OwnershipEdge, ...],
    thread_ids: set[ThreadId],
    rollout_ids: set[RolloutId],
) -> None:
    owners: dict[RolloutId, ThreadId] = {}
    for edge in edges:
        if edge.thread_id not in thread_ids or edge.rollout_id not in rollout_ids:
            msg = "An ownership edge cannot reference an unknown record."
            raise InvalidGraphError(msg)
        if edge.rollout_id in owners:
            msg = "A rollout must have exactly one ownership edge."
            raise InvalidGraphError(msg)
        owners[edge.rollout_id] = edge.thread_id
    if set(owners) != rollout_ids:
        msg = "Every rollout record requires one ownership edge."
        raise InvalidGraphError(msg)


def _validate_history_references(
    references: tuple[HistoryProviderReference, ...],
    rollout_ids: set[RolloutId],
) -> None:
    if len(set(references)) != len(references):
        msg = "A snapshot cannot contain duplicate history references."
        raise InvalidGraphError(msg)
    for reference in references:
        if (
            reference.rollout_id not in rollout_ids
            or reference.provider_rollout_id not in rollout_ids
        ):
            msg = "A history reference cannot reference an unknown record."
            raise InvalidGraphError(msg)
