# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from codex_session_gc.execution.operation_status import project_operation_status
from codex_session_gc.recovery import (
    AttemptId,
    LaunchId,
    LaunchIntent,
    LaunchPurpose,
    Payload,
    StartEnvelope,
    TerminalKind,
    Unit,
    UnitId,
    create,
    fence_launch,
    inspect,
    record_clean_shutdown,
    record_dispatch_intent,
    record_launch_intent,
    record_lifecycle_resolution,
    record_terminal_evidence,
)


def _payload(value: bytes) -> Payload:
    return Payload("test/operation-status", 1, value)


def _receipt(path: Path, outcomes: tuple[TerminalKind | None, ...]) -> AttemptId:
    attempt = AttemptId(uuid4())
    evidence = _payload(b"declared")
    units = tuple(Unit(UnitId(uuid4()), index, evidence) for index in range(len(outcomes)))
    create(path, StartEnvelope(attempt, evidence, evidence, evidence, evidence), units)
    for unit, kind in zip(units, outcomes, strict=True):
        if kind is None:
            continue
        request = _payload(str(unit.unit_id.value).encode())
        record_dispatch_intent(path, attempt, unit.unit_id, request, evidence, evidence)
        record_terminal_evidence(path, attempt, unit.unit_id, request.digest, kind, evidence)
    return attempt


def test_exact_lifecycle_settlement_resolves_missing_original_shutdown(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    attempt = _receipt(path, (TerminalKind.CONFIRMED,))
    launch = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"launch"))
    record_launch_intent(path, attempt, launch)
    fence_launch(path, attempt, launch.launch_id, 1, _payload(b"never granted"))
    record_lifecycle_resolution(path, attempt, 1, TerminalKind.CONFIRMED, _payload(b"settled"))

    (stored,) = inspect(path)
    result = project_operation_status(stored)

    assert stored.startup_unresolved
    assert stored.shutdown is None
    assert result.outcome is TerminalKind.CONFIRMED
    assert not result.lifecycle_unresolved


def test_no_launch_is_not_positive_process_settlement(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    _receipt(path, (TerminalKind.CONFIRMED,))
    (stored,) = inspect(path)
    assert not stored.lifecycle_unresolved

    result = project_operation_status(stored)

    assert result.outcome is TerminalKind.UNKNOWN
    assert result.lifecycle_unresolved


@pytest.mark.parametrize(
    ("outcomes", "skipped", "expected"),
    [
        ((TerminalKind.CONFIRMED,), (), TerminalKind.CONFIRMED),
        ((TerminalKind.CONFIRMED,), ("skipped-root",), TerminalKind.PARTIAL),
        ((TerminalKind.CONFIRMED, TerminalKind.REFUSED), (), TerminalKind.PARTIAL),
        ((TerminalKind.CONFIRMED, None), (), TerminalKind.PARTIAL),
        ((TerminalKind.REFUSED, None), (), TerminalKind.REFUSED),
        ((None,), (), TerminalKind.REFUSED),
        ((TerminalKind.PARTIAL,), (), TerminalKind.PARTIAL),
        ((TerminalKind.UNKNOWN,), (), TerminalKind.UNKNOWN),
    ],
)
def test_settled_delivery_aggregation_preserves_unattempted_and_skipped_roots(
    tmp_path: Path,
    outcomes: tuple[TerminalKind | None, ...],
    skipped: tuple[str, ...],
    expected: TerminalKind,
) -> None:
    path = tmp_path / "receipt.sqlite"
    attempt = _receipt(path, outcomes)
    record_clean_shutdown(path, attempt, _payload(b"clean"))

    result = project_operation_status(inspect(path)[0], skipped_roots=skipped)

    assert result.outcome is expected
    assert not result.lifecycle_unresolved


def test_dispatched_unit_without_reply_is_unknown_after_shutdown(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    attempt = _receipt(path, (None,))
    (stored,) = inspect(path)
    payload = _payload(b"sent")
    record_dispatch_intent(path, attempt, stored.units[0].unit.unit_id, payload, payload, payload)
    record_clean_shutdown(path, attempt, _payload(b"clean"))

    assert project_operation_status(inspect(path)[0]).outcome is TerminalKind.UNKNOWN


@pytest.mark.parametrize("generation", [None, 0, 2])
def test_lifecycle_payload_without_exact_positive_generation_cannot_settle(
    tmp_path: Path, generation: int | None
) -> None:
    path = tmp_path / "receipt.sqlite"
    attempt = _receipt(path, (TerminalKind.CONFIRMED,))
    launch = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"launch"))
    record_launch_intent(path, attempt, launch)
    (stored,) = inspect(path)
    claimed = replace(
        stored,
        lifecycle=_payload(b"claimed"),
        lifecycle_kind=TerminalKind.CONFIRMED,
        lifecycle_generation=generation,
    )

    result = project_operation_status(claimed)

    assert result.outcome is TerminalKind.UNKNOWN
    assert result.lifecycle_unresolved
