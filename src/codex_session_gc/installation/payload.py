# Copyright (c) 2026

"""Consume one attested bundle through its verified descriptor lease."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import struct
import tarfile
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from codex_session_gc.artifacts.bundle import (
    CONTROL_FILE_MAX_BYTES,
    MANIFEST_MAX_BYTES,
    WHEEL_CONTRACT_MAX_BYTES,
    WHEEL_DIRECTORY_MAX_BYTES,
    WHEEL_MAX_ENTRIES,
    WHEEL_METADATA_MAX_BYTES,
    BundleManifest,
    parse_manifest,
)
from codex_session_gc.artifacts.ustar import UstarReader
from codex_session_gc.installation.provenance import VerifiedArtifact

if TYPE_CHECKING:
    from collections.abc import Buffer, Callable

    from codex_session_gc.installation.read_budget import ReadBudget


class PayloadRefusalError(ValueError):
    """An attested archive is outside the installable bundle contract."""


@dataclass(frozen=True, slots=True)
class Payload:
    """A verified payload extracted into the caller's exclusive version root."""

    version: str
    root: Path
    wheel: str
    manifest: dict[str, object]
    manifest_sha256: str


def _require(condition: object, message: str) -> None:
    if not condition:
        raise PayloadRefusalError(message)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key.")
        result[key] = value
    return result


