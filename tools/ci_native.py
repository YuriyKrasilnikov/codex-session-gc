# Copyright (c) 2026

"""Select only the source-qualified native material component for CI assembly."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from tools.native_candidate import SOURCE_PROFILE as PROFILE
from tools.native_candidate import SOURCE_PROVENANCE as PROVENANCE
from tools.native_candidate import (
    CandidateError,
    Qualification,
    ReuseError,
    Transport,
    digest,
    load_qualification,
    select_qualified_candidate,
    verify_material_source,
    verify_qualification_source,
)

__all__ = ("PROFILE", "PROVENANCE", "ReuseError", "digest", "verify_material_source")


def verify_source(root: Path) -> dict[str, object]:
    """Validate source-pinned qualified material without requiring a transport locator."""
    return asdict(verify_qualification_source(root))


def describe(root: Path) -> tuple[Qualification, Transport]:
    """Return the single configured fixed-repository asset after source validation."""
    qualified = verify_qualification_source(root)
    current, transport = load_qualification(root)
    if current != qualified:
        message = "Native qualification changed during description."
        raise ReuseError(message)
    if transport is None:
        message = "Native candidate transport is not configured."
        raise ReuseError(message)
    return qualified, transport


def select_helper(root: Path, component: Path, helper: Path) -> dict[str, object]:
    """Verify the pinned component and publish only its exact qualified helper."""
    qualified, selected = select_qualified_candidate(root, component, helper)
    return {"qualification": asdict(qualified), "transport": asdict(selected)}


def main() -> None:
    """Expose source checking and component selection without identity override flags."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--component", type=Path)
    parser.add_argument("--helper", type=Path)
    arguments = parser.parse_args()
    if (arguments.component is None) != (arguments.helper is None):
        parser.error("--component and --helper must be supplied together")
    if arguments.describe and arguments.component is not None:
        parser.error("--describe cannot be combined with component selection")
    root = Path(__file__).resolve().parents[1]
    try:
        if arguments.describe:
            qualified, transport = describe(root)
            result: dict[str, object] = {
                "qualification": asdict(qualified),
                "transport": {**asdict(transport), "api_url": transport.api_url},
            }
        elif arguments.component is not None and arguments.helper is not None:
            result = select_helper(root, arguments.component, arguments.helper)
        else:
            result = verify_source(root)
    except (CandidateError, ReuseError, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Qualified native material refused: {error}\n")
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
