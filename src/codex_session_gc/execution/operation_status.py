# Copyright (c) 2026

"""Project effective delivery and owned-process evidence without changing a receipt."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from codex_session_gc.recovery import TerminalKind

if TYPE_CHECKING:
    from collections.abc import Collection

    from codex_session_gc.recovery import AttemptStatus


@dataclass(frozen=True, slots=True)
class OperationStatus:
    """Effective operation outcome and whether owned-process settlement is missing."""

    outcome: TerminalKind
    lifecycle_unresolved: bool


def project_operation_status(
    attempt: AttemptStatus, *, skipped_roots: Collection[str] = ()
) -> OperationStatus:
    """Combine saved unit results with positive evidence for the complete launch ledger.

    Later lifecycle evidence can settle ownership without inventing an original
    shutdown event. A launch-free receipt requires its actual shutdown record;
    lack of launch records alone never proves that the operation completed.
    """
    if not lifecycle_settled(attempt):
        return OperationStatus(TerminalKind.UNKNOWN, lifecycle_unresolved=True)
    if any(
        unit.request is not None and unit.effective_terminal_kind is None for unit in attempt.units
    ):
        return OperationStatus(TerminalKind.UNKNOWN, lifecycle_unresolved=False)
    outcomes = tuple(unit.effective_terminal_kind for unit in attempt.units)
    if TerminalKind.UNKNOWN in outcomes:
        outcome = TerminalKind.UNKNOWN
    elif TerminalKind.PARTIAL in outcomes:
        outcome = TerminalKind.PARTIAL
    elif TerminalKind.CONFIRMED in outcomes:
        outcome = (
            TerminalKind.CONFIRMED
            if not skipped_roots and all(kind is TerminalKind.CONFIRMED for kind in outcomes)
            else TerminalKind.PARTIAL
        )
    else:
        outcome = TerminalKind.REFUSED
    return OperationStatus(outcome, lifecycle_unresolved=False)


def lifecycle_settled(attempt: AttemptStatus) -> bool:
    """Whether positive evidence settles the exact complete owned-launch ledger."""
    if not attempt.launches:
        return attempt.shutdown is not None
    return (
        attempt.lifecycle is not None
        and attempt.lifecycle_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}
        and attempt.lifecycle_generation is not None
        and attempt.lifecycle_generation >= 1
        and attempt.lifecycle_generation == attempt.launch_generation
    )