def _json(data: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(data, object_pairs_hook=_object)
    except (UnicodeDecodeError, ValueError) as error:
        message = "Invalid JSON payload."
        raise PayloadRefusalError(message) from error
    if not isinstance(value, dict):
        message = "JSON payload must be an object."
        raise PayloadRefusalError(message)
    return value


def _safe_parts(name: str) -> tuple[str, ...]:
    raw = name.rstrip("/")
    path = PurePosixPath(raw)
    _require(
        bool(raw)
        and not path.is_absolute()
        and path.as_posix() == raw
        and all(part not in (".", "..") for part in path.parts)
        and "\\" not in raw,
        "Unsafe archive member path.",
    )
    return path.parts


def _root_version(name: str) -> str:
    match = re.fullmatch(
        r"codex-session-gc-([A-Za-z0-9][A-Za-z0-9._+!-]{0,127})-linux-x86_64", name
    )
    if match is None or ".." in name:
        message = "Unexpected archive root."
        raise PayloadRefusalError(message)
    return match.group(1)


def _directory(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=current)
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = next_fd
    except BaseException:
        os.close(current)
        raise
    else:
        return current


class _Readable(Protocol):
    def read(self, size: int) -> bytes: ...


def _copy_file(
    root_fd: int, parts: tuple[str, ...], source: _Readable, size: int
) -> dict[str, object]:
    directory = _directory(root_fd, parts[:-1])
    try:
        fd = os.open(
            parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        with os.fdopen(fd, "wb") as output:
            digest = hashlib.sha256()
            remaining = size
            while remaining:
                data = source.read(min(1024 * 1024, remaining))
                _require(bool(data), "Truncated archive member.")
                output.write(data)
                digest.update(data)
                remaining -= len(data)
            return {"sha256": digest.hexdigest(), "bytes": size}
    finally:
        os.close(directory)


def _read_control(path: Path) -> bytes:
    with path.open("rb") as source:
        value = source.read(CONTROL_FILE_MAX_BYTES + 1)
    _require(len(value) <= CONTROL_FILE_MAX_BYTES, "Control file exceeds its memory bound.")
    return value


def _check_zip_directory(path: Path, budget: ReadBudget | None = None) -> None:
    """Bound ZipFile central-directory allocation before constructing ZipFile."""
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        if budget is not None:
            budget.check(size)
        # Include the 20-byte ZIP64 locator even when the classic comment is maximal.
        tail_size = min(size, 65577)
        source.seek(size - tail_size)
        tail = source.read(tail_size)
        if budget is not None:
            budget.check()
    offset = tail.rfind(b"PK\x05\x06")
    trailer_length = 22
    _require(offset >= 0 and len(tail) - offset >= trailer_length, "Wheel ZIP trailer is missing.")
    locator_length = 20
    _require(
        offset < locator_length or tail[offset - locator_length : offset - 16] != b"PK\x06\x07",
        "ZIP64 wheels are unsupported.",
    )
    fields = struct.unpack_from("<4s4H2IH", tail, offset)
    _, disk, directory_disk, disk_entries, entries, directory_bytes, directory_offset, comment = (
        fields
    )
    zip64_count_sentinel, zip64_offset_sentinel = 0xFFFF, 0xFFFFFFFF
    _require(
        disk == directory_disk == 0
        and zip64_count_sentinel not in (entries, disk_entries)
        and zip64_offset_sentinel not in (directory_bytes, directory_offset)
        and disk_entries == entries
        and entries <= WHEEL_MAX_ENTRIES
        and directory_bytes <= WHEEL_DIRECTORY_MAX_BYTES
        and directory_offset + directory_bytes <= size - tail_size + offset
        and offset + trailer_length + comment == len(tail),
        "Wheel ZIP directory is outside supported bounds.",
    )


def _wheel_binding(
    root: Path,
    version: str,
    manifest: dict[str, object],
    hashes: dict[str, dict[str, object]],
    *,
    budget: ReadBudget | None = None,
) -> str:
    wheel = f"codex_session_gc-{version}-py3-none-any.whl"
    _require(wheel in hashes and "codex-session-gc-native" in hashes, "Missing wheel or helper.")
    try:
        _check_zip_directory(root / wheel, budget)
        with zipfile.ZipFile(root / wheel) as archive:
            if budget is not None:
                budget.check()
            names = archive.namelist()
            meta = [name for name in names if name.endswith(".dist-info/METADATA")]
            contract_name = "codex_session_gc/execution/closed_work_contract.json"
            _require(len(meta) == 1 and names.count(contract_name) == 1, "Invalid wheel metadata.")
            if budget is not None:
                budget.check(archive.getinfo(meta[0]).file_size)
                budget.check(archive.getinfo(contract_name).file_size)
            _require(
                archive.getinfo(meta[0]).file_size <= WHEEL_METADATA_MAX_BYTES,
                "Wheel metadata too large.",
            )
            _require(
                archive.getinfo(contract_name).file_size <= WHEEL_CONTRACT_MAX_BYTES,
                "Wheel contract too large.",
            )
            fields = BytesParser().parsebytes(archive.read(meta[0]))
            if budget is not None:
                budget.check()
            raw = archive.read(contract_name)
            if budget is not None:
                budget.check()
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        message = "Wheel cannot be inspected."
        raise PayloadRefusalError(message) from error
    contract = _json(raw)
    _require(
        fields.get("Name") == "codex-session-gc" and fields.get("Version") == version,
        "Wheel version differs.",
    )
    _require(
        fields.get("Requires-Python") == manifest.get("requires_python"),
        "Python requirement differs.",
    )
    _require(
        hashlib.sha256(raw).hexdigest() == manifest.get("wheel_contract_sha256"),
        "Wheel contract differs.",
    )
    binary = contract.get("binary_sha256")
    digest_length, max_byte = 32, 255
    _require(
        isinstance(binary, list)
        and len(binary) == digest_length
        and all(type(x) is int and 0 <= x <= max_byte for x in binary),
        "Invalid helper identity.",
    )
    if not isinstance(binary, list):
        message = "Invalid helper identity."
        raise PayloadRefusalError(message)
    _require(
        bytes(binary).hex() == hashes["codex-session-gc-native"]["sha256"],
        "Helper differs from contract.",
    )
    _require(
        contract.get("source_identity") == manifest.get("native_source_identity")
        and type(contract.get("wire_version")) is int
        and contract.get("wire_version") == manifest.get("wire_version"),
        "Native contract identity differs.",
    )
    return wheel


def _validate(
    root: Path, parsed: BundleManifest, hashes: dict[str, dict[str, object]], manifest_sha256: str
) -> Payload:
    manifest, version = parsed.raw, parsed.version
    _require(manifest.get("payload") == hashes, "Bundle payload set or hashes differ.")
    wheel = _wheel_binding(root, version, manifest, hashes)
    _require(
        _json(_read_control(root / "native/build-inputs/provenance.json"))
        == manifest.get("source_provenance"),
        "Native provenance differs.",
    )
    required = (
        f"-r requirements-runtime.txt\n./{wheel} --hash=sha256:{hashes[wheel]['sha256']}\n"
    ).encode()
    _require(
        _read_control(root / "install-requirements.txt") == required, "Install requirements differ."
    )
    runtime = _read_control(root / "requirements-runtime.txt").decode("utf-8").replace("\\\n", " ")
    lines = [
        line.strip()
        for line in runtime.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    pattern = (
        r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_.,-]+\])?==[^\s;]+"
        r"(?:\s*;[^\n]*?)?\s+(?:--hash=sha256:[a-f0-9]{64}\s*)+"
    )
    _require(
        bool(lines) and all(re.fullmatch(pattern, line) for line in lines),
        "Runtime requirements are not pinned and hashed.",
    )
    return Payload(version, root, wheel, manifest, manifest_sha256)


class _LeaseReader(io.RawIOBase):
    def __init__(self, lease: VerifiedArtifact) -> None:
        self.lease = lease

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer)
        data = self.lease.read(len(view)) if view else b""
        view[: len(data)] = data
        return len(data)


