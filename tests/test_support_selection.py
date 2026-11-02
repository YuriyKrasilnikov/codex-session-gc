# Copyright (c) 2026

"""Explicit support selection preserves separate local trust observations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from codex_session_gc import diagnostics
from codex_session_gc.artifacts import ArtifactIdentity
from codex_session_gc.compatibility.observation import (
    ImportedSupport,
    RecordAvailability,
    RecordOrigin,
    observe_unavailable_record,
)
from codex_session_gc.compatibility.record import BindingState
from codex_session_gc.compatibility.store import promote_support
from codex_session_gc.installation.activation import _digest
from codex_session_gc.installation.model import InstallLayout
from codex_session_gc.installation.provenance import PREDICATE_TYPE, verify_artifact
from codex_session_gc.installation.read_budget import ReadBudget
from codex_session_gc.installation.transaction import InstallationError, _read_receipt
from tests.test_support_intake import _fixture

pytest_plugins = ["tests.test_doctor"]


def test_import_selection_requires_exact_content_and_context_ids(tmp_path: Path) -> None:
    """A selected record and its verification context both need exact identities."""
    layout = InstallLayout(tmp_path)
    identifier = uuid4()
    for record, context in (("a" * 64, "short"), ("A" * 64, "b" * 64)):
        with pytest.raises(ValueError, match="SHA-256"):
            ImportedSupport(layout, identifier, record, context)


def test_unavailable_import_remains_selected_and_non_authorizing(tmp_path: Path) -> None:
    """Unavailable saved evidence cannot silently become a bundled support claim."""
    selection = ImportedSupport(InstallLayout(tmp_path), uuid4(), "a" * 64, "b" * 64)
    observation = observe_unavailable_record(selection, profile_raw=None)
    if (
        observation.origin is not RecordOrigin.CALLER
        or observation.availability is not RecordAvailability.UNKNOWN
        or observation.record_sha256 != selection.record_sha256
        or observation.record is not None
        or observation.claim_verified
        or observation.authorizes_operation
    ):
        pytest.fail("The unavailable explicit record was promoted or substituted")


def _check_current_binding(report: diagnostics.DoctorReport) -> None:
    support = report.source_support
    if support.availability is not RecordAvailability.AVAILABLE or support.bindings is None:
        pytest.fail("Stored support was not observed")
    if support.origin is not RecordOrigin.CALLER or support.claim_verified:
        pytest.fail("Stored support was relabeled as current cryptographic verification")
    if not report.recorded_verification or report.recorded_verification.get("origin") != (
        "verification_recorded_not_reauthenticated"
    ):
        pytest.fail("Recorded verification context was not reported separately")
    if support.authorizes_operation:
        pytest.fail("Support observations cannot authorize cleanup")
    if support.record is None:
        pytest.fail("Stored support record is unavailable")
    checks = {check.field: check.state for check in support.bindings.checks}
    expected_helper = (
        BindingState.MATCH
        if support.record.execution.helper_sha256 == report.helper.sha256
        else BindingState.MISMATCH
    )
    expected_profile = (
        BindingState.MATCH
        if support.record.execution.profile_sha256 == report.profile.sha256
        else BindingState.MISMATCH
    )
    if checks["helper"] is not expected_helper or checks["profile"] is not expected_profile:
        pytest.fail("Support binding did not use the current helper and profile observations")


def test_imported_diagnosis_compares_the_current_profile_and_helper(
    tmp_path: Path, environment: tuple[Path, Path]
) -> None:
    """A stored claim observes current identities while keeping saved origin separate."""
    home, helper = environment
    layout, identifier, artifact, identity, source, verifier = _fixture(tmp_path)

    async def promote() -> ImportedSupport:
        with await verify_artifact(artifact, identity, source, trusted_gh=verifier) as lease:
            receipt = promote_support(layout, identifier, lease, 123)
        return ImportedSupport(layout, identifier, identity.sha256, receipt.context_sha256)

    selection = asyncio.run(promote())
    report = diagnostics.inspect_selected_compatibility(home, helper, selection)
    _check_current_binding(report)
    selected = selection.layout.base / "support" / str(selection.installation_id)
    (selected / f"{selection.record_sha256}.json").unlink()
    missing = diagnostics.inspect_selected_compatibility(home, helper, selection)
    if missing.source_support.availability is not RecordAvailability.UNKNOWN:
        pytest.fail("Missing selected support should remain unavailable")
    if missing.source_support.record_sha256 != identity.sha256 or missing.recorded_verification:
        pytest.fail("Missing selected support was substituted or promoted")
    if (missing.profile, missing.helper, missing.databases) != (
        report.profile,
        report.helper,
        report.databases,
    ):
        pytest.fail("Unavailable support changed the local diagnostic observations")


def test_receipt_growth_during_bounded_read_refuses_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt growing after its size check cannot escape the file bound."""
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b"{}")
    original = os.fstat
    changed = False

    def growing(fd: int) -> os.stat_result:
        nonlocal changed
        result = original(fd)
        if not changed and Path(f"/proc/self/fd/{fd}").resolve() == receipt:
            changed = True
            with receipt.open("ab") as stream:
                stream.write(b" " * 128)
        return result

    monkeypatch.setattr(os, "fstat", growing)
    with pytest.raises(InstallationError):
        _read_receipt(receipt, ReadBudget(16, time.monotonic() + 5))
    if not changed:
        pytest.fail("Receipt growth was not exercised")
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            if descriptor.resolve() == receipt:
                pytest.fail("Refused receipt descriptor remained open")
        except FileNotFoundError:
            continue


