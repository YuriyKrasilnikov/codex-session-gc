# Copyright (c) 2026
# ruff: noqa: EM101, TRY003, PLR0913

"""Compose explicit closed-work batches with the existing no-replay journal.

Helper qualification is supplied by the caller, not inferred here. Collection
is observational; the native operation remains responsible for final validation
under ownership. This module does not reconcile or start a stock app-server.
"""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import asdict, dataclass
from hashlib import file_digest
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from codex_session_gc.execution.closed_work_request import (
    ClosedWorkFacts,
    CollectionLimits,
    QualifiedHelperContract,
    prepare_request,
)
from codex_session_gc.execution.launch import LaunchBinding
from codex_session_gc.execution.one_shot import (
    OneShotError,
    OneShotResult,
    PreGrantCleanup,
    dispatch_once,
)
from codex_session_gc.execution.progress import (
    ProgressChannel,
    ProgressEvent,
    ProgressKind,
    checked_channel,
)
from codex_session_gc.execution.selection_provenance import association_bindings
from codex_session_gc.execution.transport import ChildCommand
from codex_session_gc.planning.closed_work import ClosedWorkSelection, select_closed_work
from codex_session_gc.recovery import (
    AttemptId,
    LaunchId,
    LaunchPurpose,
    LaunchStatus,
    Payload,
    StartEnvelope,
    TerminalKind,
    Unit,
    UnitId,
    create,
    inspect,
    record_clean_shutdown,
    record_lifecycle_resolution,
)

if TYPE_CHECKING:
    from codex_session_gc.domain import ThreadId


