# Copyright (c) 2026

"""Behavioral tests for validated immutable domain inputs."""

from datetime import UTC, datetime, timedelta, tzinfo
from typing import Final

import pytest

from codex_session_gc.domain import (
    ActivityState,
    ArchiveState,
    GraphSnapshot,
    GuardianAssociation,
    HistoryProviderReference,
    InvalidGraphError,
    InvalidIdentifierError,
    InvalidRecordError,
    InvalidTimestampError,
    LastActivity,
    OwnershipEdge,
    RolloutId,
    RolloutRecord,
    SpawnEdge,
    SpawnEdgeStatus,
    ThreadId,
    ThreadOrigin,
    ThreadRecord,
)

MAIN_ID: Final = ThreadId.parse("11111111-1111-4111-8111-111111111111")
FORK_ID: Final = ThreadId.parse("22222222-2222-4222-8222-222222222222")
SUBAGENT_ID: Final = ThreadId.parse("33333333-3333-4333-8333-333333333333")
UNKNOWN_THREAD_ID: Final = ThreadId.parse("44444444-4444-4444-8444-444444444444")
ROLLOUT_ID: Final = RolloutId.parse("11111111-1111-4111-8111-111111111111")
UNKNOWN_ROLLOUT_ID: Final = RolloutId.parse("55555555-5555-4555-8555-555555555555")
PROVIDER_ROLLOUT_ID: Final = RolloutId.parse("66666666-6666-4666-8666-666666666666")
KNOWN_TIMESTAMP: Final = datetime(2030, 1, 2, 12, tzinfo=UTC)
KNOWN_ACTIVITY: Final = LastActivity(KNOWN_TIMESTAMP)
ONE_HOUR: Final = timedelta(hours=1)


def test_preserves_typed_relations_in_an_immutable_snapshot() -> None:
    """Keep spawn, ownership, and history-provider relations distinct."""
    main = _thread(MAIN_ID, ThreadOrigin.MAIN, ActivityState.ACTIVE)
    fork = _thread(FORK_ID, ThreadOrigin.USER_FORK, ActivityState.INACTIVE)
    subagent = _thread(SUBAGENT_ID, ThreadOrigin.SUBAGENT, ActivityState.INACTIVE)
    rollout = RolloutRecord(ROLLOUT_ID, ArchiveState.NOT_ARCHIVED)
    provider_rollout = RolloutRecord(PROVIDER_ROLLOUT_ID, ArchiveState.ARCHIVED)
    spawn = SpawnEdge(MAIN_ID, SUBAGENT_ID)
    ownership = OwnershipEdge(MAIN_ID, ROLLOUT_ID)
    provider_ownership = OwnershipEdge(MAIN_ID, PROVIDER_ROLLOUT_ID)
    history = HistoryProviderReference(ROLLOUT_ID, PROVIDER_ROLLOUT_ID)

    snapshot = GraphSnapshot(
        threads=(main, fork, subagent),
        rollouts=(rollout, provider_rollout),
        spawn_edges=(spawn,),
        ownership_edges=(ownership, provider_ownership),
        history_provider_references=(history,),
    )

    _expect_same(actual=snapshot.spawn_edges, expected=(spawn,))
    _expect_same(actual=snapshot.ownership_edges, expected=(ownership, provider_ownership))
    _expect_same(actual=snapshot.history_provider_references, expected=(history,))
    _expect_same(actual=snapshot.rollouts[0].archive_state, expected=ArchiveState.NOT_ARCHIVED)
    _expect_same(actual=snapshot.rollouts[1].archive_state, expected=ArchiveState.ARCHIVED)


def test_preserves_unknown_activity_and_origin_without_guessing() -> None:
    """Represent unknown observable facts independently from age."""
    unknown = ThreadRecord(
        id=UNKNOWN_THREAD_ID,
        origin=ThreadOrigin.UNKNOWN,
        activity=ActivityState.UNKNOWN,
        archive_state=ArchiveState.UNKNOWN,
        last_activity=LastActivity(None),
    )

    _expect_same(actual=unknown.origin, expected=ThreadOrigin.UNKNOWN)
    _expect_same(actual=unknown.activity, expected=ActivityState.UNKNOWN)
    _expect_same(actual=unknown.last_activity.timestamp, expected=None)