def _extract_member(
    archive: UstarReader,
    member: tarfile.TarInfo,
    root_fd: int,
    parts: tuple[str, ...],
    hashes: dict[str, dict[str, object]],
) -> None:
    name = "/".join(parts[1:])
    if not name:
        return
    if member.isdir():
        directory = _directory(root_fd, parts[1:])
        os.close(directory)
        return
    identity = _copy_file(root_fd, parts[1:], archive, member.size)
    if name != "manifest.json":
        hashes[name] = identity


def _capacity(
    root_fd: int, manifest: BundleManifest, manifest_bytes: int, reserve_bytes: int
) -> None:
    _require(type(reserve_bytes) is int and reserve_bytes >= 0, "Storage reserve is invalid.")
    stat = os.fstatvfs(root_fd)
    block = stat.f_frsize
    _require(block > 0, "Storage allocation unit is unavailable.")
    allocated = ((manifest_bytes + block - 1) // block) * block
    allocated += sum(((item.size + block - 1) // block) * block for item in manifest.files.values())
    _require(
        allocated + reserve_bytes <= stat.f_bavail * block, "Insufficient installation capacity."
    )


def _manifest_first(archive: UstarReader) -> tuple[str, bytes, BundleManifest]:
    first = archive.next()
    _require(first is not None and first.isdir(), "Archive must start with its root.")
    if first is None:
        message = "Archive has no root."
        raise PayloadRefusalError(message)
    parts = _safe_parts(first.name)
    _require(len(parts) == 1, "Unexpected archive root.")
    version = _root_version(parts[0])
    member = archive.next()
    _require(
        member is not None
        and member.isfile()
        and _safe_parts(member.name) == (parts[0], "manifest.json")
        and 0 < member.size <= MANIFEST_MAX_BYTES,
        "Bounded manifest must precede every payload member.",
    )
    if member is None:
        message = "Archive lacks its manifest."
        raise PayloadRefusalError(message)
    raw = archive.read(MANIFEST_MAX_BYTES + 1)
    _require(len(raw) == member.size, "Truncated manifest.")
    manifest = parse_manifest(raw)
    _require(manifest.version == version, "Archive and manifest versions differ.")
    return parts[0], raw, manifest


def _payload_members(
    archive: UstarReader, root_fd: int, root_name: str, manifest: BundleManifest
) -> dict[str, dict[str, object]]:
    hashes: dict[str, dict[str, object]] = {}
    directories = {
        "/".join(path.split("/")[:index])
        for path in manifest.files
        for index in range(1, len(path.split("/")))
    }
    seen: set[str] = set()
    previous = ""
    while (entry := archive.next()) is not None:
        member_parts = _safe_parts(entry.name)
        _require(member_parts[0] == root_name and len(member_parts) > 1, "Multiple archive roots.")
        name = "/".join(member_parts[1:])
        _require(name not in seen and name > previous, "Duplicate or unsorted archive member.")
        seen.add(name)
        previous = name
        if entry.isdir():
            _require(name in directories, "Undeclared archive directory.")
        else:
            _require(entry.isfile() and name in manifest.files, "Undeclared archive member.")
            _require(entry.size == manifest.files[name].size, "Archive member size differs.")
        _extract_member(archive, entry, root_fd, member_parts, hashes)
    _require(set(hashes) == set(manifest.files), "Archive payload is incomplete.")
    return hashes


def extract_payload(
    lease: VerifiedArtifact,
    create_root: Callable[[str], Path],
    *,
    reserve_bytes: int,
    expected_version: str,
) -> Payload:
    """Stream a manifest-first lease into an exclusively created final version root."""
    _require(type(lease) is VerifiedArtifact, "A verified artifact lease is required.")
    root_fd = -1
    try:
        with gzip.GzipFile(fileobj=_LeaseReader(lease), mode="rb") as compressed:
            archive = UstarReader(compressed)
            root_name, raw, manifest = _manifest_first(archive)
            _require(
                manifest.version == expected_version,
                "Verified bundle differs from expected version.",
            )
            root = create_root(manifest.version)
            _require(root.is_absolute(), "Version root must be absolute.")
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            _capacity(root_fd, manifest, len(raw), reserve_bytes)
            _copy_file(root_fd, ("manifest.json",), io.BytesIO(raw), len(raw))
            hashes = _payload_members(archive, root_fd, root_name, manifest)
        lease.validate()
        result = _validate(root, manifest, hashes, hashlib.sha256(raw).hexdigest())
        lease.validate()
    except (tarfile.TarError, OSError, EOFError, ValueError) as error:
        if isinstance(error, PayloadRefusalError):
            raise
        message = "Archive could not be safely consumed."
        raise PayloadRefusalError(message) from error
    else:
        return result
    finally:
        if root_fd >= 0:
            os.close(root_fd)