def test_receipt_budget_rejects_tiny_limit_and_expired_deadline(tmp_path: Path) -> None:
    """Receipt reads honor the caller's per-file size and shared deadline."""
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b'{"value": 1}')
    for budget in (ReadBudget(1, time.monotonic() + 5), ReadBudget(1024, time.monotonic() - 1)):
        with pytest.raises(InstallationError):
            _read_receipt(receipt, budget)


def test_two_imports_compare_same_current_helper_on_each_selection(
    tmp_path: Path, environment: tuple[Path, Path]
) -> None:
    """Selecting a newer record does not replace the running helper observation."""
    home, helper = environment
    layout, identifier, artifact, first_identity, source, verifier = _fixture(tmp_path)

    async def promote(identity: ArtifactIdentity, asset_id: int) -> ImportedSupport:
        with await verify_artifact(artifact, identity, source, trusted_gh=verifier) as lease:
            receipt = promote_support(layout, identifier, lease, asset_id)
        return ImportedSupport(layout, identifier, identity.sha256, receipt.context_sha256)

    first = asyncio.run(promote(first_identity, 123))
    record = json.loads(artifact.read_bytes())
    record["execution"]["helper_sha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()
    record["report_sha256"] = "9" * 64
    updated = json.dumps(record).encode()
    artifact.write_bytes(updated)
    second_identity = ArtifactIdentity(hashlib.sha256(updated).hexdigest(), len(updated))
    proof = [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": PREDICATE_TYPE,
                    "subject": [{"digest": {"sha256": second_identity.sha256}}],
                }
            }
        }
    ]
    verifier.write_text(f"#!{sys.executable}\nprint({json.dumps(proof)!r})\n")
    second = asyncio.run(promote(second_identity, 124))
    reports = [
        diagnostics.inspect_selected_compatibility(home, helper, selection)
        for selection in (first, second)
    ]
    if reports[0].helper.sha256 != reports[1].helper.sha256:
        pytest.fail("Changing support selection changed the observed helper")
    if reports[0].profile.sha256 != reports[1].profile.sha256:
        pytest.fail("Changing support selection changed the observed profile")
    for report, identity in zip(reports, (first_identity, second_identity), strict=True):
        if report.source_support.record_sha256 != identity.sha256:
            pytest.fail("Diagnosis did not use the explicitly selected record")
        _check_current_binding(report)
    checks = reports[1].source_support.bindings
    if checks is None or next(x.state for x in checks.checks if x.field == "helper") is not (
        BindingState.MATCH
    ):
        pytest.fail("Updated support record did not compare with the current helper")


def test_read_budget_checks_long_file_during_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long file read stops at a cooperative deadline rather than finishing unchecked."""
    target = tmp_path / "large"
    target.write_bytes(b"x" * (2 * 1024 * 1024))
    original = os.pread

    def delayed(fd: int, size: int, offset: int) -> bytes:
        time.sleep(0.02)
        return original(fd, size, offset)

    monkeypatch.setattr(os, "pread", delayed)
    with target.open("rb") as stream, pytest.raises(ValueError, match="deadline"):
        _digest(stream.fileno(), ReadBudget(3 * 1024 * 1024, time.monotonic() + 0.01))
