# Copyright (c) 2026

"""Typed application composition of selected-work execution and saved-intent observation."""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from codex_session_gc.domain import GuardianAssociation, ThreadId, UtcCutoff
from codex_session_gc.execution.closed_work_apply import (
    ApplyPreflightRefusedError,
    DispatchLimits,
    apply_closed_work,
)
from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
from codex_session_gc.execution.closed_work_profile import (
    ContractRefused,
    load_closed_work_contract,
    reconciliation_contract,
)
from codex_session_gc.execution.closed_work_reconcile import (
    FinalState,
    reconcile,
    reconcile_lifecycle,
)
from codex_session_gc.execution.closed_work_request import review_targets
from codex_session_gc.execution.confirmed_declaration import (
    Declaration,
    ReviewedSelection,
    create_declaration,
    saved_declaration,
    validate_current_declaration,
)
from codex_session_gc.execution.operation_status import project_operation_status
from codex_session_gc.execution.physical_manifest import manifest_bytes, validate_reviewed_manifest
from codex_session_gc.execution.progress import ProgressChannel, checked_channel
from codex_session_gc.execution.resource_policy import (
    READ_BYTES,
    TIMEOUT_SECONDS,
    collection_limits,
    dispatch_limits,
)
from codex_session_gc.execution.selection_provenance import check_reviewed_associations
from codex_session_gc.planning.closed_work import select_closed_work
from codex_session_gc.recovery import AttemptId, TerminalKind, inspect

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from codex_session_gc.execution.closed_work_apply import RootResult
    from codex_session_gc.execution.closed_work_request import (
        ClosedWorkFacts,
        CollectionLimits,
        QualifiedHelperContract,
    )
    from codex_session_gc.planning.closed_work import ClosedWorkSelection
    from codex_session_gc.recovery import AttemptStatus, UnitStatus


