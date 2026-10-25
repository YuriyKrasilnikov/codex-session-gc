# Copyright (c) 2026

"""Browse storage, preview explicit work and run one qualified cleanup protocol."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codex_session_gc import diagnostics
from codex_session_gc.cli import closed_work_execution
from codex_session_gc.cli.console_display import preview_lines
from codex_session_gc.cli.console_tasks import preview_selection
from codex_session_gc.domain import ThreadId, UtcCutoff
from codex_session_gc.execution.resource_policy import READ_BYTES, TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import Sequence

INVALID_INPUT_EXIT = 2
UNAVAILABLE_GRAPH_EXIT = 3


def _parse_cutoff(value: str) -> UtcCutoff:
    """Require a caller-selected UTC instant rather than an implicit age rule."""
    try:
        return UtcCutoff(datetime.fromisoformat(value))
    except ValueError as error:
        message = "must be an explicit UTC timestamp"
        raise argparse.ArgumentTypeError(message) from error


def _parse_thread(value: str) -> ThreadId:
    """Validate an explicitly selected logical thread UUID."""
    try:
        return ThreadId.parse(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _selection_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare the explicit work roots, age and optional exact subset."""
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--cutoff", type=_parse_cutoff, required=True)
    parser.add_argument("--closed-work", type=_parse_thread, action="append", required=True)
    parser.add_argument("--select-thread", type=_parse_thread, action="append")


def _execution_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare receipt identity and bounded current-protocol execution."""
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--max-read-bytes", type=int)
    parser.add_argument("--timeout-seconds", type=float)


def _parser() -> argparse.ArgumentParser:
    """Expose one supported cleanup route and its browsing adapters."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Observe helper and exact storage compatibility")
    doctor.add_argument("--home", type=Path, required=True)
    doctor.add_argument("--helper", type=Path)
    doctor.add_argument("--max-read-bytes", type=int, default=READ_BYTES)
    doctor.add_argument("--timeout-seconds", type=float, default=TIMEOUT_SECONDS)
    doctor.add_argument("--json", action="store_true")
    ui = commands.add_parser("ui", help="Browse and select histories with Textual")
    ui.add_argument("--home", type=Path)
    ui.add_argument("--helper", type=Path)
    ui.add_argument("--cutoff", type=_parse_cutoff)
    ui.add_argument("--receipt-dir", type=Path)
    mcp = commands.add_parser("mcp", help="Serve catalogue and confirmed cleanup tools")
    mcp.add_argument("--home", type=Path, required=True)
    mcp.add_argument("--helper", type=Path)
    mcp.add_argument("--receipt-dir", type=Path)
    plan = commands.add_parser("plan", help="Preview selected closed work without deletion")
    _selection_arguments(plan)
    apply = commands.add_parser("apply", help="Delete an explicit selection once")
    _selection_arguments(apply)
    _execution_arguments(apply)
    apply.add_argument("--helper", type=Path, required=True)
    apply.add_argument("--default-provider")
    reconcile = commands.add_parser("reconcile", help="Observe the saved attempt without replay")
    _execution_arguments(reconcile)
    for command in (plan, apply, reconcile):
        command.add_argument("--online", action="store_true", required=True)
        command.add_argument("--json", action="store_true")
    return parser


def _preview(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Use the same scoped preview as the terminal and MCP interfaces."""
    try:
        report = preview_selection(
            arguments.home.expanduser().resolve(),
            arguments.cutoff,
            frozenset(arguments.closed_work),
            frozenset(arguments.select_thread) if arguments.select_thread is not None else None,
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        return {
            "kind": "closed_work_selection",
            "errors": [str(error)],
            "conditional": True,
            "executable": False,
        }, UNAVAILABLE_GRAPH_EXIT
    return report, UNAVAILABLE_GRAPH_EXIT if report["errors"] else 0


def _print_preview(report: dict[str, Any]) -> str:
    """Display preparation errors or the exact conditional declaration."""
    if report["errors"]:
        return "\n".join(str(error) for error in report["errors"])
    return "\n".join(preview_lines(report))


def main(argv: Sequence[str] | None = None) -> int:
    """Run one current command; unsupported commands fail during argument parsing."""
    arguments = _parser().parse_args(argv)
    if arguments.command == "doctor":
        try:
            doctor_report = diagnostics.inspect_compatibility(
                arguments.home.expanduser().absolute(),
                arguments.helper.expanduser().absolute() if arguments.helper is not None else None,
                maximum_read_bytes=arguments.max_read_bytes,
                timeout_seconds=arguments.timeout_seconds,
            )
        except ValueError as error:
            _parser().error(str(error))
        output = (
            json.dumps(asdict(doctor_report), sort_keys=True)
            if arguments.json
            else diagnostics.terminal(doctor_report)
        )
        sys.stdout.write(output + "\n")
        return doctor_report.exit_code
    if arguments.command == "ui":
        from codex_session_gc.cli import textual_console  # noqa: PLC0415

        return textual_console.run(arguments)
    if arguments.command == "mcp":
        from codex_session_gc.cli import mcp_server  # noqa: PLC0415

        home = arguments.home.expanduser().resolve()
        if not home.is_dir():
            _parser().error("--home must name an existing directory")
        return mcp_server.run(home, arguments.helper, arguments.receipt_dir)
    if arguments.command == "plan":
        report, status = _preview(arguments)
        output = json.dumps(report, sort_keys=True) if arguments.json else _print_preview(report)
    else:
        try:
            closed_work_execution.validate_arguments(arguments)
        except ValueError as error:
            _parser().error(str(error))
        report, status = closed_work_execution.execute(arguments)
        output = (
            json.dumps(report, sort_keys=True)
            if arguments.json
            else closed_work_execution.terminal(report)
        )
    sys.stdout.write(output + "\n")
    return status
