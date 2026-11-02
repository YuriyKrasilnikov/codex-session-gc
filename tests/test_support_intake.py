# Copyright (c) 2026

"""Exercise content-addressed support intake with isolated installation fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity
from codex_session_gc.compatibility import intake
from codex_session_gc.compatibility.store import SupportStoreError, promote_support, read_support
from codex_session_gc.installation import provenance
from tests.test_installation_activation import _setup
from tools.support_record import prepare_support_record

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from codex_session_gc.installation.model import InstallLayout


def _fixture(
    tmp_path: Path,
) -> tuple[InstallLayout, UUID, Path, ArtifactIdentity, SourceIdentity, Path]:
    layout, identifier, _, _ = _setup(tmp_path)
    raw = Path("src/codex_session_gc/compatibility/support.json").read_bytes()
    artifact = tmp_path / "support.json"
    artifact.write_bytes(raw)
    identity = ArtifactIdentity(hashlib.sha256(raw).hexdigest(), len(raw))
    source = SourceIdentity("a" * 40, "refs/heads/main")
    verifier = tmp_path / "gh-support"
    proof = [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": provenance.PREDICATE_TYPE,
                    "subject": [{"digest": {"sha256": identity.sha256}}],
                }
            }
        }
    ]
    verifier.write_text(f"#!{sys.executable}\nimport json\nprint({json.dumps(proof)!r})\n")
    verifier.chmod(0o700)
    return layout, identifier, artifact, identity, source, verifier


def test_promoted_record_keeps_exact_bytes_and_separate_recorded_origin(tmp_path: Path) -> None:
    """A verified lease publishes exact content with an explicitly non-authorizing receipt."""
    layout, identifier, artifact, identity, source, verifier = _fixture(tmp_path)

    async def run() -> None:
        lease = await provenance.verify_artifact(artifact, identity, source, trusted_gh=verifier)
        with lease:
            receipt = promote_support(layout, identifier, lease, 123)
        saved = read_support(layout, identifier, identity.sha256, receipt.context_sha256)
        if saved.raw != artifact.read_bytes() or saved.receipt != receipt:
            pytest.fail("The stored record or verification context changed")
        if saved.receipt.claim_verified or saved.receipt.authorizes_operation:
            pytest.fail("A recorded origin must never authorize an operation")
        if saved.receipt.origin != "verification_recorded_not_reauthenticated":
            pytest.fail("Offline origin was incorrectly represented as reauthenticated")

    asyncio.run(run())


def test_missing_receipt_and_tampered_content_refuse(tmp_path: Path) -> None:
    """An incomplete or modified saved record cannot be observed as an intake."""
    layout, identifier, artifact, identity, source, verifier = _fixture(tmp_path)

    async def run() -> None:
        lease = await provenance.verify_artifact(artifact, identity, source, trusted_gh=verifier)
        with lease:
            receipt = promote_support(layout, identifier, lease, 123)
        directory = layout.base / "support" / str(identifier)
        record = directory / f"{identity.sha256}.json"
        record.write_bytes(b"tampered")
        with pytest.raises(SupportStoreError):
            read_support(layout, identifier, identity.sha256, receipt.context_sha256)
        record.write_bytes(artifact.read_bytes())
        (directory / f"{identity.sha256}.{receipt.context_sha256}.receipt.json").unlink()
        with pytest.raises(FileNotFoundError):
            read_support(layout, identifier, identity.sha256, receipt.context_sha256)

    asyncio.run(run())


def test_signing_subject_preserves_exact_source_bytes(tmp_path: Path) -> None:
    """The support-only assembly signs the source record without wrapping or rewriting it."""
    output = tmp_path / "support-subject.json"
    digest, size = prepare_support_record(output)
    raw = Path("src/codex_session_gc/compatibility/support.json").read_bytes()
    if output.read_bytes() != raw or digest != hashlib.sha256(raw).hexdigest() or size != len(raw):
        pytest.fail("Support signing subject changed source bytes")
    with pytest.raises(FileExistsError):
        prepare_support_record(output)


def test_explicit_intake_checks_metadata_before_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong asset identity is refused before its body is requested."""
    layout, identifier, artifact, identity, source, verifier = _fixture(tmp_path)
    url = f"https://github.com/{provenance.REPOSITORY}/releases/download/v1/support.json"
    metadata = {
        "id": 123,
        "state": "uploaded",
        "size": identity.size,
        "digest": f"sha256:{identity.sha256}",
        "browser_download_url": url,
    }

    class Transport:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get(
            self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
        ) -> None:
            if deadline <= 0:
                pytest.fail("Missing transfer deadline")
            self.calls.append(url)
            body = (
                json.dumps(metadata).encode() if "api.github.com" in url else artifact.read_bytes()
            )
            if len(body) > limit:
                pytest.fail("Fixture body exceeded the requested bound")
            sink(body)

    async def ready(gh: Path, directory: Path, limits: object) -> None:
        pass

    monkeypatch.setattr(intake, "preflight_gh", ready)
    request = intake.SupportIntakeRequest(layout, identifier, 123, identity, source, verifier, 10)

    async def run() -> None:
        transport = Transport()
        receipt = await intake.intake_support(request, transport)
        saved = read_support(layout, identifier, identity.sha256, receipt.context_sha256)
        if saved.raw != artifact.read_bytes():
            pytest.fail("Intake did not retain exact bytes")
        metadata["digest"] = "sha256:" + "0" * 64
        rejected = Transport()
        with pytest.raises(SupportStoreError):
            await intake.intake_support(request, rejected)
        if len(rejected.calls) != 1 or "api.github.com" not in rejected.calls[0]:
            pytest.fail("Invalid asset was downloaded")

    asyncio.run(run())


@pytest.mark.parametrize("seconds", [0.0, float("nan"), float("inf")])
def test_invalid_verifier_deadline_refuses_before_metadata(tmp_path: Path, seconds: float) -> None:
    """Non-finite and nonpositive network bounds never initiate acquisition."""
    layout, identifier, _, identity, source, verifier = _fixture(tmp_path)

    class NoNetwork:
        async def get(
            self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
        ) -> None:
            pytest.fail(f"Invalid deadline reached network: {url}, {limit}, {deadline}, {sink}")

    request = intake.SupportIntakeRequest(
        layout, identifier, 123, identity, source, verifier, seconds
    )
    with pytest.raises(provenance.VerifierPreflightError, match="bounds"):
        asyncio.run(intake.intake_support(request, NoNetwork()))


def test_unsupported_verifier_refuses_before_metadata(tmp_path: Path) -> None:
    """A verifier without the required policy flags cannot cause a download."""
    layout, identifier, _, identity, source, verifier = _fixture(tmp_path)

    class NoNetwork:
        async def get(
            self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
        ) -> None:
            pytest.fail(f"Unqualified verifier reached network: {url}, {limit}, {deadline}, {sink}")

    request = intake.SupportIntakeRequest(layout, identifier, 123, identity, source, verifier, 10)
    with pytest.raises(provenance.VerifierPreflightError, match="attestation"):
        asyncio.run(intake.intake_support(request, NoNetwork()))
