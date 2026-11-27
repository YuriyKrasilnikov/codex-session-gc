# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Read-only preview jobs and immutable console-to-execution declarations."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from codex_session_gc.domain import ExactIds, GuardianAssociation, ThreadId, UtcCutoff
from codex_session_gc.execution.closed_work_operation import confirmed_execution_request, execute
from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.execution.closed_work_request import review_targets
from codex_session_gc.execution.confirmed_declaration import (
    Declaration,
    ReviewedSelection,
    create_declaration,
)
from codex_session_gc.execution.physical_manifest import (
    decode_targets,
    manifest_bytes,
    validate_reviewed_manifest,
)
from codex_session_gc.execution.selection_provenance import (
    association_bindings,
    check_reviewed_associations,
    reviewed_associations,
)
from codex_session_gc.execution.timing import (
    Count,
    Phase,
    TimingObserver,
    count,
    current,
    interval,
    observe,
    signal_worker_summary,
)
from codex_session_gc.inventory.selection import project_preview_selection

if TYPE_CHECKING:
    from codex_session_gc.execution.progress import ProgressChannel


_TIMING_BYTES = 16384


class PreviewMode(StrEnum):
    """Observation stays independent of allocating a confirmation operation."""

    OBSERVE = "observe"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class Intent:
    """Exact reviewed selection; refreshes may retain members but never add deletions."""

    home: Path
    cutoff: UtcCutoff | ExactIds
    roots: tuple[ThreadId, ...]
    selected: tuple[ThreadId, ...]
    reviewed_guardian_associations: tuple[GuardianAssociation, ...] | None = None
    reviewed_physical_manifest: bytes | None = None
    declaration: Declaration | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous duplicate inputs before any storage observation."""
        if isinstance(self.cutoff, ExactIds) and self.cutoff.ids != frozenset(self.selected):
            raise ValueError("Exact policy must match the selected histories.")
        if len(set(self.roots)) != len(self.roots) or len(set(self.selected)) != len(self.selected):
            raise ValueError("Selected work and history identifiers must be unique.")
        if self.reviewed_physical_manifest is not None:
            validate_reviewed_manifest(self.reviewed_physical_manifest, self.selected)
        if self.reviewed_guardian_associations is not None:
            check_reviewed_associations(self.reviewed_guardian_associations, self.selected)
        if self.declaration is not None:
            declaration = self.declaration
            if (
                declaration.digest != declaration.calculated_digest()
                or declaration.roots != tuple(str(value.value) for value in sorted(self.roots))
                or declaration.selected
                != tuple(str(value.value) for value in sorted(self.selected))
                or declaration.cutoff != cutoff_text(self.cutoff)
                or manifest_bytes(declaration.physical_manifest) != self.reviewed_physical_manifest
                or tuple(binding.association for binding in declaration.guardian_associations)
                != self.reviewed_guardian_associations
            ):
                raise ValueError("Confirmation binding differs from the reviewed intent.")

    def payload(self) -> dict[str, Any]:
        """Serialize only explicit inputs to the private read-only worker."""
        return {
            "home": str(self.home),
            "cutoff": cutoff_text(self.cutoff),
            "roots": [str(x.value) for x in sorted(self.roots)],
            "selected": [str(x.value) for x in sorted(self.selected)],
        }

    def confirmation(self) -> Declaration:
        """Require the exact binding before execution or operation-identity display."""
        if self.declaration is None:
            raise ValueError("Apply requires the reviewed confirmation binding.")
        return self.declaration


def cutoff_text(cutoff: UtcCutoff | ExactIds) -> str | None:
    """Return the canonical declared boundary, or null for exact IDs."""
    return cutoff.timestamp.isoformat() if isinstance(cutoff, UtcCutoff) else None


def parse_cutoff(text: str) -> UtcCutoff:
    """Accept an editable UTC instant or a date interpreted as UTC midnight."""
    parsed = datetime.fromisoformat(text)
    if "T" not in text and " " not in text:
        parsed = parsed.replace(tzinfo=UTC)
    return UtcCutoff(parsed)


def preview(intent: Intent, *, mode: PreviewMode = PreviewMode.OBSERVE) -> dict[str, Any]:
    """Explain an exact selection using scoped metadata and selected headers."""
    if mode is PreviewMode.CONFIRM:
        helper = load_closed_work_contract()
        metadata = intent.home.stat()
        home_identity = (metadata.st_dev, metadata.st_ino)
    report = preview_selection(
        intent.home,
        intent.cutoff,
        frozenset(intent.roots),
        frozenset(intent.selected),
    )
    if mode is PreviewMode.CONFIRM and not report["errors"] and report["deletions"]:
        current = intent.home.stat()
        if (current.st_dev, current.st_ino) != home_identity:
            raise ValueError("The home identity changed during preview.")
        reviewed = _reviewed_intent(intent, report)
        declaration = create_declaration(
            ReviewedSelection(
                roots=tuple(str(value.value) for value in sorted(reviewed.roots)),
                selected=tuple(str(value.value) for value in sorted(reviewed.selected)),
                cutoff=cutoff_text(reviewed.cutoff),
                physical_manifest=decode_targets(report["physical_manifest"]),
                guardian_associations=reviewed.reviewed_guardian_associations or (),
            ),
            operation_id=uuid4(),
            home_identity=home_identity,
            helper=helper,
        )
        report["declaration"] = declaration.model_dump(mode="json")
    return report


def preview_selection(
    home: Path,
    cutoff: UtcCutoff | ExactIds,
    roots: frozenset[ThreadId],
    selected: frozenset[ThreadId] | None,
) -> dict[str, Any]:
    """Resolve a whole-work or explicit subset into one conditional declaration."""
    from codex_session_gc.execution.closed_work_facts import (  # noqa: PLC0415
        read_closed_work_facts,
    )
    from codex_session_gc.execution.resource_policy import collection_limits  # noqa: PLC0415
    from codex_session_gc.planning.attachment_cleanup import (  # noqa: PLC0415
        AdvisoryAncestry,
        attachment_cleanup,
    )
    from codex_session_gc.planning.closed_work import select_closed_work  # noqa: PLC0415

    before = home.stat()
    home_identity = (before.st_dev, before.st_ino)
    ancestry = AdvisoryAncestry()
    facts = read_closed_work_facts(
        home,
        cutoff,
        roots,
        selected,
        collection_limits(),
        advisory_ancestry=ancestry,
    )
    after = home.stat()
    if (after.st_dev, after.st_ino) != home_identity:
        raise ValueError("The home identity changed during preview.")
    with interval(Phase.SELECTION_MANIFEST):
        selection = select_closed_work(
            facts.snapshot,
            facts.cutoff,
            roots,
            selected_threads=selected,
            physical_inventory=facts.physical_inventory,
        )
        candidates = sorted(selection.conditional_candidates)
        targets = review_targets(facts, selection, home, collection_limits())
    selection_summary = (
        project_preview_selection(facts.snapshot, facts.physical_inventory, selected)
        if selected is not None
        else None
    )
    count(Count.ROOTS, len(roots))
    count(Count.CANDIDATES, len(candidates))
    sizes = {
        ThreadId.parse(target.thread_id): sum(
            item.physical_identity.size for item in target.rollouts
        )
        for target in targets
    }
    selected_physical = {
        item.ownership.rollout_id
        for item in facts.physical_inventory.rollouts
        if item.ownership.thread_id in selection.conditional_candidates
    }
    physicals = {item.ownership.rollout_id: item for item in facts.physical_inventory.rollouts}
    providers = {
        item.ownership.rollout_id: item.history_prefix.provider_rollout_id
        for item in facts.physical_inventory.rollouts
        if item.history_prefix is not None
    }
    retained_providers = set()
    visited = set()
    pending = list(selected_physical)
    while pending:
        rollout = pending.pop()
        if rollout in visited:
            continue
        visited.add(rollout)
        provider = providers.get(rollout)
        if provider is not None:
            if provider not in selected_physical:
                retained_providers.add(provider)
            pending.append(provider)
    shared_retained_bytes = 0
    for identifier in retained_providers:
        physical = physicals[identifier].physical
        if physical is None:
            raise ValueError("A retained physical provider extent is unknown.")
        shared_retained_bytes += physical.stat.stored_bytes
    intent = Intent(home, cutoff, tuple(sorted(roots)), tuple(sorted(selection.requested_threads)))
    return {
        **intent.payload(),
        "kind": "closed_work_selection",
        "home_identity": list(home_identity),
        "selection_summary": selection_summary,
        "attachment_cleanup": attachment_cleanup(facts.snapshot, selected, ancestry, home_identity),
        "selected_attachments": {
            str(child.value): str(source.value)
            for child, source in sorted(ancestry.selected_attachments.items())
        },
        "physical_manifest": [target.model_dump(mode="json") for target in targets],
        "physical_lineage": "observed",
        "known_stored_bytes": sum(sizes.values()),
        "conditional_reclaim_bytes": sum(sizes.values()),
        "shared_retained_bytes": shared_retained_bytes,
        "guardian_associations": [
            binding.model_dump(mode="json")
            for binding in association_bindings(selection.guardian_associations)
        ],
        "deletions": [str(x.value) for x in candidates],
        "sizes": [sizes.get(x) for x in candidates],
        "retained": [
            {
                "id": str(x.thread_id.value),
                "reason": x.reason.value,
                "witness": str(x.witness.value) if x.witness else None,
            }
            for x in selection.retained
        ],
        "errors": list(selection.errors),
        "conditional": True,
        "incomingReferencesComplete": facts.incoming_references_complete,
        "closureCertified": False,
        "executable": False,
    }


def _reviewed_intent(original: Intent, report: dict[str, Any]) -> Intent:
    """Bind confirmation to the displayed inputs and exact candidate subset."""
    if any(report.get(k) != v for k, v in original.payload().items()) or report.get("errors"):
        raise ValueError("Preview does not match the selected inputs or contains errors.")
    selected = tuple(ThreadId.parse(x) for x in report["deletions"])
    if (
        not selected
        or len(set(selected)) != len(selected)
        or not set(selected) <= set(original.selected)
    ):
        raise ValueError("Preview has no candidates or broadens the explicit selection.")
    associations = reviewed_associations(report.get("guardian_associations"))
    if any(item.guardian_thread_id not in selected for item in associations):
        raise ValueError("Preview association exceeds the confirmed exact selection.")
    return Intent(
        original.home,
        ExactIds(frozenset(selected)) if isinstance(original.cutoff, ExactIds) else original.cutoff,
        original.roots,
        selected,
        associations,
        manifest_bytes(report["physical_manifest"]),
    )


def confirmed_intent(original: Intent, report: dict[str, Any]) -> Intent:
    """Keep the worker's exact identity binding through explicit terminal confirmation."""
    reviewed = _reviewed_intent(original, report)
    return Intent(
        reviewed.home,
        reviewed.cutoff,
        reviewed.roots,
        reviewed.selected,
        reviewed.reviewed_guardian_associations,
        reviewed.reviewed_physical_manifest,
        Declaration.model_validate(report.get("declaration")),
    )