@dataclass(frozen=True, slots=True)
class DispatchLimits:
    """Explicit cooperative transport and owned-child cleanup bounds."""

    deadline_seconds: float
    maximum_request_bytes: int
    maximum_output_bytes: int
    cleanup_seconds: float

    def __post_init__(self) -> None:
        """Reject invalid bounds before any receipt or process is created."""
        for value in (self.deadline_seconds, self.cleanup_seconds):
            if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
                raise ValueError("Dispatch deadlines must be positive and finite.")
        for value in (self.maximum_request_bytes, self.maximum_output_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("Dispatch byte bounds must be positive integers.")


@dataclass(frozen=True, slots=True)
class RootResult:
    """A declared work, including work never dispatched after an earlier stop."""

    root: ThreadId
    unit_id: UnitId
    outcome: TerminalKind | None
    dispatch: OneShotResult | None
    detail: str


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Per-root delivery observations without claiming storage reclamation."""

    attempt_id: AttemptId
    outcome: TerminalKind
    roots: tuple[RootResult, ...]
    lifecycle_recorded: bool
    detail: str


def _payload(value: object) -> Payload:
    return Payload(
        "application/vnd.codex-session-gc.closed-work-apply+json",
        1,
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
    )


def _scope(selection: ClosedWorkSelection) -> dict[str, object]:
    return {
        "root": str(next(iter(selection.roots)).value),
        "ownership_scope": sorted(str(x.value) for x in selection.ownership_scope),
        "requested_threads": sorted(str(x.value) for x in selection.requested_threads),
        "deletion_scope": sorted(str(x.value) for x in selection.conditional_candidates),
        "guardian_associations": [
            binding.model_dump(mode="json")
            for binding in association_bindings(selection.guardian_associations)
        ],
    }


def _validate_apply_inputs(
    facts: ClosedWorkFacts,
    selections: tuple[ClosedWorkSelection, ...],
    paths: tuple[Path, ...],
    default_provider: str,
) -> None:
    if type(facts) is not ClosedWorkFacts or type(selections) is not tuple or not selections:
        raise ValueError("Finite facts and a nonempty immutable selection tuple are required.")
    for path in paths:
        if not path.is_absolute():
            raise ValueError("All execution paths must be absolute.")
    if not isinstance(default_provider, str) or not default_provider:
        raise ValueError("The store-default provider is required.")
    seen: set[ThreadId] = set()
    for selection in selections:
        if type(selection) is not ClosedWorkSelection or len(selection.roots) != 1:
            raise ValueError("Each unit must describe exactly one root.")
        refreshed = select_closed_work(
            facts.snapshot,
            facts.cutoff,
            selection.roots,
            selected_threads=selection.requested_threads,
            physical_inventory=facts.physical_inventory,
        )
        if refreshed != selection or selection.errors or not selection.conditional_candidates:
            raise ValueError("A selection is invalid, stale or empty.")
        if seen & selection.ownership_scope:
            raise ValueError("Work ownership scopes must be disjoint.")
        seen.update(selection.ownership_scope)


def _validate_helper_descriptor(descriptor: int, helper: QualifiedHelperContract) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or not metadata.st_mode & 0o111:
        raise ValueError("The qualified helper must be a regular executable.")
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        if file_digest(stream, "sha256").digest() != helper.binary_sha256:
            raise ValueError("The executable differs from the qualified helper digest.")


class ApplyPreflightRefusedError(ValueError):
    """Admission failed before any child launch; the original failure remains its cause."""


def apply_closed_work(  # noqa: C901, PLR0912, PLR0915
    facts: ClosedWorkFacts,
    selections: tuple[ClosedWorkSelection, ...],
    *,
    codex_home: Path,
    sqlite_home: Path,
    journal: Path,
    helper_executable: Path,
    helper: QualifiedHelperContract,
    default_provider: str,
    collection_limits: CollectionLimits,
    dispatch_limits: DispatchLimits,
    attempt_id: AttemptId | None = None,
    declaration: dict[str, object] | None = None,
    progress: ProgressChannel | None = None,
) -> ApplyResult:
    """Declare immutable scopes, then collect and send each root at most once.

    Validation/receipt-creation failures raise ApplyPreflightRefusedError before any child
    launch, retaining the original failure as their cause. A root
    outcome of None means its request was never dispatched. Existing receipts
    are never reopened as a new apply attempt. The pinned Linux executable fd
    binds launched helper bytes to the supplied digest across path replacement.
    """
    descriptor: int | None = None
    try:
        try:
            progress = checked_channel(progress)
            _validate_apply_inputs(
                facts,
                selections,
                (codex_home, sqlite_home, journal, helper_executable),
                default_provider,
            )
            descriptor = os.open(helper_executable, os.O_RDONLY | os.O_NOFOLLOW)
            _validate_helper_descriptor(descriptor, helper)
            attempt = attempt_id or AttemptId(uuid4())
            if progress is not None:
                progress.publish(ProgressEvent(ProgressKind.OPERATION, attempt))
            units = tuple(UnitId(uuid4()) for _ in selections)
            scopes = [_scope(s) for s in selections]
            context = _payload(
                {
                    "codex_home": str(codex_home),
                    "sqlite_home": str(sqlite_home),
                    "journal": str(journal),
                    "helper_path": str(helper_executable),
                    "helper_sha256": helper.binary_sha256.hex(),
                    "source_identity": helper.source_identity,
                    "provider": default_provider,
                    "cutoff": facts.cutoff.timestamp.isoformat(),
                    "collection_limits": asdict(collection_limits),
                    "dispatch_limits": asdict(dispatch_limits),
                    "scopes": scopes,
                    **({"operation_declaration": declaration} if declaration is not None else {}),
                }
            )
            create(
                journal,
                StartEnvelope(attempt, helper.evidence, context, _payload(scopes), context),
                tuple(
                    Unit(unit, index, _payload(scope))
                    for index, (unit, scope) in enumerate(zip(units, scopes, strict=True))
                ),
            )
        except Exception as error:
            message = str(error)
            raise ApplyPreflightRefusedError(message) from error
        results = [
            RootResult(next(iter(s.roots)), u, None, None, "Not attempted.")
            for s, u in zip(selections, units, strict=True)
        ]
        outcome = TerminalKind.CONFIRMED
        cleanup = PreGrantCleanup.NOT_STARTED
        for index, (selection, unit) in enumerate(zip(selections, units, strict=True)):
            if progress is not None:
                progress.publish(
                    ProgressEvent(ProgressKind.ROOT_STARTED, attempt, results[index].root)
                )
            try:
                request = prepare_request(
                    facts,
                    selection,
                    cutoff=facts.cutoff,
                    codex_home=codex_home,
                    sqlite_home=sqlite_home,
                    journal=journal,
                    attempt_id=attempt,
                    unit_id=unit,
                    helper=helper,
                    default_provider=default_provider,
                    limits=collection_limits,
                )
                binding = LaunchBinding(
                    journal, attempt, LaunchId(uuid4()), index + 1, LaunchPurpose.APPLY
                )
                command = ChildCommand(Path(f"/proc/self/fd/{descriptor}"), (), {}, (descriptor,))
                result = dispatch_once(
                    binding, command, unit, request, context, **asdict(dispatch_limits)
                )
            except (ValueError, OSError, OneShotError) as error:
                results[index] = RootResult(results[index].root, unit, None, None, str(error))
                if isinstance(error, OneShotError):
                    cleanup = error.cleanup
                outcome = (
                    TerminalKind.UNKNOWN
                    if cleanup is PreGrantCleanup.UNRESOLVED
                    else TerminalKind.REFUSED
                )
                if progress is not None:
                    progress.publish(
                        ProgressEvent(ProgressKind.ROOT_RESULT, attempt, results[index].root)
                    )
                break
            results[index] = RootResult(
                results[index].root, unit, result.status, result, result.detail
            )
            if progress is not None:
                progress.publish(
                    ProgressEvent(
                        ProgressKind.ROOT_RESULT, attempt, results[index].root, result.status
                    )
                )
            if result.status in {TerminalKind.UNKNOWN, TerminalKind.PARTIAL}:
                outcome = result.status
                break
            if result.status is TerminalKind.REFUSED:
                outcome = TerminalKind.REFUSED
        if outcome is TerminalKind.REFUSED and any(
            root.outcome is TerminalKind.CONFIRMED for root in results
        ):
            outcome = TerminalKind.PARTIAL
        lifecycle = False
        detail = "Declared work processed; inspect per-root outcomes."
        try:
            (stored,) = inspect(journal)
            if cleanup in {PreGrantCleanup.NOT_STARTED, PreGrantCleanup.REAPED} and all(
                launch.status
                in {LaunchStatus.CLOSED, LaunchStatus.OBSERVED_ABSENT, LaunchStatus.FENCED}
                for launch in stored.launches
            ):
                evidence = _payload({"owned_launches_closed": True})
                record_clean_shutdown(journal, attempt, evidence)
                if stored.launches:
                    record_lifecycle_resolution(
                        journal, attempt, stored.launch_generation, TerminalKind.CONFIRMED, evidence
                    )
                lifecycle = True
            else:
                outcome = TerminalKind.UNKNOWN
                detail = "An owned launch still requires lifecycle reconciliation."
        except Exception as error:  # noqa: BLE001
            outcome, detail = TerminalKind.UNKNOWN, f"Lifecycle journal failed: {error}"
        return ApplyResult(attempt, outcome, tuple(results), lifecycle, detail)
    finally:
        if descriptor is not None:
            os.close(descriptor)
