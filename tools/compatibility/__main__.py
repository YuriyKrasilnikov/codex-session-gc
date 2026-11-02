# Copyright (c) 2026

"""Compare local Git objects and report conservative compatibility evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from tools.compatibility.cargo import CargoLimits
from tools.compatibility.decision import (
    AssessmentError,
    Decision,
    Impact,
    encode_report,
    evaluate,
    read_assessment,
    report_sha256,
)
from tools.compatibility.report import ObservationLimits, Request, assess


def main() -> None:
    """Write a JSON report without changing the repository or qualification state."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", required=True, type=Path, help="Absolute path to trusted Git.")
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--baseline")
    parser.add_argument("--target")
    parser.add_argument("--boundaries", type=Path)
    parser.add_argument("--profile-sha256")
    parser.add_argument("--native-inputs-sha256")
    parser.add_argument("--helper-sha256")
    parser.add_argument("--assessment", type=Path, help="Supplied scoped review claim in JSON.")
    parser.add_argument("--workspace", help="Workspace Cargo.toml path inside each commit.")
    parser.add_argument(
        "--cargo-root", action="append", default=[], help="Root crate Cargo.toml path."
    )
    defaults = ObservationLimits()
    parser.add_argument("--max-cargo-nodes", type=int, default=defaults.cargo.nodes)
    parser.add_argument("--max-cargo-edges", type=int, default=defaults.cargo.edges)
    parser.add_argument("--max-cargo-reads", type=int, default=defaults.cargo.reads)
    parser.add_argument("--max-cargo-bytes", type=int, default=defaults.cargo.bytes)
    parser.add_argument("--cargo-seconds", type=float, default=defaults.cargo.seconds)
    parser.add_argument("--max-objects", type=int, default=defaults.objects)
    parser.add_argument("--max-object-bytes", type=int, default=defaults.object_bytes)
    parser.add_argument("--object-seconds", type=float, default=defaults.seconds)
    arguments = parser.parse_args()
    try:
        limits = ObservationLimits(
            CargoLimits(
                arguments.max_cargo_nodes,
                arguments.max_cargo_edges,
                arguments.max_cargo_reads,
                arguments.max_cargo_bytes,
                arguments.cargo_seconds,
            ),
            arguments.max_objects,
            arguments.max_object_bytes,
            arguments.object_seconds,
        )
    except ValueError as error:
        parser.error(str(error))
    request = Request(
        arguments.git,
        arguments.repository,
        arguments.baseline,
        arguments.target,
        arguments.boundaries,
        arguments.profile_sha256,
        arguments.native_inputs_sha256,
        arguments.workspace,
        tuple(arguments.cargo_root),
        arguments.helper_sha256,
        limits,
    )
    report = asyncio.run(assess(request))
    if arguments.assessment is None:
        sys.stdout.buffer.write(encode_report(report))
        return
    try:
        decision = evaluate(report, read_assessment(arguments.assessment))
    except AssessmentError, OSError:
        decision = Decision(
            Impact.UNKNOWN, report_sha256(report), ("assessment_unavailable_or_invalid",)
        )
    output = {
        "report": report,
        "report_sha256": decision.report_sha256,
        "decision": {
            "impact": decision.impact.value,
            "reasons": list(decision.reasons),
            "assessment_sha256": decision.assessment_sha256,
            "scope": decision.scope,
            "review": None if decision.review is None else asdict(decision.review),
            "review_origin": decision.review_origin,
            "review_claim_verified": decision.review_claim_verified,
            "execution_identity_verified": decision.execution_identity_verified,
            "authorizes_operation": decision.authorizes_operation,
        },
    }
    sys.stdout.write(json.dumps(output, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
