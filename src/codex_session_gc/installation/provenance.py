# Copyright (c) 2026

"""Verify artifact origin using a separately trusted GitHub CLI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import stat
import tempfile
from enum import StrEnum
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity

REPOSITORY = "YuriyKrasilnikov/codex-session-gc"
SIGNER_WORKFLOW = f"{REPOSITORY}/.github/workflows/artifacts.yml"
PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
_OUTPUT_LIMIT = 1024 * 1024
_TIMEOUT = 120.0
_LEASE_TOKEN = object()


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

    def __init__(self, fd: int, fingerprint: tuple[int, int, int, int], *, _token: object) -> None:
        """Retain a descriptor admitted by provenance verification."""
        if _token is not _LEASE_TOKEN:
            msg = "verified leases are created by provenance verification"
            raise ValueError(msg)
        self._fd = fd
        self._fingerprint = fingerprint
        self._offset = 0

    def validate(self) -> None:
        """Refuse if the leased file has changed or is closed."""
        if self._fd < 0 or _fingerprint(self._fd) != self._fingerprint:
            raise ProvenanceRefusalError(RefusalReason.ARTIFACT)

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


def _wake(ready: asyncio.Future[None]) -> None:
    if not ready.done():
        ready.set_result(None)


async def _read_pipe(fd: int) -> bytes:
    loop = asyncio.get_running_loop()
    output = bytearray()
    while True:
        ready: asyncio.Future[None] = loop.create_future()

        loop.add_reader(fd, _wake, ready)
        try:
            await ready
        finally:
            loop.remove_reader(fd)
        try:
            block = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if not block:
            return bytes(output)
        output.extend(block)
        if len(output) > _OUTPUT_LIMIT:
            raise ProvenanceRefusalError(RefusalReason.OUTPUT)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


async def _cleanup_verifier(
    creation: asyncio.Task[asyncio.subprocess.Process] | None,
    process: asyncio.subprocess.Process | None,
    read_fd: int,
    write_fd: int,
) -> None:
    try:
        if process is None and creation is not None:
            with contextlib.suppress(OSError):
                process = await creation
        if process is not None:
            _kill_group(process)
            await process.wait()
    finally:
        try:
            os.close(read_fd)
        finally:
            if write_fd >= 0:
                os.close(write_fd)


async def _await_cleanup(cleanup: asyncio.Task[None]) -> None:
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


async def _run_verifier(command: list[str], fd: int, directory: str) -> bytes:
    allowed = ("HOME", "PATH", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR")
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["GH_PROMPT_DISABLED"] = "1"
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    os.set_blocking(read_fd, False)
    process: asyncio.subprocess.Process | None = None
    creation: asyncio.Task[asyncio.subprocess.Process] | None = None
    try:
        creation = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=directory,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=write_fd,
                stderr=asyncio.subprocess.DEVNULL,
                pass_fds=(fd,),
                start_new_session=True,
            )
        )
        try:
            async with asyncio.timeout(_TIMEOUT):
                process = await asyncio.shield(creation)
                os.close(write_fd)
                write_fd = -1
                output = await _read_pipe(read_fd)
                await process.wait()
                if process.returncode != 0:
                    raise ProvenanceRefusalError(RefusalReason.VERIFIER_FAILED)
                return output
        except TimeoutError as exc:
            raise ProvenanceRefusalError(RefusalReason.TIMEOUT) from exc
    except OSError as exc:
        raise ProvenanceRefusalError(RefusalReason.VERIFIER_UNAVAILABLE) from exc
    finally:
        cleanup = asyncio.create_task(_cleanup_verifier(creation, process, read_fd, write_fd))
        await _await_cleanup(cleanup)


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
        "--repo",
        REPOSITORY,
        "--signer-workflow",
        SIGNER_WORKFLOW,
        "--source-digest",
        source.commit,
        "--source-ref",
        source.ref,
        "--predicate-type",
        PREDICATE_TYPE,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    with tempfile.TemporaryDirectory(prefix="codex-gc-verify-") as directory:
        output = await _run_verifier(command, fd, directory)
    if not _statement_matches(output, expected):
        raise ProvenanceRefusalError(RefusalReason.OUTPUT)
    if _fingerprint(fd) != before:
        raise ProvenanceRefusalError(RefusalReason.ARTIFACT)
    return VerifiedArtifact(fd, before, _token=_LEASE_TOKEN)


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
