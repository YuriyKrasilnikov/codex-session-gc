# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Thin CLI composition of qualified selected-work execution and saved-intent observation."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from codex_session_gc.execution.closed_work_operation import ExecutionRequest
from codex_session_gc.execution.closed_work_operation import execute as execute_operation

if TYPE_CHECKING:
    import argparse


def validate_arguments(arguments: argparse.Namespace) -> None:
    """Validate the supported command before observations or profile loading."""
    if not arguments.online:
        raise ValueError("Execution requires the supported --online protocol.")
    if arguments.command not in {"apply", "reconcile"}:
        raise ValueError("Unsupported execution command.")
    if arguments.command == "apply":
        _validate_apply(arguments)
    if not arguments.receipt.is_absolute():
        raise ValueError("Online execution requires an absolute receipt path.")
    if arguments.max_read_bytes is not None and arguments.max_read_bytes < 1:
        raise ValueError("--max-read-bytes must be positive.")
    if arguments.timeout_seconds is not None and (
        not math.isfinite(arguments.timeout_seconds) or arguments.timeout_seconds <= 0
    ):
        raise ValueError("--timeout-seconds must be positive and finite.")


def _validate_apply(arguments: argparse.Namespace) -> None:
    """Require the exact source, helper and work roots for one dispatch."""
    if any(getattr(arguments, name) is None for name in ("home", "cutoff", "helper")):
        raise ValueError("Online apply requires --home, --cutoff and --helper.")
    if not arguments.closed_work:
        raise ValueError("Online apply requires explicit --closed-work roots.")
    if any(not path.is_absolute() for path in (arguments.home, arguments.helper)):
        raise ValueError("Online execution requires absolute paths.")
    if not arguments.home.is_dir():
        raise ValueError("--home must name an existing directory.")
    if arguments.default_provider == "":
        raise ValueError("--default-provider must be nonempty.")


def execute(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Adapt validated CLI arguments to the shared typed operation."""
    return execute_operation(
        ExecutionRequest(
            command=arguments.command,
            receipt=arguments.receipt,
            home=getattr(arguments, "home", None),
            cutoff=getattr(arguments, "cutoff", None),
            closed_work=tuple(getattr(arguments, "closed_work", ()) or ()),
            select_thread=tuple(arguments.select_thread)
            if getattr(arguments, "select_thread", None) is not None
            else None,
            helper=getattr(arguments, "helper", None),
            default_provider=getattr(arguments, "default_provider", None),
            max_read_bytes=arguments.max_read_bytes,
            timeout_seconds=arguments.timeout_seconds,
        )
    )


def terminal(report: dict[str, Any]) -> str:
    """Show delivery and observed-state labels without claiming reclaimed bytes."""
    lines = [f"{report['kind']}: {report['outcome']}", f"receipt: {report['receipt']}"]
    if report.get("scope"):
        lines.append("scope: saved_journal_units; excludes roots skipped before receipt creation")
    if report.get("selectionErrors"):
        lines.append("selection errors: " + ", ".join(report["selectionErrors"]))
    if report.get("detail"):
        lines.append(str(report["detail"]))
    for row in report["roots"]:
        lines.append(
            f"work {row['root']}: {row['status']}; native={row['originalNativeOutcome']}; "
            f"final_observed={row['finalObservedState']}"
        )
        if row.get("selectionErrors"):
            lines.append("  selection errors: " + ", ".join(row["selectionErrors"]))
        lines.extend(
            f"  retained {retained['threadId']}: {retained['reason']}"
            for retained in row.get("retained", [])
        )
    return "\n".join(lines)