@dataclass(frozen=True)
class ExecutionRequest:
    """Transport-independent command for the single native operation engine."""

    command: str
    receipt: Path
    home: Path | None = None
    cutoff: UtcCutoff | None = None
    closed_work: tuple[ThreadId, ...] = ()
    select_thread: tuple[ThreadId, ...] | None = None
    helper: Path | None = None
    default_provider: str | None = None
    max_read_bytes: int | None = None
    timeout_seconds: float | None = None
    attempt_id: AttemptId | None = None
    declaration: dict[str, Any] | None = None
    reviewed_guardian_associations: tuple[GuardianAssociation, ...] | None = None
    reviewed_physical_manifest: bytes | None = None

    def __post_init__(self) -> None:
        """Reject invalid adapter inputs before profiles, storage or native observation."""
        if self.command not in {"apply", "reconcile"} or not self.receipt.is_absolute():
            message = "Execution requires a supported command and an absolute receipt path."
            raise ValueError(message)
        if self.max_read_bytes is not None and self.max_read_bytes < 1:
            message = "The read-byte budget must be positive."
            raise ValueError(message)
        if self.timeout_seconds is not None and (
            not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            message = "The timeout must be positive and finite."
            raise ValueError(message)
        if self.command == "apply":
            self._validate_apply()
        if self.reviewed_physical_manifest is not None:
            if self.select_thread is None:
                message = "Reviewed physical context requires explicit selected thread IDs."
                raise ValueError(message)
            validate_reviewed_manifest(self.reviewed_physical_manifest, self.select_thread)
        if self.reviewed_guardian_associations is not None:
            if self.select_thread is None:
                message = "Reviewed Guardian context requires explicit selected thread IDs."
                raise ValueError(message)
            check_reviewed_associations(self.reviewed_guardian_associations, self.select_thread)

    def _validate_apply(self) -> None:
        if self.home is None or self.cutoff is None or self.helper is None or not self.closed_work:
            message = "Apply requires a home, cutoff, qualified helper and explicit roots."
            raise ValueError(message)
        if not self.home.is_absolute() or not self.helper.is_absolute() or not self.home.is_dir():
            message = "Apply requires an existing absolute home and an absolute helper path."
            raise ValueError(message)
        if self.default_provider == "":
            message = "The default provider must be nonempty."
            raise ValueError(message)


def confirmed_execution_request(
    declaration: Declaration,
    *,
    receipt: Path,
    home: Path,
    helper: Path | None,
) -> ExecutionRequest:
    """Project an exact reviewed binding into the existing one-shot application path."""
    return ExecutionRequest(
        command="apply",
        receipt=receipt,
        home=home,
        cutoff=UtcCutoff(datetime.fromisoformat(declaration.cutoff)),
        closed_work=tuple(ThreadId.parse(value) for value in declaration.roots),
        select_thread=tuple(ThreadId.parse(value) for value in declaration.selected),
        helper=helper,
        attempt_id=AttemptId(UUID(declaration.operation_id)),
        declaration=declaration.model_dump(mode="json"),
        reviewed_physical_manifest=manifest_bytes(declaration.physical_manifest),
        reviewed_guardian_associations=tuple(
            binding.association for binding in declaration.guardian_associations
        ),
    )


def _limits(arguments: ExecutionRequest) -> tuple[CollectionLimits, DispatchLimits]:
    maximum = arguments.max_read_bytes if arguments.max_read_bytes is not None else READ_BYTES
    timeout = (
        arguments.timeout_seconds if arguments.timeout_seconds is not None else TIMEOUT_SECONDS
    )
    return (
        collection_limits(maximum, timeout),
        dispatch_limits(timeout),
    )


def _selection(selection: ClosedWorkSelection) -> dict[str, Any]:
    return {
        "root": str(next(iter(selection.roots)).value),
        "ownershipScope": sorted(str(x.value) for x in selection.ownership_scope),
        "requestedThreads": sorted(str(x.value) for x in selection.requested_threads),
        "selectedThreads": sorted(str(x.value) for x in selection.conditional_candidates),
        "retained": [
            {
                "threadId": str(x.thread_id.value),
                "reason": x.reason.value,
                "witness": str(x.witness.value) if x.witness else None,
            }
            for x in selection.retained
        ],
        "selectionErrors": list(selection.errors),
        "unitId": None,
        "originalNativeOutcome": None,
        "finalObservedState": None,
        "status": "not_dispatched",
    }


class SavedScopeError(ValueError):
    """Saved metadata cannot establish which reviewed roots acquired journal units."""


def saved_skipped_roots(attempt: AttemptStatus) -> tuple[str, ...]:
    """Keep reviewed roots without journal units distinct from unreadable saved scopes."""
    declared = set()
    for unit in attempt.units:
        root = _display_scope(unit.unit.evidence.data)["root"]
        if root is None:
            message = "Saved operation contains an unreadable unit scope."
            raise SavedScopeError(message)
        declared.add(root)
    try:
        context = json.loads(attempt.start.evidence.data)
    except (ValueError, TypeError) as error:
        message = "Saved operation context is unreadable."
        raise SavedScopeError(message) from error
    if not isinstance(context, dict):
        message = "Saved operation context must be an object."
        raise SavedScopeError(message)
    declaration = context.get("operation_declaration")
    if declaration is None:
        return ()
    if not isinstance(declaration, dict) or not isinstance(declaration.get("roots"), list):
        message = "Saved operation reviewed roots are unreadable."
        raise SavedScopeError(message)
    roots = declaration["roots"]
    try:
        canonical = tuple(str(ThreadId.parse(root).value) for root in roots)
    except (ValueError, TypeError) as error:
        message = "Saved operation reviewed roots are invalid."
        raise SavedScopeError(message) from error
    if list(canonical) != roots:
        message = "Saved operation reviewed roots are not canonical."
        raise SavedScopeError(message)
    return tuple(root for root in canonical if root not in declared)


def _exit(outcome: str) -> int:
    return {"confirmed": 0, "refused": 3, "partial": 4, "unknown": 4}[outcome]


@contextmanager
def _engine_phase(report: dict[str, Any], rows: list[dict[str, Any]]) -> Iterator[None]:
    """Keep dispatch uncertain unless the mutator proves refusal before launch."""
    report["outcome"] = "unknown"
    for row in rows:
        row["status"] = "unknown"
    try:
        yield
    except ApplyPreflightRefusedError:
        report["outcome"] = "refused"
        for row in rows:
            row["status"] = "not_dispatched"
        raise


def execute(
    arguments: ExecutionRequest, *, progress: ProgressChannel | None = None
) -> tuple[dict[str, Any], int]:
    """Fail closed on missing bundled qualification before reading inventory."""
    report: dict[str, Any] = {
        "kind": f"online_closed_work_{arguments.command}",
        "outcome": "unknown" if arguments.command == "reconcile" else "refused",
        "receipt": str(arguments.receipt),
        "attemptId": None,
        "roots": [],
    }
    try:
        progress = checked_channel(progress)
        collection, dispatch = _limits(arguments)
        report["limits"] = {"collection": asdict(collection), "dispatch": asdict(dispatch)}
        if arguments.command == "apply":
            helper = load_closed_work_contract()
            _apply(arguments, helper, (collection, dispatch), report, progress=progress)
        else:
            _reconcile(arguments, collection, report)
    except Exception as error:  # noqa: BLE001
        report["detail"] = str(error)
    return report, _exit(str(report["outcome"]))


def _apply(
    arguments: ExecutionRequest,
    helper: QualifiedHelperContract,
    limits: tuple[CollectionLimits, DispatchLimits],
    report: dict[str, Any],
    *,
    progress: ProgressChannel | None = None,
) -> None:
    collection, dispatch = limits
    if arguments.home is None or arguments.cutoff is None or arguments.helper is None:
        message = "Apply requires a home, cutoff and qualified helper."
        raise ValueError(message)
    roots = frozenset(arguments.closed_work)
    selected = frozenset(arguments.select_thread) if arguments.select_thread is not None else None
    facts = read_closed_work_facts(arguments.home, arguments.cutoff, roots, selected, collection)
    report["initialInventoryResourceBounded"] = True
    report["initialIncomingReferencesComplete"] = facts.incoming_references_complete
    combined = select_closed_work(
        facts.snapshot,
        facts.cutoff,
        roots,
        selected_threads=selected,
        physical_inventory=facts.physical_inventory,
    )
    _confirmed_context(arguments, helper)
    _reviewed_context(arguments, facts, combined, arguments.home, collection)
    if combined.errors:
        report["selectionErrors"] = list(combined.errors)
        report["requestedRoots"] = sorted(str(x.value) for x in roots)
        return
    selections = []
    for root in sorted(roots):
        one = select_closed_work(
            facts.snapshot,
            facts.cutoff,
            frozenset({root}),
            physical_inventory=facts.physical_inventory,
        )
        if selected is not None:
            one = select_closed_work(
                facts.snapshot,
                facts.cutoff,
                one.roots,
                selected_threads=selected & one.ownership_scope,
                physical_inventory=facts.physical_inventory,
            )
        selections.append(one)
    rows = [_selection(selection) for selection in selections]
    report["roots"] = rows
    eligible = tuple(x for x in selections if not x.errors and x.conditional_candidates)
    if not eligible:
        report["detail"] = "No eligible selected threads; no helper was dispatched."
        return
    declaration = arguments.declaration
    attempt_id = arguments.attempt_id
    if declaration is None:
        binding = _fresh_binding(arguments, helper, facts, combined, collection)
        declaration = binding.model_dump(mode="json")
        attempt_id = AttemptId(UUID(binding.operation_id))
        eligible = tuple(
            select_closed_work(
                facts.snapshot,
                facts.cutoff,
                selection.roots,
                selected_threads=selection.conditional_candidates,
                physical_inventory=facts.physical_inventory,
            )
            for selection in eligible
        )
    eligible_roots = {next(iter(selection.roots)) for selection in eligible}
    uncertain_rows = [
        row
        for selection, row in zip(selections, rows, strict=True)
        if next(iter(selection.roots)) in eligible_roots
    ]
    with _engine_phase(report, uncertain_rows):
        result = apply_closed_work(
            facts,
            eligible,
            codex_home=arguments.home,
            sqlite_home=arguments.home,
            journal=arguments.receipt,
            helper_executable=arguments.helper,
            helper=helper,
            default_provider=arguments.default_provider or "openai",
            collection_limits=collection,
            dispatch_limits=dispatch,
            attempt_id=attempt_id,
            declaration=declaration,
            progress=progress,
        )
    report["attemptId"] = str(result.attempt_id.value)
    report["lifecycleRecorded"] = result.lifecycle_recorded
    report["detail"] = result.detail
    results = {x.root: x for x in result.roots}
    for selection, row in zip(selections, rows, strict=True):
        native = results.get(next(iter(selection.roots)))
        if native is not None:
            _root_result(row, native)
    (stored,) = inspect(arguments.receipt)
    skipped = saved_skipped_roots(stored)
    projected = project_operation_status(stored, skipped_roots=skipped)
    report.update(
        outcome=projected.outcome.value,
        lifecycleUnresolved=projected.lifecycle_unresolved,
        skipped_roots=list(skipped),
    )


def _root_result(row: dict[str, Any], native: RootResult) -> None:
    row["unitId"] = str(native.unit_id.value)
    row["originalNativeOutcome"] = native.outcome.value if native.outcome else None
    row["status"] = native.outcome.value if native.outcome else "not_dispatched"
    row["detail"] = native.detail
    if native.dispatch is not None and native.dispatch.reply is not None:
        row["nativeReceipt"] = json.loads(native.dispatch.reply.data)


def _receipt_bound(path: Path, maximum: int) -> None:
    size = path.stat().st_size
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            size += sidecar.stat().st_size
    if size > maximum:
        message = "Receipt exceeds the explicit read-byte budget."
        raise ValueError(message)


def _display_scope(data: bytes) -> dict[str, Any]:
    """Render a saved declaration after observation, without granting eligibility."""
    empty = {
        "root": None,
        "ownershipScope": None,
        "requestedThreads": None,
        "selectedThreads": None,
    }
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            return empty
        root = ThreadId.parse(value.get("root"))
        result: dict[str, Any] = {"root": str(root.value)}
        for source, destination in (
            ("ownership_scope", "ownershipScope"),
            ("requested_threads", "requestedThreads"),
            ("deletion_scope", "selectedThreads"),
        ):
            identities = value.get(source)
            if not isinstance(identities, list):
                return empty
            result[destination] = [str(ThreadId.parse(item).value) for item in identities]
    except ValueError, TypeError, AttributeError:
        return empty
    return result


def _saved_unit(unit: UnitStatus) -> dict[str, Any]:
    """Expose saved delivery and declaration independently of fresh observation."""
    return {
        **_display_scope(unit.unit.evidence.data),
        "unitId": str(unit.unit.unit_id.value),
        "originalNativeOutcome": unit.terminal_kind.value if unit.terminal_kind else None,
        "finalObservedState": None,
        "observationSource": "saved",
        "status": unit.effective_terminal_kind.value
        if unit.effective_terminal_kind
        else "not_dispatched",
    }


def _reconcile(
    arguments: ExecutionRequest,
    collection: CollectionLimits,
    report: dict[str, Any],
) -> None:
    report["scope"] = "saved_journal_units"
    report["lifecycleUnresolved"] = None
    _receipt_bound(arguments.receipt, collection.maximum_total_bytes)
    (stored,) = inspect(arguments.receipt)
    report["attemptId"] = str(stored.start.attempt_id.value)
    reconcile_lifecycle(arguments.receipt, stored.start.attempt_id, limits=collection)
    (refreshed,) = inspect(arguments.receipt)
    if refreshed.start.attempt_id != stored.start.attempt_id:
        message = "The saved attempt changed during observation."
        raise ValueError(message)
    stored = refreshed
    rows = [_saved_unit(unit) for unit in stored.units]
    report["roots"] = rows
    helper = None
    for unit, row in zip(stored.units, rows, strict=True):
        effective = unit.effective_terminal_kind
        if unit.unresolved and unit.request is not None:
            try:
                if helper is None:
                    helper = load_closed_work_contract()
                helper = reconciliation_contract(stored.start.profile, helper)
            except ContractRefused as error:
                row.update(observationSource="unavailable", status=TerminalKind.UNKNOWN.value)
                row["detail"] = str(error)
                report["detail"] = str(error)
                continue
            result = reconcile(
                arguments.receipt,
                stored.start.attempt_id,
                unit.unit.unit_id,
                helper=helper,
                limits=collection,
            )
            row.update(
                finalObservedState=result.state.value,
                observationSource="fresh",
                evidenceRecorded=result.recorded,
                evidence=json.loads(result.evidence.data),
            )
            effective = (
                TerminalKind.CONFIRMED
                if result.recorded and result.state is FinalState.CONFIRMED
                else TerminalKind.UNKNOWN
            )
            row["status"] = effective.value
        elif unit.resolution is not None:
            try:
                evidence = json.loads(unit.resolution.data)
                if isinstance(evidence, dict):
                    row["finalObservedState"] = evidence.get("state")
            except ValueError, TypeError:
                effective = TerminalKind.UNKNOWN
                row["status"] = effective.value
    _receipt_bound(arguments.receipt, collection.maximum_total_bytes)
    (refreshed,) = inspect(arguments.receipt)
    if refreshed.start.attempt_id != stored.start.attempt_id:
        message = "The saved attempt changed during observation."
        raise ValueError(message)
    skipped = saved_skipped_roots(refreshed)
    projected = project_operation_status(refreshed, skipped_roots=skipped)
    report.update(
        outcome=projected.outcome.value,
        lifecycleUnresolved=projected.lifecycle_unresolved,
        skipped_roots=list(skipped),
    )


def _fresh_binding(
    arguments: ExecutionRequest,
    helper: QualifiedHelperContract,
    facts: ClosedWorkFacts,
    selection: ClosedWorkSelection,
    collection: CollectionLimits,
) -> Declaration:
    """Bind fresh conditional effects without rereading the physical namespace."""
    if arguments.home is None:
        message = "Fresh confirmation requires the selected home."
        raise ValueError(message)
    targets = review_targets(facts, selection, arguments.home, collection)
    metadata = arguments.home.stat()
    return create_declaration(
        ReviewedSelection(
            roots=tuple(str(value.value) for value in sorted(selection.roots)),
            selected=tuple(target.thread_id for target in targets),
            cutoff=facts.cutoff.timestamp.isoformat(),
            physical_manifest=targets,
            guardian_associations=selection.guardian_associations,
        ),
        operation_id=arguments.attempt_id.value if arguments.attempt_id is not None else uuid4(),
        home_identity=(metadata.st_dev, metadata.st_ino),
        helper=helper,
    )


def _confirmed_context(arguments: ExecutionRequest, helper: QualifiedHelperContract) -> None:
    """Bind a supplied declaration to this execution before journal creation or dispatch."""
    if arguments.declaration is None:
        return
    if arguments.attempt_id is None or arguments.home is None or arguments.cutoff is None:
        message = "A confirmed declaration requires its attempt, home and cutoff."
        raise ValueError(message)
    declaration = saved_declaration(arguments.declaration, arguments.attempt_id.value, helper)
    metadata = arguments.home.stat()
    validate_current_declaration(declaration, (metadata.st_dev, metadata.st_ino), helper)
    if (
        declaration.roots != tuple(str(value.value) for value in sorted(arguments.closed_work))
        or declaration.selected
        != tuple(str(value.value) for value in sorted(arguments.select_thread or ()))
        or declaration.cutoff != arguments.cutoff.timestamp.isoformat()
        or manifest_bytes(declaration.physical_manifest) != arguments.reviewed_physical_manifest
        or tuple(binding.association for binding in declaration.guardian_associations)
        != arguments.reviewed_guardian_associations
    ):
        message = "The confirmed declaration differs from this execution's reviewed inputs."
        raise ValueError(message)


def _reviewed_context(
    arguments: ExecutionRequest,
    facts: ClosedWorkFacts,
    combined: ClosedWorkSelection,
    home: Path,
    collection: CollectionLimits,
) -> None:
    """Check both reviewed semantic bindings before receipt ownership or native dispatch."""
    if arguments.reviewed_guardian_associations is not None:
        observed = {item.guardian_thread_id: item for item in facts.snapshot.guardian_associations}
        reviewed = {
            item.guardian_thread_id: item for item in arguments.reviewed_guardian_associations
        }
        if observed != reviewed:
            message = "Guardian association changed since the exact selection was reviewed."
            raise ValueError(message)
    if arguments.reviewed_physical_manifest is not None:
        current_manifest = manifest_bytes(review_targets(facts, combined, home, collection))
        if current_manifest != arguments.reviewed_physical_manifest:
            message = "The exact physical manifest changed since selection was reviewed."
            raise ValueError(message)