def test_detaches_snapshot_from_caller_collections() -> None:
    """Keep validated graph records stable after caller collection mutation."""
    main = _thread(MAIN_ID, ThreadOrigin.MAIN, ActivityState.ACTIVE)
    subagent = _thread(SUBAGENT_ID, ThreadOrigin.SUBAGENT, ActivityState.INACTIVE)
    rollout = RolloutRecord(ROLLOUT_ID, ArchiveState.NOT_ARCHIVED)
    threads = [main, subagent]
    rollouts = [rollout]
    ownership_edges = [OwnershipEdge(MAIN_ID, ROLLOUT_ID)]

    snapshot = GraphSnapshot.from_records(
        threads=threads,
        rollouts=rollouts,
        ownership_edges=ownership_edges,
    )
    threads.clear()
    rollouts.clear()
    ownership_edges.clear()

    _expect_same(actual=snapshot.threads, expected=(main, subagent))
    _expect_same(actual=snapshot.rollouts, expected=(rollout,))
    _expect_same(actual=snapshot.ownership_edges, expected=(OwnershipEdge(MAIN_ID, ROLLOUT_ID),))


def test_detaches_known_last_activity_from_caller_timezone() -> None:
    """Keep known activity stable after caller timezone mutation."""
    mutable_timezone = MutableTimezone()
    observed_activity = KNOWN_TIMESTAMP.replace(tzinfo=mutable_timezone)
    last_activity = LastActivity(observed_activity)

    mutable_timezone.set_offset(ONE_HOUR)

    _expect_same(actual=last_activity.timestamp, expected=KNOWN_TIMESTAMP)


def test_rejects_malformed_identifiers_and_naive_known_activity() -> None:
    """Refuse unsupported identity and timestamp inputs explicitly."""
    with pytest.raises(InvalidIdentifierError):
        ThreadId.parse("not-a-uuid")
    with pytest.raises(InvalidIdentifierError):
        RolloutId.parse(None)
    with pytest.raises(InvalidTimestampError):
        LastActivity(KNOWN_TIMESTAMP.replace(tzinfo=None))


def test_rejects_duplicate_or_dangling_records_and_relations() -> None:
    """Reject duplicate identities and references outside the snapshot."""
    main = _thread(MAIN_ID, ThreadOrigin.MAIN, ActivityState.ACTIVE)
    subagent = _thread(SUBAGENT_ID, ThreadOrigin.SUBAGENT, ActivityState.INACTIVE)
    rollout = RolloutRecord(ROLLOUT_ID, ArchiveState.NOT_ARCHIVED)

    with pytest.raises(InvalidGraphError):
        GraphSnapshot(threads=(main, main), rollouts=())
    with pytest.raises(InvalidGraphError):
        GraphSnapshot(
            threads=(main,),
            rollouts=(),
            spawn_edges=(SpawnEdge(MAIN_ID, UNKNOWN_THREAD_ID),),
        )
    with pytest.raises(InvalidGraphError):
        GraphSnapshot(threads=(main,), rollouts=(rollout,))
    with pytest.raises(InvalidGraphError):
        GraphSnapshot(
            threads=(main, subagent),
            rollouts=(rollout,),
            ownership_edges=(
                OwnershipEdge(MAIN_ID, ROLLOUT_ID),
                OwnershipEdge(SUBAGENT_ID, ROLLOUT_ID),
            ),
        )
    with pytest.raises(InvalidGraphError):
        GraphSnapshot(
            threads=(main,),
            rollouts=(rollout,),
            ownership_edges=(OwnershipEdge(MAIN_ID, ROLLOUT_ID),),
            history_provider_references=(HistoryProviderReference(UNKNOWN_ROLLOUT_ID, ROLLOUT_ID),),
        )


