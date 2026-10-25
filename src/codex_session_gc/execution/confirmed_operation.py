# Copyright (c) 2026

"""Fixed-namespace confirmed operations over the existing durable attempt journal."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from anyio import CancelScope

from codex_session_gc.execution.closed_work_operation import (
    ExecutionRequest,
    confirmed_execution_request,
    execute,
)
from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.execution.confirmed_declaration import (
    Declaration,
    ReviewedSelection,
    create_declaration,
    validate_current_declaration,
)
from codex_session_gc.execution.physical_manifest import decode_targets
from codex_session_gc.recovery import RecoveryError, inspect

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.domain import GuardianAssociation


def _identifier(value: str) -> str:
    return str(UUID(value))


class ConfirmedOperations:
    """One startup configuration and owned background calls, without another engine."""

    def __init__(self, home: Path, helper: Path | None, receipts: Path) -> None:
        """Fix all paths before accepting tool requests."""
        self.home = home.resolve()
        self.helper = helper.expanduser().absolute() if helper is not None else None
        self.receipts = receipts.resolve()
        if self.receipts == self.home or self.home in self.receipts.parents:
            message = "Receipt storage must be outside the selected home."
            raise ValueError(message)
        metadata = self.home.stat()
        self.home_identity = (metadata.st_dev, metadata.st_ino)
        self._lock = threading.Lock()
        self._declarations: dict[str, Declaration] = {}
        self._results: dict[str, dict[str, Any]] = {}

    def prepare(
        self,
        roots: tuple[str, ...],
        selected: tuple[str, ...],
        cutoff: str,
        physical_manifest: object,
        guardian_associations: tuple[GuardianAssociation, ...] = (),
    ) -> Declaration:
        """Allocate the future AttemptId without claiming or dispatching an attempt."""
        helper = load_closed_work_contract()
        declaration = create_declaration(
            ReviewedSelection(
                roots=roots,
                selected=selected,
                cutoff=cutoff,
                guardian_associations=guardian_associations,
                physical_manifest=decode_targets(physical_manifest),
            ),
            operation_id=uuid4(),
            home_identity=self.home_identity,
            helper=helper,
        )
        with self._lock:
            self._declarations[declaration.operation_id] = declaration
        return declaration

    def _receipt(self, operation_id: str) -> Path:
        return self.receipts / f"{_identifier(operation_id)}.sqlite"

    def status(self, operation_id: str) -> dict[str, Any]:
        """Observe saved delivery only; absent receipt does not prove a prior RPC stopped."""
        path = self._receipt(operation_id)
        report: dict[str, Any] = {
            "operation_id": _identifier(operation_id),
            "outcome": "unknown",
            "roots": [],
            "receipt": str(path),
            "dispatch_replayed": False,
        }
        if not path.exists():
            completed = self._results.get(_identifier(operation_id))
            if completed is not None:
                return dict(completed)
            report["state"] = "receipt_absent"
            report["detail"] = "No durable claim observed; a prior RPC may still be preparing."
            return report
        try:
            (stored,) = inspect(path)
        except (OSError, RecoveryError) as error:
            report.update(state="receipt_unavailable", detail=str(error))
            return report
        if str(stored.start.attempt_id.value) != _identifier(operation_id):
            message = "Receipt identity differs from the operation identifier."
            raise ValueError(message)
        report.update(
            state="saved",
            lifecycle_unresolved=stored.lifecycle_unresolved,
            roots=[
                {
                    "unit_id": str(unit.unit.unit_id.value),
                    "scope": json.loads(unit.unit.evidence.data),
                    "original_native_outcome": unit.terminal_kind.value
                    if unit.terminal_kind
                    else None,
                    "outcome": unit.effective_terminal_kind.value
                    if unit.effective_terminal_kind
                    else "unknown",
                    "unresolved": unit.unresolved,
                }
                for unit in stored.units
            ],
        )
        declaration = json.loads(stored.start.evidence.data).get("operation_declaration")
        skipped = []
        if isinstance(declaration, dict):
            declared = {row["scope"]["root"] for row in report["roots"]}
            skipped = [root for root in declaration["roots"] if root not in declared]
        report["skipped_roots"] = skipped
        outcomes = {unit.effective_terminal_kind for unit in stored.units}
        if not stored.startup_unresolved and not stored.lifecycle_unresolved:
            values = {outcome.value if outcome else "unknown" for outcome in outcomes}
            if "unknown" in values:
                report["outcome"] = "unknown"
            elif "partial" in values or ("confirmed" in values and (skipped or len(values) > 1)):
                report["outcome"] = "partial"
            elif values:
                report["outcome"] = next(iter(values))
        return report

    def apply(self, declaration: Declaration) -> dict[str, Any]:
        """Claim exclusively using existing journal creation, with no deletion replay."""
        identifier = _identifier(declaration.operation_id)
        if (
            identifier != declaration.operation_id
            or not declaration.roots
            or not declaration.selected
            or tuple(sorted(set(declaration.roots))) != declaration.roots
            or tuple(sorted(set(declaration.selected))) != declaration.selected
        ):
            message = "Declaration requires canonical unique roots and a nonempty exact selection."
            raise ValueError(message)
        if declaration.digest != declaration.calculated_digest():
            message = "Declaration digest does not match the exact payload."
            raise ValueError(message)
        with self._lock:
            previous = self._declarations.get(identifier)
            if previous is not None and previous != declaration:
                message = "Operation identifier is already bound to a different declaration."
                raise ValueError(message)
            path = self._receipt(identifier)
            if path.exists():
                try:
                    (stored,) = inspect(path)
                except OSError, RecoveryError:
                    return self.status(identifier)
                saved = json.loads(stored.start.evidence.data)
                if saved.get("operation_declaration") != declaration.model_dump(mode="json"):
                    message = "Saved operation identifier has a different declaration."
                    raise ValueError(message)
                return self.status(identifier)
            if identifier in self._results:
                return self._results[identifier]
            if previous is None:
                return self.status(identifier)
            result = self._execute_prepared(declaration, path)
            self._results[identifier] = result
            return result

    def _execute_prepared(self, declaration: Declaration, path: Path) -> dict[str, Any]:
        try:
            self._validate_current(declaration)
            request = confirmed_execution_request(
                declaration, receipt=path, home=self.home, helper=self.helper
            )
            self.receipts.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError, TypeError) as error:
            report = {
                "outcome": "refused",
                "detail": str(error),
                "roots": [],
                "receipt": str(path),
            }
        else:
            try:
                report, _ = execute(request)
            except Exception as error:  # noqa: BLE001
                # Engine entry can precede mutation or durable delivery. An exception
                # cannot establish refusal; prefer the saved observation when readable.
                report = {
                    "outcome": "unknown",
                    "detail": str(error),
                    "roots": [],
                    "receipt": str(path),
                }
                if path.exists():
                    with suppress(OSError, RecoveryError, ValueError, TypeError):
                        report = self.status(declaration.operation_id)
        result = {
            **report,
            "operation_id": declaration.operation_id,
            "dispatch_replayed": False,
        }
        if not path.exists():
            result["state"] = "completed_without_receipt"
        return result

    def apply_reference(self, operation_id: str, digest: str) -> dict[str, Any]:
        """Resolve only an exact prepared binding; saved references can observe, never replay."""
        identifier = _identifier(operation_id)
        if (
            identifier != operation_id
            or not isinstance(digest, str)
            or len(digest) != hashlib.sha256().digest_size * 2
        ):
            message = "Confirmation requires a canonical operation UUID and SHA-256 digest."
            raise ValueError(message)
        with self._lock:
            declaration = self._declarations.get(identifier)
            if declaration is None:
                path = self._receipt(identifier)
                if not path.exists():
                    return self.status(identifier)
                try:
                    (stored,) = inspect(path)
                except OSError, RecoveryError:
                    return self.status(identifier)
                value = json.loads(stored.start.evidence.data).get("operation_declaration")
                declaration = Declaration.model_validate(value)
                if (
                    declaration.operation_id != identifier
                    or declaration.digest != declaration.calculated_digest()
                ):
                    message = "Saved operation does not contain a valid exact declaration."
                    raise ValueError(message)
                if declaration.digest != digest:
                    message = "Confirmation digest differs from the saved declaration."
                    raise ValueError(message)
                return self.status(identifier)
            if declaration.digest != digest:
                message = "Confirmation digest differs from the prepared declaration."
                raise ValueError(message)
        return self.apply(declaration)

    def _validate_current(self, declaration: Declaration) -> None:
        metadata = self.home.stat()
        helper = load_closed_work_contract()
        if (metadata.st_dev, metadata.st_ino) != self.home_identity:
            message = "The startup home or qualified profile identity has changed."
            raise ValueError(message)
        validate_current_declaration(declaration, self.home_identity, helper)
        if self.helper is None:
            message = "No qualified helper is available in the startup configuration."
            raise ValueError(message)

    def reconcile(self, operation_id: str) -> dict[str, Any]:
        """Observe saved intents through the current non-dispatch reconciliation path."""
        with self._lock:
            path = self._receipt(operation_id)
            if not path.exists():
                return self.status(operation_id)
            self.status(operation_id)
            report, _ = execute(ExecutionRequest(command="reconcile", receipt=path))
            whole = self.status(operation_id)
            return {
                **report,
                "operation_id": _identifier(operation_id),
                "dispatch_replayed": False,
                "outcome": "unknown" if report["outcome"] == "unknown" else whole["outcome"],
                "skipped_roots": whole.get("skipped_roots", []),
            }

    async def apply_owned(self, operation_id: str, digest: str) -> dict[str, Any]:
        """Keep backend ownership through cancellation and join native cleanup."""
        task = asyncio.create_task(asyncio.to_thread(self.apply_reference, operation_id, digest))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # AnyIO cancellation is level-triggered. Shield its scope while the
            # independently owned backend completes; preserve the original
            # exception so the SDK recognizes its request cancellation scope.
            with CancelScope(shield=True):
                while not task.done():
                    try:
                        await asyncio.shield(asyncio.gather(task, return_exceptions=True))
                    except asyncio.CancelledError:
                        continue
            raise