def apply_intent(
    intent: Intent, helper: Path, receipt: Path, *, progress: ProgressChannel | None = None
) -> tuple[dict[str, Any], int]:
    """Reuse the existing execution lifecycle with an explicit immutable subset."""
    return execute(
        confirmed_execution_request(
            intent.confirmation(),
            receipt=receipt,
            home=intent.home,
            helper=helper,
        ),
        progress=progress,
    )


def _preview_directory(home: Path) -> tempfile.TemporaryDirectory[str]:
    temporary = Path(tempfile.gettempdir()).resolve()
    if temporary == home or home in temporary.parents:
        raise ValueError("Preview temporary storage must be outside the selected home.")
    return tempfile.TemporaryDirectory(prefix="codex-session-gc-preview-", dir=temporary)


def _preview_result(path: Path) -> dict[str, Any]:
    result_path = path / "result.json"
    if not result_path.exists():
        raise ValueError("Read-only preview worker exited without a result.")
    result: dict[str, Any] = json.loads(result_path.read_text(encoding="utf-8"))
    if "error" in result:
        raise ValueError(result["error"])
    return result


def _read_worker_timing(path: Path, observer: TimingObserver) -> None:
    """Discard malformed optional observations without changing the business result."""
    try:
        with (path / "timings.json").open("rb") as stream:
            raw = stream.read(_TIMING_BYTES + 1)
        if len(raw) <= _TIMING_BYTES and observer.accept_worker(json.loads(raw)):
            signal_worker_summary()
    except Exception:  # noqa: BLE001, S110
        pass


