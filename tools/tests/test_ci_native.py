# Copyright (c) 2026

"""Source-qualified component selection without legacy release anchors."""

from __future__ import annotations

import hashlib
import json
import sys
from typing import TYPE_CHECKING

import pytest

from tools import ci_native, native_candidate

if TYPE_CHECKING:
    from pathlib import Path

pytest_plugins = ["tools.tests.test_native_candidate"]


def _equal(actual: object, expected: object) -> None:
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, observed {actual!r}")


def test_current_component_selects_only_qualified_helper(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """A matching component selects the exact helper and fixed-repository locator."""
    root, component, helper, _ = candidate
    qualification, transport = ci_native.describe(root)
    output = tmp_path / "selected-helper"
    evidence = ci_native.select_helper(root, component, output)
    qualification_evidence = evidence["qualification"]
    transport_evidence = evidence["transport"]
    if not isinstance(qualification_evidence, dict) or not isinstance(transport_evidence, dict):
        pytest.fail("Native component evidence is missing its identity objects")
    _equal(output.read_bytes(), helper.read_bytes())
    _equal(qualification_evidence["qualification_identity"], qualification.qualification_identity)
    _equal(transport_evidence["asset_id"], transport.asset_id)
    _equal(
        transport.api_url,
        "https://api.github.com/repos/YuriyKrasilnikov/codex-session-gc/releases/assets/123",
    )


def test_source_check_does_not_require_or_interpret_transport(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """Material validation remains available before a locator exists and selection refuses."""
    root, component, _, _ = candidate
    path = root / ci_native.PROVENANCE
    provenance = json.loads(path.read_bytes())
    native = provenance["build_qualification"]["native_candidate"]
    del native["transport"]
    path.write_text(json.dumps(provenance))
    _equal(ci_native.verify_source(root)["qualification_identity"], "e" * 64)
    with pytest.raises(ci_native.ReuseError, match="transport is not configured"):
        ci_native.describe(root)
    with pytest.raises(native_candidate.CandidateError, match="transport is not configured"):
        ci_native.select_helper(root, component, tmp_path / "selected-output")
    _equal((tmp_path / "selected-output").exists(), expected=False)
    native["transport"] = {"asset_id": None, "sha256": "a" * 64, "bytes": 123}
    path.write_text(json.dumps(provenance))
    _equal(ci_native.verify_source(root)["qualification_identity"], "e" * 64)
    with pytest.raises(native_candidate.CandidateError):
        ci_native.describe(root)
    with pytest.raises(native_candidate.CandidateError):
        ci_native.select_helper(root, component, tmp_path / "unbound-output")
    _equal((tmp_path / "unbound-output").exists(), expected=False)
    native["transport"] = {"asset_id": 0, "sha256": "invalid", "bytes": 0}
    path.write_text(json.dumps(provenance))
    _equal(ci_native.verify_source(root)["qualification_identity"], "e" * 64)
    with pytest.raises(native_candidate.CandidateError):
        ci_native.describe(root)


@pytest.mark.parametrize(
    "name",
    [
        "native/helper/Cargo.toml",
        "native/helper/Cargo.lock",
        "native/helper/rust-toolchain.toml",
        "native/helper/src/main.rs",
        "native/patches/test.patch",
    ],
)
def test_each_changed_native_input_refuses(
    candidate: tuple[Path, Path, Path, Path], name: str
) -> None:
    """A modified native build input cannot inherit the source qualification."""
    root, _, _, _ = candidate
    (root / name).write_bytes(b"changed")
    with pytest.raises(native_candidate.CandidateError, match="Native source does not match"):
        ci_native.verify_source(root)


@pytest.mark.parametrize("name", ["native/helper/build.rs", "native/helper/src/extra.rs"])
def test_extra_native_input_refuses(candidate: tuple[Path, Path, Path, Path], name: str) -> None:
    """An unlisted native build script or source is not silently accepted."""
    root, _, _, _ = candidate
    (root / name).write_text("new source")
    with pytest.raises(native_candidate.CandidateError, match="Native source does not match"):
        ci_native.verify_source(root)


def test_raw_profile_change_refuses(candidate: tuple[Path, Path, Path, Path]) -> None:
    """Even equivalent edited profile bytes do not match the qualified raw identity."""
    root, _, _, _ = candidate
    path = root / ci_native.PROFILE
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(native_candidate.CandidateError, match="Native source does not match"):
        ci_native.verify_source(root)


def test_component_and_output_mutation_refuse(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """Changed component bytes and preexisting helper paths are never selected."""
    root, component, _, _ = candidate
    existing = tmp_path / "existing-helper"
    existing.write_bytes(b"keep")
    with pytest.raises(native_candidate.CandidateError):
        ci_native.select_helper(root, component, existing)
    _equal(existing.read_bytes(), b"keep")
    component.write_bytes(b"changed")
    with pytest.raises(native_candidate.CandidateError):
        ci_native.select_helper(root, component, tmp_path / "new-helper")
    _equal((tmp_path / "new-helper").exists(), expected=False)


def test_rewritten_current_manifest_cannot_replace_profile_bound_source(
    candidate: tuple[Path, Path, Path, Path],
) -> None:
    """A self-consistent replacement manifest cannot change profile-qualified inputs."""
    root, _, _, _ = candidate
    source = root / "native/helper/src/main.rs"
    source.write_text("unqualified replacement")
    path = root / ci_native.PROVENANCE
    provenance = json.loads(path.read_bytes())
    current = provenance["current_delivery_inputs"]
    manifest = current["manifest"]
    entry = next(item for item in manifest["inputs"] if item["path"] == "native/helper/src/main.rs")
    entry.update(bytes=source.stat().st_size, sha256=ci_native.digest(source))
    current["manifest_sha256"] = hashlib.sha256(
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    ).hexdigest()
    path.write_text(json.dumps(provenance))
    with pytest.raises(native_candidate.CandidateError, match="Profile material identities differ"):
        ci_native.verify_source(root)


def test_rewritten_qualification_partitions_cannot_replace_profile_evidence(
    candidate: tuple[Path, Path, Path, Path],
) -> None:
    """A matching rewritten public summary cannot replace original profile-bound checks."""
    root, _, _, summary = candidate
    profile_digest = ci_native.digest(root / ci_native.PROFILE)
    path = root / ci_native.PROVENANCE
    provenance = json.loads(path.read_bytes())
    build = provenance["build_qualification"]
    build["partitions"][0]["cases"] += 1
    checks = [
        hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for item in build["partitions"]
    ]
    public_summary = json.loads(summary.read_bytes())
    public_summary["checks"] = checks
    summary.write_text(json.dumps(public_summary))
    build["native_candidate"]["summary_sha256"] = ci_native.digest(summary)
    path.write_text(json.dumps(provenance))
    _equal(ci_native.digest(root / ci_native.PROFILE), profile_digest)
    with pytest.raises(
        native_candidate.CandidateError, match="Profile qualification checks differ"
    ):
        ci_native.verify_source(root)


def test_cli_has_no_source_or_digest_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller flags cannot replace the checkout's qualified material authority."""
    monkeypatch.setattr(sys, "argv", ["ci_native", "--root", "/alternate/source"])
    with pytest.raises(SystemExit) as failure:
        ci_native.main()
    _equal(failure.value.code, 2)
