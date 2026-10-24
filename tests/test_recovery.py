# Copyright (c) 2026
# ruff: noqa: D103, E501, PLR2004, S101, S603, TC003

"""Behavioral tests for CLI-owned recovery receipts."""

import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from codex_session_gc.recovery import (
    AttemptId,
    LaunchId,
    LaunchIntent,
    LaunchPurpose,
    LaunchRegistration,
    LaunchStatus,
    Payload,
    ReceiptIdentityError,
    ReceiptMissingError,
    ReceiptRecoveryRequiredError,
    StartEnvelope,
    TerminalKind,
    TransitionError,
    Unit,
    UnitId,
    create,
    fence_launch,
    inspect,
    record_clean_shutdown,
    record_dispatch_intent,
    record_launch_absence,
    record_launch_closure,
    record_launch_intent,
    record_lifecycle_resolution,
    record_reconciliation_evidence,
    record_terminal_evidence,
    recover,
    register_launch,
)


def _payload(data: bytes) -> Payload:
    return Payload("application/test", 1, data)


def _attempt() -> tuple[StartEnvelope, tuple[Unit, Unit]]:
    start = StartEnvelope(
        AttemptId(uuid4()),
        _payload(b"profile"),
        _payload(b"start-evidence"),
        _payload(b"plan"),
        _payload(b"process"),
    )
    return start, (
        Unit(UnitId(uuid4()), 0, _payload(b"unit-zero")),
        Unit(UnitId(uuid4()), 1, _payload(b"unit-one")),
    )


