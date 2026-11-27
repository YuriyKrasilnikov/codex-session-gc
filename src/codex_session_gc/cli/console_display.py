# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Safe terminal formatting, qualified helper discovery and receipt allocation."""

from __future__ import annotations

import hashlib
import os
import shlex
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


def _plan_command(report: dict[str, Any], root: str, thread: str) -> str:
    arguments = [
        "codex-session-gc",
        "plan",
        "--online",
        "--home",
        report["home"],
        "--closed-work",
        root,
        "--select-thread",
        thread,
    ]
    if report["cutoff"] is not None:
        arguments.extend(("--cutoff", report["cutoff"]))
    return shlex.join(arguments)


def attachment_plan_lines(report: dict[str, Any]) -> list[str]:
    """Render read-only navigation for separate CLI invocations."""
    guidance = report.get("attachment_cleanup")
    identity = report.get("home_identity")
    output = []
    if identity is not None:
        output.append(f"Observed home device/inode: {identity[0]}/{identity[1]}")
    for child, source in report.get("selected_attachments", {}).items():
        output.append(f"Selected attachment {child} currently points at source {source}.")
    if not guidance or not (guidance["sources"] or guidance["truncated"]):
        return output
    output.append("Attached histories holding selected sources:")
    commands_available = report["home"].isprintable()
    if not commands_available:
        output.append("Preview commands are unavailable for a home path with control characters.")
    for source in guidance["sources"]:
        output.append(f"  Source {source['id']}")
        for child in source["children"]:
            membership = "selected" if child["selected"] else "unselected"
            root = child["root_id"]
            output.append(
                f"    Attachment {child['id']} ({membership}), "
                f"work root {root if root is not None else 'unavailable'}"
            )
            if commands_available and root is not None:
                output.append(f"    Review attachment: {_plan_command(report, root, child['id'])}")
        root = source["root_id"]
        if commands_available and root is not None:
            output.append(
                "    After confirmed attachment removal, preview source: "
                + _plan_command(report, root, source["id"])
            )
    if guidance["truncated"]:
        output.append("Additional observed attachments are omitted from this report.")
    output.extend(
        [
            "Use the first attempt's receipt to check the result before reviewing the source.",
            (
                "Each command reads current storage independently. Compare home device/inode "
                "and review the work roots and current attachment sources before a separate apply."
            ),
        ]
    )
    return output


def _selection_lines(summary: dict[str, Any] | None) -> list[str]:
    if summary is None:
        return []
    total = summary["stored_bytes"]
    size = (
        f"{total} stored bytes"
        if total is not None
        else f"at least {summary['known_stored_bytes']} stored bytes; "
        f"{summary['incomplete_history_count']} histories incomplete"
    )
    visible, hidden = summary["visible"], summary["hidden"]
    visibility = (
        f"visible: {visible}; hidden: {hidden}"
        if visible is not None and hidden is not None
        else "visible and hidden counts unavailable"
    )
    return [
        (
            f"Selected histories: {summary['history_count']}; {size}; {visibility}; "
            f"missing: {summary['missing']}"
        )
    ]


def preview_lines(report: dict[str, Any]) -> list[str]:
    """Expose the exact consequential declaration and every retained selection."""
    sizes = report["sizes"]
    labels = report.get("labels", {})
    associations = reviewed_associations(report["guardian_associations"])
    return [
        "CONDITIONAL PREVIEW - not proof that work is closed or deletable",
        *_selection_lines(report.get("selection_summary")),
        f"Home: {report['home']}",
        (
            f"UTC upper boundary: {report['cutoff']}"
            if report["cutoff"] is not None
            else "Selection policy: exact IDs (no date filter)"
        ),
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
        *[
            f"  {item['id']}: {item['reason']}"
            + (f"; witness: {item['witness']}" if item.get("witness") else "")
            for item in report["retained"]
        ],
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
    effects = report.get("effects")
    if isinstance(effects, dict):
        count = effects["recorded_removed_rollout_count"]
        size = effects["recorded_removed_logical_bytes"]
        recorded = (
            f"{count} files; their logical volume: {size} bytes"
            if count is not None and size is not None
            else "unavailable"
        )
        lines.append(
            f"Recorded removed files: {recorded} "
            f"({effects['completeness']}; partial units: {effects['partial_unit_count']}; "
            f"unavailable units: {effects['unavailable_unit_count']})"
        )
        reviewed = report.get("reviewed_logical_bytes")
        lines.append(
            f"Reviewed logical bytes: {reviewed if reviewed is not None else 'unavailable'}"
        )
        lines.append(
            f"Observed at: {report.get('observed_at')}; "
            f"recommended action: {report.get('recommended_action')}"
        )
    elif report.get("recommended_action"):
        lines.append(f"Recommended action: {clean(report['recommended_action'])}")
    if report.get("recommended_action") == "reconcile" or (
        "recommended_action" not in report and report["outcome"] in {"unknown", "partial"}
    ):
        lines.append("Keep the receipt and reconcile; do not retry automatically.")
    return "\n".join(lines)
