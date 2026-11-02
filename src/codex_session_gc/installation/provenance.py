# Copyright (c) 2026

"""Verify artifact origin using a separately trusted GitHub CLI."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity
from codex_session_gc.installation.process import (
    OwnedProcessError,
    ProcessFailure,
    ProcessLimits,
    run_owned,
)

if TYPE_CHECKING:
    from pathlib import Path

REPOSITORY = "YuriyKrasilnikov/codex-session-gc"
SIGNER_WORKFLOW = f"{REPOSITORY}/.github/workflows/artifacts.yml"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
VERIFIER_OUTPUT_LIMIT = 1024 * 1024
_TIMEOUT = 120.0
_LEASE_TOKEN = object()


class VerificationOption(StrEnum):
    """Options required for the trusted artifact-verification policy."""

    REPOSITORY = "--repo"
    SIGNER_WORKFLOW = "--signer-workflow"
    SOURCE_DIGEST = "--source-digest"
    SOURCE_REF = "--source-ref"
    PREDICATE_TYPE = "--predicate-type"
    DENY_SELF_HOSTED = "--deny-self-hosted-runners"
    FORMAT = "--format"


class RefusalReason(StrEnum):
    """Why an artifact could not be admitted."""

    ARTIFACT = "artifact"
    VERIFIER_UNAVAILABLE = "verifier_unavailable"
    VERIFIER_FAILED = "verifier_failed"
    TIMEOUT = "timeout"
    OUTPUT = "output"


class ProvenanceRefusalError(Exception):
    """The artifact cannot be admitted under the provenance policy."""

    def __init__(self, reason: RefusalReason) -> None:
        """Record the refusal category."""
        self.reason = reason
        super().__init__(reason.value)


class VerifierPreflightError(Exception):
    """The separately trusted verifier cannot satisfy the required policy."""


async def preflight_gh(gh: Path, directory: Path, limits: ProcessLimits) -> None:
    """Check the verifier's required policy options before acquiring an artifact."""
    if (
        type(limits.seconds) not in (int, float)
        or not math.isfinite(limits.seconds)
        or limits.seconds <= 0
        or type(limits.output_bytes) is not int
        or limits.output_bytes <= 0
    ):
        message = "Verifier resource bounds must be finite and positive."
        raise VerifierPreflightError(message)
    environment = {
        key: os.environ[key]
        for key in ("HOME", "PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in os.environ
    }
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        output = await run_owned(
            [str(gh), "attestation", "verify", "--help"],
            cwd=directory,
            environment=environment,
            limits=limits,
        )
    except OwnedProcessError as error:
        message = "Trusted gh cannot provide the required attestation verifier."
        raise VerifierPreflightError(message) from error
    text = output.decode("utf-8", errors="replace")
    if any(
        re.search(rf"(?<![\w-]){re.escape(option.value)}(?![\w-])", text) is None
        for option in VerificationOption
    ):
        message = "Trusted gh lacks required attestation verification options."
        raise VerifierPreflightError(message)


def _fingerprint(fd: int) -> tuple[int, int, int, int]:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise ProvenanceRefusalError(RefusalReason.ARTIFACT) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ProvenanceRefusalError(RefusalReason.ARTIFACT)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class VerifiedArtifact:
    """Own the verified descriptor for the duration of artifact consumption."""

    def __init__(
        self,
        fd: int,
        fingerprint: tuple[int, int, int, int],
        artifact: ArtifactIdentity,
        source: SourceIdentity,
        *,
        _token: object,
    ) -> None:
        """Retain a descriptor admitted by provenance verification."""
        if _token is not _LEASE_TOKEN:
            msg = "verified leases are created by provenance verification"
            raise ValueError(msg)
        self._fd = fd
        self._fingerprint = fingerprint
        self._artifact = artifact
        self._source = source
        self._offset = 0

    def validate(self) -> None:
        """Refuse if the leased file has changed or is closed."""
        if self._fd < 0 or _fingerprint(self._fd) != self._fingerprint:
            raise ProvenanceRefusalError(RefusalReason.ARTIFACT)

    @property
    def artifact_identity(self) -> ArtifactIdentity:
        """Return the exact artifact identity verified for this open lease."""
        self.validate()
        return self._artifact

    @property
    def source_identity(self) -> SourceIdentity:
        """Return the source identity enforced by the verifier for this lease."""
        self.validate()
        return self._source

    def read(self, size: int) -> bytes:
        """Read the next bytes from the verified descriptor after checking its identity."""
        self.validate()
        if size <= 0:
            msg = "read size must be positive"
            raise ValueError(msg)
        try:
            data = os.pread(self._fd, size, self._offset)
        except OSError as exc:
            raise ProvenanceRefusalError(RefusalReason.ARTIFACT) from exc
        self.validate()
        self._offset += len(data)
        return data

    def close(self) -> None:
        """Close the descriptor."""
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> Self:
        """Enter a verified artifact lease."""
        self.validate()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Validate and close the lease when consumption ends."""
        try:
            if exc_type is None:
                self.validate()
        finally:
            self.close()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = "duplicate JSON key"
            raise ValueError(msg)
        result[key] = value
    return result


def _subject_match(subject: object, expected: ArtifactIdentity) -> tuple[bool, bool]:
    if not isinstance(subject, dict):
        return False, False
    digest = subject.get("digest")
    if not isinstance(digest, dict) or not digest:
        return False, False
    for algorithm, value in digest.items():
        if (
            not isinstance(algorithm, str)
            or not algorithm
            or not isinstance(value, str)
            or not value
            or (algorithm == "sha256" and re.fullmatch(r"[0-9a-f]{64}", value) is None)
        ):
            return False, False
    return True, digest.get("sha256") == expected.sha256


def _verified_entry_matches(entry: object, expected: ArtifactIdentity) -> bool:
    if not isinstance(entry, dict):
        return False
    verification = entry.get("verificationResult")
    if not isinstance(verification, dict):
        return False
    statement = verification.get("statement")
    if not isinstance(statement, dict) or statement.get("predicateType") != PREDICATE_TYPE:
        return False
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or not subjects:
        return False
    matched = False
    for subject in subjects:
        valid, match = _subject_match(subject, expected)
        if not valid:
            return False
        matched = matched or match
    return matched


def _statement_matches(output: bytes, expected: ArtifactIdentity) -> bool:
    try:
        result: object = json.loads(output, object_pairs_hook=_unique_object)
        return (
            isinstance(result, list)
            and bool(result)
            and all(_verified_entry_matches(entry, expected) for entry in result)
        )
    except ValueError, UnicodeDecodeError, RecursionError:
        return False


async def _run_verifier(command: list[str], fd: int, directory: str) -> bytes:
    allowed = ("HOME", "PATH", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["GH_PROMPT_DISABLED"] = "1"
    try:
        return await run_owned(
            command,
            cwd=directory,
            environment=environment,
            limits=ProcessLimits(_TIMEOUT, VERIFIER_OUTPUT_LIMIT),
            pass_fds=(fd,),
        )
    except OwnedProcessError as error:
        reason = {
            ProcessFailure.UNAVAILABLE: RefusalReason.VERIFIER_UNAVAILABLE,
            ProcessFailure.FAILED: RefusalReason.VERIFIER_FAILED,
            ProcessFailure.TIMEOUT: RefusalReason.TIMEOUT,
            ProcessFailure.OUTPUT: RefusalReason.OUTPUT,
            ProcessFailure.STORAGE: RefusalReason.VERIFIER_FAILED,
        }[error.reason]
        raise ProvenanceRefusalError(reason) from error


async def _verify_open_artifact(
    fd: int,
    expected: ArtifactIdentity,
    source: SourceIdentity,
    trusted_gh: Path,
) -> VerifiedArtifact:
    before = _fingerprint(fd)
    if before[2] != expected.size:
        raise ProvenanceRefusalError(RefusalReason.ARTIFACT)
    command = [
        str(trusted_gh),
        "attestation",
        "verify",
        f"/proc/self/fd/{fd}",
        VerificationOption.REPOSITORY.value,
        REPOSITORY,
        VerificationOption.SIGNER_WORKFLOW.value,
        SIGNER_WORKFLOW,
        VerificationOption.SOURCE_DIGEST.value,
        source.commit,
        VerificationOption.SOURCE_REF.value,
        source.ref,
        VerificationOption.PREDICATE_TYPE.value,
        PREDICATE_TYPE,
        VerificationOption.DENY_SELF_HOSTED.value,
        VerificationOption.FORMAT.value,
        "json",
    ]
    with tempfile.TemporaryDirectory(prefix="codex-gc-verify-") as directory:
        output = await _run_verifier(command, fd, directory)
    if not _statement_matches(output, expected):
        raise ProvenanceRefusalError(RefusalReason.OUTPUT)
    if _fingerprint(fd) != before:
        raise ProvenanceRefusalError(RefusalReason.ARTIFACT)
    return VerifiedArtifact(fd, before, expected, source, _token=_LEASE_TOKEN)


async def verify_artifact(
    path: Path,
    expected: ArtifactIdentity,
    source: SourceIdentity,
    *,
    trusted_gh: Path,
) -> VerifiedArtifact:
    """Verify an owned staging file and return its still-open descriptor lease.

    The caller obtains gh independently and controls the staging file throughout
    verification and consumption. The signature establishes assembly origin.
    """
    if type(expected) is not ArtifactIdentity or type(source) is not SourceIdentity:
        raise ProvenanceRefusalError(RefusalReason.ARTIFACT)
    if not trusted_gh.is_absolute():
        raise ProvenanceRefusalError(RefusalReason.VERIFIER_UNAVAILABLE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProvenanceRefusalError(RefusalReason.ARTIFACT) from exc
    try:
        return await _verify_open_artifact(fd, expected, source, trusted_gh)
    except BaseException:
        os.close(fd)
        raise