def test_reopen_preserves_exact_payloads_and_completed_prefix(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    request = _payload(b"exact request\x00bytes")
    terminal = _payload(b"exact terminal\x00bytes")
    record_dispatch_intent(
        path, start.attempt_id, units[0].unit_id, request, _payload(b"fresh"), _payload(b"p")
    )
    record_terminal_evidence(
        path, start.attempt_id, units[0].unit_id, request.digest, TerminalKind.CONFIRMED, terminal
    )
    record_dispatch_intent(
        path,
        start.attempt_id,
        units[1].unit_id,
        _payload(b"next"),
        _payload(b"fresh-2"),
        _payload(b"p2"),
    )

    [status] = inspect(path)

    assert status.start.profile.data == b"profile"
    assert status.units[0].request == request
    assert status.units[0].terminal == terminal
    assert status.units[0].terminal_kind is TerminalKind.CONFIRMED
    assert status.units[1].unresolved
    assert status.unresolved_unit == status.units[1]
    assert status.startup_unresolved


def test_startup_without_shutdown_stays_unresolved_without_units(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)

    [status] = inspect(path)

    assert status.startup_unresolved
    record_clean_shutdown(path, start.attempt_id, _payload(b"clean"))
    assert not inspect(path)[0].startup_unresolved
    with pytest.raises(TransitionError):
        record_dispatch_intent(
            path,
            start.attempt_id,
            units[0].unit_id,
            _payload(b"request"),
            _payload(b"evidence"),
            _payload(b"process"),
        )


def test_unknown_blocks_later_intent_and_request_mismatch_refuses(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    request = _payload(b"request")
    record_dispatch_intent(
        path, start.attempt_id, units[0].unit_id, request, _payload(b"e"), _payload(b"p")
    )
    with pytest.raises(TransitionError):
        record_terminal_evidence(
            path,
            start.attempt_id,
            units[0].unit_id,
            b"x" * 32,
            TerminalKind.UNKNOWN,
            _payload(b"unknown"),
        )
    record_terminal_evidence(
        path,
        start.attempt_id,
        units[0].unit_id,
        request.digest,
        TerminalKind.UNKNOWN,
        _payload(b"unknown"),
    )

    with pytest.raises(TransitionError):
        record_dispatch_intent(
            path,
            start.attempt_id,
            units[1].unit_id,
            _payload(b"later"),
            _payload(b"e"),
            _payload(b"p"),
        )


def test_reordered_cross_attempt_and_duplicate_transitions_refuse(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    with pytest.raises(TransitionError):
        record_dispatch_intent(
            path,
            start.attempt_id,
            units[1].unit_id,
            _payload(b"later"),
            _payload(b"e"),
            _payload(b"p"),
        )
    with pytest.raises(TransitionError):
        record_dispatch_intent(
            path,
            AttemptId(uuid4()),
            units[0].unit_id,
            _payload(b"x"),
            _payload(b"e"),
            _payload(b"p"),
        )
    record_dispatch_intent(
        path, start.attempt_id, units[0].unit_id, _payload(b"first"), _payload(b"e"), _payload(b"p")
    )
    with pytest.raises(TransitionError):
        record_dispatch_intent(
            path,
            start.attempt_id,
            units[0].unit_id,
            _payload(b"again"),
            _payload(b"e"),
            _payload(b"p"),
        )


def test_foreign_empty_and_missing_receipts_are_never_initialized_by_inspection(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ReceiptMissingError):
        inspect(missing)
    assert not missing.exists()

    for name in ("empty.sqlite", "foreign.sqlite"):
        path = tmp_path / name
        if name == "empty.sqlite":
            path.write_bytes(b"")
        else:
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE foreign_data (value TEXT)")
        before = path.read_bytes()
        with pytest.raises(ReceiptIdentityError):
            inspect(path)
        assert path.read_bytes() == before


def test_crashed_uncommitted_transition_requires_recovery_and_is_not_visible(
    tmp_path: Path,
) -> None:
    """A reaped owned child leaves no partial event after explicit recovery."""
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    script = """
import os
import sqlite3
import sys
from uuid import UUID

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA synchronous = FULL')
connection.execute('BEGIN IMMEDIATE')
connection.execute(
    \"INSERT INTO events (attempt_id, unit_id, kind, payload_type, payload_version, payload, digest, evidence_type, evidence_version, evidence, evidence_digest, process_type, process_version, process, process_digest, terminal_kind) VALUES (?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)\",
    (UUID(sys.argv[2]).bytes, UUID(sys.argv[3]).bytes, 'application/test', 1, b'request', b'x' * 32, 'application/test', 1, b'evidence', b'y' * 32, 'application/test', 1, b'process', b'z' * 32),
)
os._exit(23)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(path),
            str(start.attempt_id.value),
            str(units[0].unit_id.value),
        ],
        check=False,
        timeout=5,
    )
    assert child.returncode == 23

    with pytest.raises(ReceiptRecoveryRequiredError):
        inspect(path)
    recover(path)
    [status] = inspect(path)
    assert status.units[0].request is None


def test_dispatch_evidence_round_trips_and_tamper_is_refused(tmp_path: Path) -> None:
    """Refreshed dispatch evidence remains distinct from declaration evidence."""
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    evidence = Payload("application/fresh", 2, b"fresh evidence")
    record_dispatch_intent(
        path,
        start.attempt_id,
        units[0].unit_id,
        _payload(b"request"),
        evidence,
        _payload(b"process"),
    )
    assert inspect(path)[0].units[0].dispatch_evidence == evidence
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE events SET evidence = ? WHERE kind = 'intent'", (b"tampered evidence",)
        )
    with pytest.raises(ReceiptIdentityError):
        inspect(path)


def test_orphan_terminal_history_is_refused(tmp_path: Path) -> None:
    """Replay rejects malformed whole-attempt event relations."""
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO events (attempt_id, unit_id, kind, payload_type, payload_version, payload, digest, terminal_kind) VALUES (?, ?, 'terminal', ?, ?, ?, ?, 'confirmed')",
            (
                start.attempt_id.value.bytes,
                units[0].unit_id.value.bytes,
                "application/test",
                1,
                b"terminal",
                _payload(b"terminal").digest,
            ),
        )
    with pytest.raises(ReceiptIdentityError):
        inspect(path)


def test_global_event_order_and_attempt_relations_are_refused(tmp_path: Path) -> None:
    """Replay rejects every persisted event outside the sequential attempt relation."""
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    request = _payload(b"request")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO events (attempt_id, unit_id, kind, payload_type, payload_version, payload, digest, evidence_type, evidence_version, evidence, evidence_digest, process_type, process_version, process, process_digest) VALUES (?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                start.attempt_id.value.bytes,
                units[1].unit_id.value.bytes,
                request.content_type,
                request.version,
                request.data,
                request.digest,
                request.content_type,
                request.version,
                request.data,
                request.digest,
                request.content_type,
                request.version,
                request.data,
                request.digest,
            ),
        )
    with pytest.raises(ReceiptIdentityError):
        inspect(path)


def test_unsupported_journal_schema_refuses_without_migration(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    request = _payload(b"request")
    record_dispatch_intent(
        path, start.attempt_id, units[0].unit_id, request, _payload(b"e"), _payload(b"p")
    )
    record_terminal_evidence(
        path,
        start.attempt_id,
        units[0].unit_id,
        request.digest,
        TerminalKind.UNKNOWN,
        _payload(b"lost"),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE resolutions")
        connection.execute("DROP TABLE launch_events")
        connection.execute("DROP TABLE lifecycle_resolutions")
        connection.execute("UPDATE identity SET schema_version = 1")
        connection.execute("PRAGMA user_version = 1")
    before = path.read_bytes()
    with pytest.raises(ReceiptIdentityError):
        inspect(path)
    with pytest.raises(ReceiptIdentityError):
        record_reconciliation_evidence(
            path,
            start.attempt_id,
            units[0].unit_id,
            request.digest,
            TerminalKind.PARTIAL,
            _payload(b"observation"),
        )
    assert path.read_bytes() == before


def test_reconciliation_refuses_mismatched_request_without_migrating(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    request = _payload(b"request")
    record_dispatch_intent(
        path, start.attempt_id, units[0].unit_id, request, _payload(b"e"), _payload(b"p")
    )
    before = path.read_bytes()
    with pytest.raises(TransitionError):
        record_reconciliation_evidence(
            path,
            start.attempt_id,
            units[0].unit_id,
            b"x" * 32,
            TerminalKind.CONFIRMED,
            _payload(b"wrong"),
        )
    assert path.read_bytes() == before


def test_orphan_reconciliation_is_refused_on_read_only_inspection(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO resolutions VALUES (1, ?, ?, ?, 'unknown', ?, 1, ?, ?)",
            (
                uuid4().bytes,
                uuid4().bytes,
                b"x" * 32,
                "application/test",
                b"e",
                _payload(b"e").digest,
            ),
        )
    with pytest.raises(ReceiptIdentityError):
        inspect(path)

    path = tmp_path / "undeclared.sqlite"
    start, units = _attempt()
    create(path, start, units)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO events (attempt_id, unit_id, kind) VALUES (?, NULL, 'shutdown')",
            (uuid4().bytes,),
        )
    with pytest.raises(ReceiptIdentityError):
        inspect(path)


def test_reconciliation_cannot_rewrite_a_definitive_delivery_terminal(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    request = _payload(b"request")
    record_dispatch_intent(
        path, start.attempt_id, units[0].unit_id, request, _payload(b"e"), _payload(b"p")
    )
    record_terminal_evidence(
        path,
        start.attempt_id,
        units[0].unit_id,
        request.digest,
        TerminalKind.CONFIRMED,
        _payload(b"done"),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO resolutions VALUES (1, ?, ?, ?, 'refused', ?, 1, ?, ?)",
            (
                start.attempt_id.value.bytes,
                units[0].unit_id.value.bytes,
                request.digest,
                "application/test",
                b"late",
                _payload(b"late").digest,
            ),
        )
    with pytest.raises(ReceiptIdentityError):
        inspect(path)


def test_launch_ledger_requires_every_child_before_lifecycle_resolution(tmp_path: Path) -> None:
    """A failed reconciliation child remains part of later lifecycle accounting."""
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    first = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"original"))
    second = LaunchIntent(LaunchId(uuid4()), 2, LaunchPurpose.RECONCILE, _payload(b"child-b"))
    record_launch_intent(path, start.attempt_id, first)
    register_launch(
        path, start.attempt_id, LaunchRegistration(first.launch_id, 1, _payload(b"incarnation-a"))
    )
    record_launch_absence(path, start.attempt_id, first.launch_id, 1, _payload(b"a absent"))
    record_launch_intent(path, start.attempt_id, second)
    register_launch(
        path, start.attempt_id, LaunchRegistration(second.launch_id, 2, _payload(b"incarnation-b"))
    )
    with pytest.raises(TransitionError):
        record_lifecycle_resolution(
            path, start.attempt_id, 2, TerminalKind.CONFIRMED, _payload(b"too early")
        )
    record_launch_closure(
        path,
        start.attempt_id,
        second.launch_id,
        2,
        TerminalKind.REFUSED,
        _payload(b"cleanup failed"),
    )
    record_lifecycle_resolution(
        path, start.attempt_id, 2, TerminalKind.UNKNOWN, _payload(b"uncertain lifecycle")
    )
    [status] = inspect(path)
    assert status.lifecycle_unresolved
    assert status.launches[1].status is LaunchStatus.CLOSED
    third = LaunchIntent(LaunchId(uuid4()), 3, LaunchPurpose.RECONCILE, _payload(b"child-c"))
    record_launch_intent(path, start.attempt_id, third)
    with pytest.raises(TransitionError):
        record_lifecycle_resolution(
            path, start.attempt_id, 3, TerminalKind.CONFIRMED, _payload(b"c unaccounted")
        )


def test_pending_fence_wins_over_registration_and_preserves_originals(tmp_path: Path) -> None:
    """A fenced intent cannot acquire an incarnation or alter startup evidence."""
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    intent = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"context"))
    record_launch_intent(path, start.attempt_id, intent)
    fence_launch(path, start.attempt_id, intent.launch_id, 1, _payload(b"parent lost"))
    with pytest.raises(TransitionError):
        register_launch(
            path, start.attempt_id, LaunchRegistration(intent.launch_id, 1, _payload(b"late child"))
        )
    [status] = inspect(path)
    assert status.start.process.data == b"process"
    assert status.shutdown is None
    assert status.launches[0].status is LaunchStatus.FENCED


def test_lifecycle_generation_reopens_for_later_reconciliation(tmp_path: Path) -> None:
    """A prior resolution cannot conceal a newly unresolved reconciliation launch."""
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    intent = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"a"))
    record_launch_intent(path, start.attempt_id, intent)
    fence_launch(path, start.attempt_id, intent.launch_id, 1, _payload(b"never spawned"))
    record_lifecycle_resolution(
        path, start.attempt_id, 1, TerminalKind.UNKNOWN, _payload(b"retry later")
    )
    next_intent = LaunchIntent(LaunchId(uuid4()), 2, LaunchPurpose.RECONCILE, _payload(b"b"))
    record_launch_intent(path, start.attempt_id, next_intent)
    [status] = inspect(path)
    assert status.lifecycle_unresolved
    assert status.lifecycle_generation == 1
    with pytest.raises(TransitionError):
        record_dispatch_intent(
            path,
            start.attempt_id,
            units[0].unit_id,
            _payload(b"no later dispatch"),
            _payload(b"e"),
            _payload(b"p"),
        )


def test_definitive_lifecycle_allows_needed_reconciliation_for_unresolved_unit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    request = _payload(b"request")
    record_dispatch_intent(
        path, start.attempt_id, units[0].unit_id, request, _payload(b"e"), _payload(b"p")
    )
    record_terminal_evidence(
        path,
        start.attempt_id,
        units[0].unit_id,
        request.digest,
        TerminalKind.UNKNOWN,
        _payload(b"lost"),
    )
    first = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"first"))
    record_launch_intent(path, start.attempt_id, first)
    fence_launch(path, start.attempt_id, first.launch_id, 1, _payload(b"fenced"))
    record_lifecycle_resolution(
        path, start.attempt_id, 1, TerminalKind.CONFIRMED, _payload(b"complete first")
    )

    second = LaunchIntent(LaunchId(uuid4()), 2, LaunchPurpose.RECONCILE, _payload(b"second"))
    record_launch_intent(path, start.attempt_id, second)

    [status] = inspect(path)
    assert status.unresolved_unit is not None
    assert status.lifecycle_kind is TerminalKind.CONFIRMED
    assert status.lifecycle_unresolved
    assert status.launch_generation == 2


def test_reopened_generation_preserves_prior_finality_and_resolves_the_full_attempt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    request = _payload(b"request")
    record_dispatch_intent(
        path, start.attempt_id, units[0].unit_id, request, _payload(b"sent"), _payload(b"process")
    )
    record_terminal_evidence(
        path,
        start.attempt_id,
        units[0].unit_id,
        request.digest,
        TerminalKind.UNKNOWN,
        _payload(b"lost"),
    )
    first = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"first"))
    record_launch_intent(path, start.attempt_id, first)
    fence_launch(path, start.attempt_id, first.launch_id, 1, _payload(b"fenced"))
    record_lifecycle_resolution(
        path, start.attempt_id, 1, TerminalKind.CONFIRMED, _payload(b"first final")
    )
    second = LaunchIntent(LaunchId(uuid4()), 2, LaunchPurpose.RECONCILE, _payload(b"second"))
    record_launch_intent(path, start.attempt_id, second)
    register_launch(
        path, start.attempt_id, LaunchRegistration(second.launch_id, 2, _payload(b"child"))
    )
    record_launch_closure(
        path, start.attempt_id, second.launch_id, 2, TerminalKind.CONFIRMED, _payload(b"closed")
    )
    record_reconciliation_evidence(
        path,
        start.attempt_id,
        units[0].unit_id,
        request.digest,
        TerminalKind.CONFIRMED,
        _payload(b"resolved"),
    )
    record_lifecycle_resolution(
        path, start.attempt_id, 2, TerminalKind.CONFIRMED, _payload(b"second final")
    )
    [status] = inspect(path)
    assert status.units[0].terminal_kind is TerminalKind.UNKNOWN
    assert status.units[0].resolution_kind is TerminalKind.CONFIRMED
    assert not status.lifecycle_unresolved
    before = path.read_bytes()
    with pytest.raises(TransitionError):
        record_lifecycle_resolution(
            path, start.attempt_id, 2, TerminalKind.CONFIRMED, _payload(b"repeat")
        )
    with pytest.raises(TransitionError):
        record_launch_intent(
            path,
            start.attempt_id,
            LaunchIntent(LaunchId(uuid4()), 3, LaunchPurpose.RECONCILE, _payload(b"unneeded")),
        )
    with pytest.raises(TransitionError):
        record_dispatch_intent(
            path,
            start.attempt_id,
            units[0].unit_id,
            request,
            _payload(b"again"),
            _payload(b"process"),
        )
    assert path.read_bytes() == before


def test_definitive_lifecycle_refuses_unneeded_or_apply_reopen(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    first = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"first"))
    record_launch_intent(path, start.attempt_id, first)
    fence_launch(path, start.attempt_id, first.launch_id, 1, _payload(b"fenced"))
    record_lifecycle_resolution(
        path, start.attempt_id, 1, TerminalKind.CONFIRMED, _payload(b"complete first")
    )
    before = path.read_bytes()
    for purpose in (LaunchPurpose.APPLY, LaunchPurpose.RECONCILE):
        with pytest.raises(TransitionError):
            record_launch_intent(
                path,
                start.attempt_id,
                LaunchIntent(LaunchId(uuid4()), 2, purpose, _payload(b"later")),
            )
        assert path.read_bytes() == before


def test_active_launch_predecessor_prevents_second_execution_grant(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    first = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"first"))
    record_launch_intent(path, start.attempt_id, first)
    register_launch(
        path, start.attempt_id, LaunchRegistration(first.launch_id, 1, _payload(b"incarnation"))
    )
    before = path.read_bytes()
    with pytest.raises(TransitionError):
        record_launch_intent(
            path,
            start.attempt_id,
            LaunchIntent(LaunchId(uuid4()), 2, LaunchPurpose.RECONCILE, _payload(b"second")),
        )
    assert path.read_bytes() == before
    record_launch_absence(path, start.attempt_id, first.launch_id, 1, _payload(b"absent"))
    second = LaunchIntent(LaunchId(uuid4()), 2, LaunchPurpose.RECONCILE, _payload(b"second"))
    record_launch_intent(path, start.attempt_id, second)
    register_launch(
        path,
        start.attempt_id,
        LaunchRegistration(second.launch_id, 2, _payload(b"second incarnation")),
    )
    assert inspect(path)[0].launches[1].status is LaunchStatus.REGISTERED


def test_duplicate_launch_identity_refuses_without_changing_receipt(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    launch_id = LaunchId(uuid4())
    first = LaunchIntent(launch_id, 1, LaunchPurpose.APPLY, _payload(b"first"))
    record_launch_intent(path, start.attempt_id, first)
    before = path.read_bytes()
    with pytest.raises(TransitionError):
        record_launch_intent(path, start.attempt_id, first)
    assert path.read_bytes() == before
    fence_launch(path, start.attempt_id, launch_id, 1, _payload(b"fenced"))
    before = path.read_bytes()
    with pytest.raises(TransitionError):
        record_launch_intent(
            path,
            start.attempt_id,
            LaunchIntent(launch_id, 2, LaunchPurpose.RECONCILE, _payload(b"reused")),
        )
    assert path.read_bytes() == before
    assert inspect(path)[0].launches[0].intent.launch_id == launch_id


def test_readonly_inspection_refuses_decreasing_lifecycle_generation(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    start, units = _attempt()
    create(path, start, units)
    first = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"first"))
    record_launch_intent(path, start.attempt_id, first)
    fence_launch(path, start.attempt_id, first.launch_id, 1, _payload(b"first fenced"))
    record_lifecycle_resolution(path, start.attempt_id, 1, TerminalKind.UNKNOWN, _payload(b"one"))
    second = LaunchIntent(LaunchId(uuid4()), 2, LaunchPurpose.RECONCILE, _payload(b"second"))
    record_launch_intent(path, start.attempt_id, second)
    fence_launch(path, start.attempt_id, second.launch_id, 2, _payload(b"second fenced"))
    record_lifecycle_resolution(path, start.attempt_id, 2, TerminalKind.UNKNOWN, _payload(b"two"))
    record_lifecycle_resolution(path, start.attempt_id, 2, TerminalKind.UNKNOWN, _payload(b"three"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE lifecycle_resolutions SET generation = 1 WHERE ordinal = "
            "(SELECT MAX(ordinal) FROM lifecycle_resolutions)"
        )
    before = path.read_bytes()
    with pytest.raises(ReceiptIdentityError):
        inspect(path)
    assert path.read_bytes() == before