async def async_preview(
    intent: Intent, *, mode: PreviewMode = PreviewMode.OBSERVE
) -> dict[str, Any]:
    """Own one read-only process without global signals, including through cancellation.

    Cancellation shields spawn and cleanup. The worker gets five seconds to exit
    after termination, then is killed and reaped before worker output is removed.
    This is a cleanup grace period, not a kernel completion-time guarantee.
    """
    directory = _preview_directory(intent.home)
    path = Path(directory.name)
    process: asyncio.subprocess.Process | None = None
    launch: asyncio.Task[asyncio.subprocess.Process] | None = None
    interrupted = False
    observer = current()

    async def cleanup() -> None:
        nonlocal process
        try:
            if process is None and launch is not None:
                process = await launch
            if process is not None:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                await process.wait()
        finally:
            directory.cleanup()

    try:
        (path / "input.json").write_text(
            json.dumps({**intent.payload(), "mode": mode.value, "timings": observer is not None}),
            encoding="utf-8",
        )
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                "-m",
                __name__,
                str(path),
                env={**os.environ, "TMPDIR": str(path), "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        with interval(Phase.PREVIEW_WORKER_WAIT):
            process = await asyncio.shield(launch)
            await process.wait()
        try:
            result = _preview_result(path)
        finally:
            if observer is not None:
                _read_worker_timing(path, observer)
        return result
    finally:
        with interval(Phase.WORKER_CLEANUP):
            finishing = asyncio.create_task(cleanup())
            while not finishing.done():
                try:
                    await asyncio.shield(finishing)
                except asyncio.CancelledError:
                    interrupted = True
            await finishing
        if interrupted:
            raise asyncio.CancelledError


def _worker(directory: Path) -> None:
    data = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    observer = TimingObserver("preview") if data.get("timings") else None
    try:
        intent = Intent(
            Path(data["home"]),
            parse_cutoff(data["cutoff"])
            if data["cutoff"] is not None
            else ExactIds(frozenset(ThreadId.parse(x) for x in data["selected"])),
            tuple(ThreadId.parse(x) for x in data["roots"]),
            tuple(ThreadId.parse(x) for x in data["selected"]),
        )
        if observer is None:
            result = preview(intent, mode=PreviewMode(data["mode"]))
        else:
            with observe(observer):
                result = preview(intent, mode=PreviewMode(data["mode"]))
    except Exception as error:  # noqa: BLE001
        result = {"error": str(error)}
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if observer is not None:
        try:
            observer.finish(interrupted="error" in result)
            (directory / "timings.json").write_text(
                json.dumps(observer.snapshot()), encoding="utf-8"
            )
        except OSError, TypeError, ValueError:
            pass


if __name__ == "__main__":
    _worker(Path(sys.argv[1]))
