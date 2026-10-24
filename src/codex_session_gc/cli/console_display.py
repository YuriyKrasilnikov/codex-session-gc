# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Safe terminal formatting, qualified helper discovery and receipt allocation."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.execution.selection_provenance import reviewed_associations


def clean(text: object) -> str:
    """Keep untrusted titles, paths and diagnostics from controlling the terminal."""
    text = str(text)
    if text.isprintable():
        return text
    return "".join(
        c if c.isprintable() and not unicodedata.category(c).startswith("C") else "?" for c in text
    )


def discover_helper() -> Path | None:
    """Find an exact qualified regular executable without launching any candidate."""
    expected = load_closed_work_contract().binary_sha256
    repository = Path(__file__).resolve().parents[3]
    candidates = [repository / "native/build/debug/codex-session-gc-native"]
    distribution = repository / "dist"
    if distribution.is_dir():
        candidates = sorted(distribution.glob("*/codex-session-gc-native")) + candidates
    for name in ("codex-session-gc-native", "gc-native"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        try:
            named = candidate.lstat()
            if not stat.S_ISREG(named.st_mode) or not os.access(candidate, os.X_OK):
                continue
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                initial = os.fstat(source.fileno())
                if not stat.S_ISREG(initial.st_mode) or initial.st_size > 512 * 1024**2:
                    continue
                digest = hashlib.file_digest(source, "sha256").digest()
                final = os.fstat(source.fileno())

            def identity(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if digest == expected and identity(initial) == identity(final) == identity(
                candidate.lstat()
            ):
                return candidate.resolve()
        except OSError:
            continue
    return None


def receipt_path(directory: Path, home: Path) -> Path:
    """Allocate a unique durable path outside the selected source namespace."""
    directory = directory.expanduser().resolve()
    if directory == home or home in directory.parents:
        raise ValueError("Receipt directory must be outside the selected home.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / f"cleanup-{uuid.uuid4()}.sqlite"


def preview_lines(report: dict[str, Any]) -> list[str]:
    """Expose the exact consequential declaration and every retained selection."""
    sizes = report["sizes"]
    labels = report.get("labels", {})
    associations = reviewed_associations(report["guardian_associations"])
    return [
        "CONDITIONAL PREVIEW - not proof that work is closed or deletable",
        f"Home: {report['home']}",
        f"UTC upper boundary: {report['cutoff']}",
        f"Deletion candidates: {len(report['deletions'])}; retained: {len(report['retained'])}",
        (
            f"Observed stored candidate bytes: {sum(x for x in sizes if x is not None)}; "
            f"unknown sizes: {sum(x is None for x in sizes)} (not guaranteed reclaim)"
        ),
        (
            f"Shared retained provider bytes: {report['shared_retained_bytes']}; "
            f"conditional stored reclaim estimate: {report['conditional_reclaim_bytes']} "
            "(not guaranteed disk reclaim)"
        ),
        "Physical histories (exact saved representations):",
        *[
            (
                f"  {target['thread_id']}: {physical['rollout_id']} "
                f"{physical['representation']} {physical['history_mode']} "
                f"{physical['physical_identity']['size']} stored bytes"
            )
            for target in report["physical_manifest"]
            for physical in target["rollouts"]
        ],
        "Work roots:",
        *[f"  {x} | {labels.get(x, '(description unavailable)')}" for x in report["roots"]],
        "Exact deletion IDs:",
        *[f"  {x} | {labels.get(x, '(description unavailable)')}" for x in report["deletions"]],
        *(
            ["Guardian historical parent associations:"]
            + [
                f"  {item.guardian_thread_id.value}: associated parent "
                f"{item.associated_parent_thread_id.value}"
                for item in associations
            ]
            if associations
            else []
        ),
        "Retained selections:",
        *[f"  {x['id']}: {x['reason']}" for x in report["retained"]],
        *[f"ERROR: {x}" for x in report["errors"]],
        "Native ownership and fresh checks may refuse or narrow this selection.",
    ]


def result_text(report: dict[str, Any]) -> str:
    """Keep actionable per-work outcomes and reasons visible after terminal teardown."""
    lines = [f"Outcome: {clean(report['outcome'])}"]
    if report.get("detail"):
        lines.append(clean(report["detail"]))
    lines.extend(f"Selection error: {clean(error)}" for error in report.get("selectionErrors", []))
    for row in report.get("roots", []):
        status = row.get("status", "not_dispatched")
        if status == "not_dispatched":
            status = "unattempted (not dispatched)"
        lines.append(
            f"Work {clean(row['root'])}: {clean(status)}; "
            f"native={clean(row.get('originalNativeOutcome'))}; "
            f"final observed={clean(row.get('finalObservedState'))}"
        )
        if row.get("detail"):
            lines.append(f"  {clean(row['detail'])}")
        native = row.get("nativeReceipt")
        if isinstance(native, dict):
            lines.append(
                f"  Native status: {clean(native.get('status', 'unknown'))}; "
                f"{clean(native.get('detail', ''))}"
            )
        lines.extend(
            f"  Selection error: {clean(error)}" for error in row.get("selectionErrors", [])
        )
        lines.extend(
            f"  Retained {clean(item['threadId'])}: {clean(item['reason'])}"
            for item in row.get("retained", [])
        )
    if report["outcome"] in {"unknown", "partial"}:
        lines.append("Keep the receipt and reconcile; do not retry automatically.")
    return "\n".join(lines)