def _thread(
    thread_id: ThreadId,
    origin: ThreadOrigin,
    activity: ActivityState,
) -> ThreadRecord:
    """Create a non-archived record with known activity for graph tests."""
    return ThreadRecord(
        id=thread_id,
        origin=origin,
        activity=activity,
        archive_state=ArchiveState.NOT_ARCHIVED,
        last_activity=KNOWN_ACTIVITY,
    )


def _expect_same(*, actual: object, expected: object) -> None:
    """Fail when two observable values differ."""
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, got {actual!r}.")


class MutableTimezone(tzinfo):
    """A caller-owned timezone whose offset can change after construction."""

    def __init__(self) -> None:
        """Start at UTC offset."""
        self._offset = timedelta()

    def dst(self, _datetime: datetime | None) -> timedelta:
        """Return no daylight-saving adjustment."""
        return timedelta()

    def set_offset(self, offset: timedelta) -> None:
        """Change the caller-owned offset."""
        self._offset = offset

    def tzname(self, _datetime: datetime | None) -> str:
        """Return a stable human-readable timezone name."""
        return "UTC"

    def utcoffset(self, _datetime: datetime | None) -> timedelta:
        """Return the current caller-owned offset."""
        return self._offset


def test_spawn_lifecycle_requires_typed_status_and_defaults_unknown() -> None:
    """Preserve lifecycle without deriving a thread activity observation."""
    _expect_same(actual=SpawnEdge(MAIN_ID, SUBAGENT_ID).status, expected=SpawnEdgeStatus.UNKNOWN)
    with pytest.raises(InvalidRecordError):
        SpawnEdge(MAIN_ID, SUBAGENT_ID, "closed")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="inactive"):
        SpawnEdgeStatus("inactive")


@pytest.mark.parametrize("second", [SpawnEdgeStatus.OPEN, SpawnEdgeStatus.CLOSED])
def test_spawn_identity_rejects_duplicate_or_conflicting_lifecycle(second: SpawnEdgeStatus) -> None:
    """Lifecycle cannot create two valid relations for the same endpoints."""
    with pytest.raises(InvalidGraphError):
        GraphSnapshot(
            threads=(
                _thread(MAIN_ID, ThreadOrigin.MAIN, ActivityState.UNKNOWN),
                _thread(SUBAGENT_ID, ThreadOrigin.SUBAGENT, ActivityState.UNKNOWN),
            ),
            rollouts=(),
            spawn_edges=(
                SpawnEdge(MAIN_ID, SUBAGENT_ID, SpawnEdgeStatus.OPEN),
                SpawnEdge(MAIN_ID, SUBAGENT_ID, second),
            ),
        )


def test_guardian_association_is_detached_and_parent_may_be_absent() -> None:
    """Historical association does not introduce a live parent record or spawn edge."""
    guardian = _thread(SUBAGENT_ID, ThreadOrigin.GUARDIAN, ActivityState.UNKNOWN)
    association = GuardianAssociation(SUBAGENT_ID, UNKNOWN_THREAD_ID)
    snapshot = GraphSnapshot(threads=(guardian,), rollouts=(), guardian_associations=(association,))
    _expect_same(actual=snapshot.guardian_associations, expected=(association,))
    _expect_same(actual=snapshot.spawn_edges, expected=())
    _expect_same(actual=tuple(record.id for record in snapshot.threads), expected=(SUBAGENT_ID,))


def test_guardian_association_requires_guardian_identity_and_unambiguous_parent() -> None:
    """Invalid provenance cannot be relabelled as ordinary ownership."""
    with pytest.raises(InvalidRecordError):
        GuardianAssociation(SUBAGENT_ID, SUBAGENT_ID)
    association = GuardianAssociation(SUBAGENT_ID, MAIN_ID)
    with pytest.raises(InvalidGraphError):
        GraphSnapshot(
            threads=(_thread(SUBAGENT_ID, ThreadOrigin.SUBAGENT, ActivityState.UNKNOWN),),
            rollouts=(),
            guardian_associations=(association,),
        )
    with pytest.raises(InvalidGraphError):
        GraphSnapshot(
            threads=(_thread(SUBAGENT_ID, ThreadOrigin.GUARDIAN, ActivityState.UNKNOWN),),
            rollouts=(),
            guardian_associations=(association, association),
        )
