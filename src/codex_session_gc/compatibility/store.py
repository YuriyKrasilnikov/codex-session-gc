# Copyright (c) 2026

"""Content-addressed support records and non-reauthenticated local receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import suppress
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal, NoReturn
from uuid import UUID

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity
from codex_session_gc.compatibility.record import MAX_RECORD_BYTES, parse_record
from codex_session_gc.installation.activation import completed_installation
from codex_session_gc.installation.model import InstallBinding, InstallLayout
from codex_session_gc.installation.provenance import REPOSITORY, SIGNER_WORKFLOW, VerifiedArtifact
from codex_session_gc.installation.transaction import installation_lock

_RECEIPT_FORMAT = "codex-session-gc-support-intake-v1"
_RECEIPT_MAX_BYTES = 16384


class SupportStoreError(Exception):
    """A support record or its saved local verification context is unavailable."""


class RecordedOrigin(StrEnum):
    """A saved receipt is not a current cryptographic re-verification."""

    RECORDED_NOT_REAUTHENTICATED = "verification_recorded_not_reauthenticated"


@dataclass(frozen=True, slots=True)
class SupportIntakeReceipt:
    """One local verification context bound to exact bytes and one installation."""

    installation_id: UUID
    artifact: ArtifactIdentity
    asset_id: int
    gc_source: SourceIdentity
    context_sha256: str
    installation_binding: InstallBinding

    @property
    def origin(self) -> RecordedOrigin:
        """A local receipt never proves fresh cryptographic verification."""
        return RecordedOrigin.RECORDED_NOT_REAUTHENTICATED

    @property
    def claim_verified(self) -> Literal[False]:
        """A signed publisher statement does not verify its support claim."""
        return False

    @property
    def authorizes_operation(self) -> Literal[False]:
        """A support receipt is not cleanup authority."""
        return False


@dataclass(frozen=True, slots=True)
class StoredSupport:
    """Exact support record bytes and their saved verification context."""

    raw: bytes
    receipt: SupportIntakeReceipt


def _fail(message: str) -> NoReturn:
    raise SupportStoreError(message)


def _encoded(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _context(
    installation_id: UUID,
    artifact: ArtifactIdentity,
    asset_id: int,
    source: SourceIdentity,
    binding: InstallBinding,
) -> dict[str, object]:
    return {
        "format": _RECEIPT_FORMAT,
        "installation_id": str(installation_id),
        "artifact": {"sha256": artifact.sha256, "size": artifact.size},
        "asset_id": asset_id,
        "gc_source": {"commit": source.commit, "ref": source.ref},
        "repository": REPOSITORY,
        "workflow": SIGNER_WORKFLOW,
        "installation_binding": asdict(binding),
    }


def _receipt(
    installation_id: UUID,
    artifact: ArtifactIdentity,
    asset_id: int,
    source: SourceIdentity,
    binding: InstallBinding,
) -> SupportIntakeReceipt:
    if (
        type(installation_id) is not UUID
        or type(artifact) is not ArtifactIdentity
        or not 0 < artifact.size <= MAX_RECORD_BYTES
        or type(asset_id) is not int
        or asset_id <= 0
        or type(source) is not SourceIdentity
        or source.ref != "refs/heads/main"
        or type(binding) is not InstallBinding
    ):
        _fail("Support verification context is invalid.")
    digest = hashlib.sha256(
        _encoded(_context(installation_id, artifact, asset_id, source, binding))
    ).hexdigest()
    return SupportIntakeReceipt(installation_id, artifact, asset_id, source, digest, binding)


def _open_store(layout: InstallLayout, installation_id: UUID, *, create: bool) -> int:
    parent = os.open(layout.base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for name in ("support", str(installation_id)):
            if create:
                with suppress(FileExistsError):
                    os.mkdir(name, 0o700, dir_fd=parent)
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
            )
            info = os.fstat(child)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                os.close(child)
                _fail("Support directory is not privately owner-controlled.")
            os.close(parent)
            parent = child
    except BaseException:
        os.close(parent)
        raise
    return parent


def _read_owned(directory: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o077
            or before.st_size > limit
        ):
            _fail("Stored support file is not a bounded private regular file.")
        chunks = bytearray()
        while len(chunks) < before.st_size:
            block = os.read(fd, min(65536, before.st_size - len(chunks)))
            if not block:
                _fail("Stored support file is truncated.")
            chunks.extend(block)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)

        def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

        if identity(before) != identity(after) or identity(before) != identity(current):
            _fail("Stored support file changed during observation.")
        return bytes(chunks)
    finally:
        os.close(fd)


def _publish(directory: int, name: str, data: bytes) -> None:
    """Publish exact bytes once, preserving any conflicting existing object."""
    temporary = f".support-{secrets.token_hex(16)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    identity = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(
                temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False
            )
        except FileExistsError:
            if _read_owned(directory, name, max(len(data), 1)) != data:
                _fail("Support content identity conflicts with an existing file.")
        os.fsync(directory)
    finally:
        try:
            current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def promote_support(
    layout: InstallLayout, installation_id: UUID, lease: VerifiedArtifact, asset_id: int
) -> SupportIntakeReceipt:
    """Promote exact leased bytes after verifier success, without replacing any record."""
    if type(lease) is not VerifiedArtifact:
        _fail("A verified artifact lease is required.")
    artifact, source = lease.artifact_identity, lease.source_identity
    if artifact.size <= 0 or artifact.size > MAX_RECORD_BYTES:
        _fail("Support record exceeds its byte bound.")
    raw = lease.read(artifact.size + 1)
    if len(raw) != artifact.size or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        _fail("Verified support bytes differ from the leased identity.")
    parse_record(raw)
    with installation_lock(layout), completed_installation(layout, installation_id) as installation:
        receipt = _receipt(installation_id, artifact, asset_id, source, installation.binding)
        encoded = _encoded(
            _context(installation_id, artifact, asset_id, source, installation.binding)
        )
        directory = _open_store(layout, installation_id, create=True)
        try:
            lease.validate()
            _publish(directory, f"{artifact.sha256}.json", raw)
            lease.validate()
            _publish(directory, f"{artifact.sha256}.{receipt.context_sha256}.receipt.json", encoded)
            lease.validate()
        finally:
            os.close(directory)
    return receipt


def read_support(
    layout: InstallLayout, installation_id: UUID, record_sha256: str, context_sha256: str
) -> StoredSupport:
    """Read one explicitly selected local record and receipt without network access."""
    if type(installation_id) is not UUID or any(
        not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
        for value in (record_sha256, context_sha256)
    ):
        _fail("Support selection requires exact SHA-256 identities.")
    with completed_installation(layout, installation_id) as installation:
        directory = _open_store(layout, installation_id, create=False)
        try:
            raw = _read_owned(directory, f"{record_sha256}.json", MAX_RECORD_BYTES)
            receipt_bytes = _read_owned(
                directory, f"{record_sha256}.{context_sha256}.receipt.json", _RECEIPT_MAX_BYTES
            )
        finally:
            os.close(directory)
        try:
            value: object = json.loads(receipt_bytes)
        except (ValueError, UnicodeError) as error:
            message = "Support receipt is invalid."
            raise SupportStoreError(message) from error
        if not isinstance(value, dict) or set(value) != {
            "format",
            "installation_id",
            "artifact",
            "asset_id",
            "gc_source",
            "repository",
            "workflow",
            "installation_binding",
        }:
            _fail("Support receipt is invalid.")
        artifact_data, source_data = value["artifact"], value["gc_source"]
        if (
            not isinstance(artifact_data, dict)
            or set(artifact_data) != {"sha256", "size"}
            or not isinstance(source_data, dict)
            or set(source_data) != {"commit", "ref"}
        ):
            _fail("Support receipt identities are invalid.")
        try:
            artifact = ArtifactIdentity(artifact_data["sha256"], artifact_data["size"])
            source = SourceIdentity(source_data["commit"], source_data["ref"])
            receipt = _receipt(
                installation_id, artifact, value["asset_id"], source, installation.binding
            )
        except (TypeError, ValueError) as error:
            message = "Support receipt identities are invalid."
            raise SupportStoreError(message) from error
        if (
            receipt.artifact.sha256 != record_sha256
            or receipt.context_sha256 != context_sha256
            or receipt_bytes != _encoded(value)
            or value
            != _context(installation_id, artifact, receipt.asset_id, source, installation.binding)
            or len(raw) != artifact.size
            or hashlib.sha256(raw).hexdigest() != record_sha256
        ):
            _fail("Stored support context or bytes do not match the selection.")
        parse_record(raw)
        return StoredSupport(raw, receipt)
