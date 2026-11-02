# Copyright (c) 2026

"""Assemble and verify source-qualified native material for release assembly."""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NoReturn

from codex_session_gc.artifacts.ustar import UstarReader

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import BinaryIO

SOURCE_PROFILE = Path("src/codex_session_gc/execution/closed_work_contract.json")
SOURCE_PROVENANCE = Path("native/build-inputs/provenance.json")
REPOSITORY = "YuriyKrasilnikov/codex-session-gc"
FORMAT = "codex-session-gc-native-material-v1"
SUMMARY_FORMAT = "codex-session-gc-native-qualification-v1"
ROOT = "native-material"
HELPER = "codex-session-gc-native"
PROFILE = "profile.json"
SUMMARY = "qualification.json"
_MAX_CONTROL = 1024 * 1024
_MAX_PROFILE = 16 * 1024 * 1024
_MAX_NOTICE = 16 * 1024 * 1024


class ReuseError(ValueError):
    """Changed inputs require independent qualification, never a substituted digest."""


def digest(path: Path) -> str:
    """Hash complete file bytes without materializing the executable."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _require_material(message: str, *, condition: bool) -> None:
    if not condition:
        raise ReuseError(message)


def _native_paths(root: Path) -> set[str]:
    helper = root / "native/helper"
    paths = {helper / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")}
    paths.update(path for path in (helper / "src").rglob("*") if path.is_file())
    paths.update((root / "native/patches").glob("*.patch"))
    if (helper / "build.rs").exists():
        paths.add(helper / "build.rs")
    paths.update(path for path in (helper / ".cargo").rglob("*") if path.is_file())
    return {path.relative_to(root).as_posix() for path in paths}


def verify_material_source(
    root: Path, *, helper_sha256: str, profile_sha256: str, native_inputs_sha256: str
) -> dict[str, str | int]:
    """Check native input and raw profile bytes against qualified material identities."""
    root = root.resolve()
    _require_material(
        "Raw profile changed; separate qualification and receipt-consumer transition required.",
        condition=digest(root / SOURCE_PROFILE) == profile_sha256,
    )
    profile = json.loads((root / SOURCE_PROFILE).read_bytes())
    _require_material(
        "Profile and qualified helper anchors disagree.",
        condition=profile["binary_sha256"] == list(bytes.fromhex(helper_sha256)),
    )
    provenance = json.loads((root / SOURCE_PROVENANCE).read_bytes())
    current = provenance["current_delivery_inputs"]
    manifest = current["manifest"]
    encoded = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    actual = hashlib.sha256(encoded).hexdigest()
    _require_material(
        "Native input manifest changed; qualified-helper reuse is refused.",
        condition=actual == current["manifest_sha256"] == native_inputs_sha256,
    )
    entries = manifest["inputs"]
    declared = {entry["path"] for entry in entries}
    _require_material(
        "Native input file set changed or contains duplicates.",
        condition=len(declared) == len(entries) and declared == _native_paths(root),
    )
    for entry in entries:
        path = root / entry["path"]
        _require_material(
            f"Native input is not a regular source file: {entry['path']}",
            condition=not path.is_symlink() and path.resolve().is_relative_to(root),
        )
        _require_material(
            f"Native input differs from qualified bytes: {entry['path']}",
            condition=path.is_file()
            and path.stat().st_size == entry["bytes"]
            and digest(path) == entry["sha256"],
        )
    return {
        "profile_sha256": profile_sha256,
        "native_inputs_sha256": actual,
        "native_input_files": len(entries),
        "helper_sha256": helper_sha256,
    }


class CandidateError(ValueError):
    """The native candidate does not match reviewed qualification."""


@dataclass(frozen=True, slots=True)
class Qualification:
    """Material identities derived from reviewed native provenance."""

    helper_sha256: str
    helper_bytes: int
    profile_sha256: str
    inputs_sha256: str
    qualification_identity: str
    summary_sha256: str
    checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Transport:
    """An asset locator and exact compressed component identity."""

    asset_id: int
    sha256: str
    size: int

    @property
    def api_url(self) -> str:
        """Locate the release asset in the fixed project repository."""
        return f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{self.asset_id}"


def _fail(message: str) -> NoReturn:
    raise CandidateError(message)


def _require(condition: object, message: str) -> None:
    if not condition:
        _fail(message)


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate JSON key.")
        result[key] = value
    return result


def _object(raw: bytes, limit: int) -> dict[str, object]:
    _require(0 < len(raw) <= limit, "Control file exceeds its bound.")
    try:
        value: object = json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, ValueError) as error:
        message = "Invalid JSON object."
        raise CandidateError(message) from error
    if not isinstance(value, dict):
        _fail("Control file must contain an object.")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _fail("Invalid SHA-256 identity.")
    return value


def _size(value: object) -> int:
    if type(value) is not int or value <= 0:
        _fail("Invalid positive byte count.")
    return value


def _bounded(path: Path, limit: int) -> bytes:
    _require(not path.is_symlink() and path.is_file(), "Expected a regular source file.")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    _require(len(data) <= limit, "Source control file exceeds its bound.")
    return data


def _check_identities(build: dict[str, object]) -> tuple[str, ...]:
    partitions = build.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        _fail("Qualification check evidence is absent.")
    checks = []
    for partition in partitions:
        if not isinstance(partition, dict):
            _fail("Qualification check evidence is invalid.")
        encoded = json.dumps(partition, sort_keys=True, separators=(",", ":"), allow_nan=False)
        checks.append(hashlib.sha256(encoded.encode()).hexdigest())
    return tuple(checks)


def _load_qualification(
    root: Path, *, include_transport: bool
) -> tuple[Qualification, Transport | None]:
    value = _object(_bounded(root / SOURCE_PROVENANCE, _MAX_CONTROL), _MAX_CONTROL)
    build = value.get("build_qualification")
    current = value.get("current_delivery_inputs")
    if not isinstance(build, dict) or not isinstance(current, dict):
        _fail("Native qualification record is absent.")
    candidate = build.get("native_candidate")
    if not isinstance(candidate, dict) or set(candidate) not in (
        {"summary_sha256"},
        {"summary_sha256", "transport"},
    ):
        _fail("Native candidate qualification is absent or invalid.")
    qualification = Qualification(
        _sha(build.get("helper_sha256")),
        _size(build.get("helper_bytes")),
        _sha(build.get("contract_sha256")),
        _sha(current.get("manifest_sha256")),
        _sha(build.get("qualification_identity")),
        _sha(candidate.get("summary_sha256")),
        _check_identities(build),
    )
    if not include_transport or "transport" not in candidate:
        return qualification, None
    locator = candidate["transport"]
    if not isinstance(locator, dict) or set(locator) != {"asset_id", "sha256", "bytes"}:
        _fail("Native candidate transport is invalid.")
    asset_id = _size(locator.get("asset_id"))
    return qualification, Transport(
        asset_id, _sha(locator.get("sha256")), _size(locator.get("bytes"))
    )


def load_qualification(root: Path) -> tuple[Qualification, Transport | None]:
    """Read source-pinned material and its optional transport identity."""
    return _load_qualification(root, include_transport=True)


def verify_qualification_source(root: Path) -> Qualification:
    """Validate qualified material without requiring or interpreting a locator."""
    qualification, _ = _load_qualification(root, include_transport=False)
    _check_source(root, qualification)
    return qualification


def _check_source(root: Path, qualification: Qualification) -> None:
    profile = _object(_bounded(root / SOURCE_PROFILE, _MAX_PROFILE), _MAX_PROFILE)
    evidence = profile.get("qualification_evidence")
    if not isinstance(evidence, dict) or set(evidence) != {
        "content_type",
        "version",
        "data_base64",
    }:
        _fail("Profile qualification evidence is invalid.")
    encoded = evidence.get("data_base64")
    if (
        evidence.get("content_type")
        != "application/vnd.codex-session-gc.direct-native-qualification+json"
        or type(evidence.get("version")) is not int
        or evidence.get("version") != 1
        or not isinstance(encoded, str)
    ):
        _fail("Profile qualification evidence is invalid.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        message = "Profile qualification evidence is invalid."
        raise CandidateError(message) from error
    decoded = _object(raw, _MAX_PROFILE)
    _require(
        _sha(decoded.get("qualification_identity")) == qualification.qualification_identity,
        "Profile qualification identity differs.",
    )
    _require(
        _sha(decoded.get("compiled_source_manifest_sha256")) == qualification.inputs_sha256
        and _sha(decoded.get("helper_sha256")) == qualification.helper_sha256
        and _size(decoded.get("helper_bytes")) == qualification.helper_bytes,
        "Profile material identities differ from qualification.",
    )
    _require(
        _check_identities(decoded) == qualification.checks,
        "Profile qualification checks differ from provenance.",
    )
    _bounded(root / SOURCE_PROVENANCE, _MAX_CONTROL)
    try:
        verify_material_source(
            root,
            helper_sha256=qualification.helper_sha256,
            profile_sha256=qualification.profile_sha256,
            native_inputs_sha256=qualification.inputs_sha256,
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        message = "Native source does not match qualification."
        raise CandidateError(message) from error


def _summary(raw: bytes, qualification: Qualification) -> None:
    _require(
        hashlib.sha256(raw).hexdigest() == qualification.summary_sha256,
        "Qualification summary digest differs.",
    )
    value = _object(raw, _MAX_CONTROL)
    _require(
        set(value)
        == {
            "format",
            "helper_sha256",
            "helper_bytes",
            "profile_sha256",
            "native_input_manifest_sha256",
            "qualification_identity",
            "checks",
        },
        "Qualification summary fields differ.",
    )
    _require(
        value.get("format") == SUMMARY_FORMAT
        and value.get("helper_sha256") == qualification.helper_sha256
        and value.get("helper_bytes") == qualification.helper_bytes
        and type(value.get("helper_bytes")) is int
        and value.get("profile_sha256") == qualification.profile_sha256
        and value.get("native_input_manifest_sha256") == qualification.inputs_sha256
        and value.get("qualification_identity") == qualification.qualification_identity,
        "Qualification summary identity differs.",
    )
    _require(value.get("checks") == list(qualification.checks), "Qualification checks differ.")


def _notices(root: Path) -> dict[str, bytes]:
    project = tomllib.loads(_bounded(root / "pyproject.toml", _MAX_CONTROL).decode())["project"]
    patterns = project["license-files"]
    _require(isinstance(patterns, list) and bool(patterns), "License declarations are absent.")
    result: dict[str, bytes] = {}
    for pattern in patterns:
        _require(
            isinstance(pattern, str)
            and bool(pattern)
            and not any(character in pattern for character in "*?[]")
            and not PurePosixPath(pattern).is_absolute()
            and PurePosixPath(pattern).as_posix() == pattern
            and all(part not in {".", ".."} for part in PurePosixPath(pattern).parts),
            "Invalid license declaration.",
        )
        paths = list(root.glob(pattern))
        _require(bool(paths), "A declared license is missing.")
        for path in paths:
            _require(path.resolve().is_relative_to(root.resolve()), "License path escapes source.")
            name = path.relative_to(root).as_posix()
            _require(
                name not in {HELPER, PROFILE, SUMMARY, "manifest.json"},
                "License path conflicts with material.",
            )
            result[name] = _bounded(path, _MAX_NOTICE)
    return result


def _members(notices: Mapping[str, bytes]) -> list[str]:
    names = {HELPER, PROFILE, SUMMARY, *notices}
    directories = {
        str(parent)
        for name in names
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    _require(not names.intersection(directories), "Material file and directory conflict.")
    return sorted(names | directories)


def _identity(data: bytes) -> dict[str, object]:
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _manifest(
    qualification: Qualification, summary_size: int, profile: bytes, notices: Mapping[str, bytes]
) -> bytes:
    files: dict[str, dict[str, object]] = {name: _identity(data) for name, data in notices.items()}
    files.update(
        {
            HELPER: {"sha256": qualification.helper_sha256, "bytes": qualification.helper_bytes},
            PROFILE: _identity(profile),
            SUMMARY: {"sha256": qualification.summary_sha256, "bytes": summary_size},
        }
    )
    return (
        json.dumps({"format": FORMAT, "files": files}, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _info(name: str, size: int, *, directory: bool = False) -> tarfile.TarInfo:
    item = tarfile.TarInfo(name)
    item.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    item.mode = 0o755 if directory or name == f"{ROOT}/{HELPER}" else 0o644
    item.uid = item.gid = item.mtime = 0
    item.uname = item.gname = ""
    item.size = size
    return item


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    archive.addfile(_info(f"{ROOT}/{name}", len(data)), io.BytesIO(data))


def _assemble_candidate(root: Path, helper: Path, summary: Path, output: Path) -> tuple[str, int]:
    qualification, transport = load_qualification(root)
    _check_source(root, qualification)
    profile = _bounded(root / SOURCE_PROFILE, _MAX_PROFILE)
    summary_bytes = _bounded(summary, _MAX_CONTROL)
    _summary(summary_bytes, qualification)
    notices = _notices(root)
    _require(not output.exists() and not output.is_symlink(), "Output already exists.")
    _require(not helper.is_symlink() and helper.is_file(), "Helper must be a regular file.")
    _require(
        helper.stat().st_size == qualification.helper_bytes
        and digest(helper) == qualification.helper_sha256,
        "Helper differs from qualification.",
    )
    manifest = _manifest(qualification, len(summary_bytes), profile, notices)
    _require(len(manifest) <= _MAX_CONTROL, "Material manifest exceeds its bound.")
    content = {PROFILE: profile, SUMMARY: summary_bytes, **notices}
    with tempfile.TemporaryDirectory(prefix=".gc-native-material-", dir=output.parent) as temporary:
        candidate = Path(temporary) / "component.tar.gz"
        with (
            candidate.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed,
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive,
        ):
            archive.addfile(_info(ROOT, 0, directory=True))
            _add_bytes(archive, "manifest.json", manifest)
            for name in _members(notices):
                if name == HELPER:
                    with helper.open("rb") as source:
                        archive.addfile(_info(f"{ROOT}/{name}", qualification.helper_bytes), source)
                elif name in content:
                    _add_bytes(archive, name, content[name])
                else:
                    archive.addfile(_info(f"{ROOT}/{name}", 0, directory=True))
        with candidate.open("rb") as source:
            _verify_component(source, qualification, profile, notices, None)
        _check_source(root, qualification)
        _require(
            load_qualification(root)[0] == qualification, "Qualification changed during assembly."
        )
        _require(_notices(root) == notices, "Notice source changed during assembly.")
        identity = digest(candidate), candidate.stat().st_size
        if transport is not None:
            _require(identity == (transport.sha256, transport.size), "Candidate transport differs.")
        os.link(candidate, output)
        return identity


def assemble_candidate(root: Path, helper: Path, summary: Path, output: Path) -> tuple[str, int]:
    """Assemble exact native material before adding any transport identity."""
    try:
        return _assemble_candidate(root, helper, summary, output)
    except CandidateError:
        raise
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        message = "Native candidate assembly was refused."
        raise CandidateError(message) from error


def _read_member(reader: UstarReader, size: int, limit: int) -> bytes:
    _require(size <= limit, "Archive control member exceeds its bound.")
    return reader.read(size)


def _expect_member(reader: UstarReader, name: str, *, directory: bool = False) -> tarfile.TarInfo:
    item = reader.next()
    _require(item is not None, "Archive member is missing.")
    if item is None:
        _fail("Archive member is missing.")
    _require(
        item.name == name and (item.isdir() if directory else item.isfile()),
        "Archive member path or type differs.",
    )
    _require(
        item.uid == item.gid == item.mtime == 0
        and item.mode == (0o755 if directory or name == f"{ROOT}/{HELPER}" else 0o644),
        "Archive member metadata differs.",
    )
    return item


def _stream_helper(reader: UstarReader, size: int, output: Path | None) -> str:
    hashed = hashlib.sha256()
    with output.open("xb") if output is not None else Path(os.devnull).open("wb") as target:
        remaining = size
        while remaining:
            chunk = reader.read(min(1024 * 1024, remaining))
            _require(bool(chunk), "Truncated helper.")
            target.write(chunk)
            hashed.update(chunk)
            remaining -= len(chunk)
    return hashed.hexdigest()


def _inspect_component(
    source: BinaryIO,
    qualification: Qualification,
    profile: bytes,
    notices: Mapping[str, bytes],
    helper_output: Path | None,
) -> None:
    with gzip.GzipFile(fileobj=source, mode="rb") as compressed:
        reader = UstarReader(compressed)
        _expect_member(reader, ROOT, directory=True)
        item = _expect_member(reader, f"{ROOT}/manifest.json")
        manifest = _object(_read_member(reader, item.size, _MAX_CONTROL), _MAX_CONTROL)
        _require(
            set(manifest) == {"format", "files"} and manifest.get("format") == FORMAT,
            "Invalid material manifest.",
        )
        files = manifest.get("files")
        _require(isinstance(files, dict), "Invalid material files.")
        if not isinstance(files, dict):
            _fail("Invalid material files.")
        summary_entry = files.get(SUMMARY)
        _require(isinstance(summary_entry, dict), "Qualification summary is missing.")
        if not isinstance(summary_entry, dict):
            _fail("Qualification summary is missing.")
        summary_size = _size(summary_entry.get("bytes"))
        _require(summary_size <= _MAX_CONTROL, "Qualification summary exceeds its bound.")
        expected = json.loads(_manifest(qualification, summary_size, profile, notices))
        for entry in files.values():
            _require(
                isinstance(entry, dict)
                and set(entry) == {"sha256", "bytes"}
                and type(entry.get("bytes")) is int
                and isinstance(entry.get("sha256"), str),
                "Invalid material identity.",
            )
        _require(manifest == expected, "Material manifest differs from qualification.")
        for name in _members(notices):
            directory = name not in {HELPER, PROFILE, SUMMARY, *notices}
            item = _expect_member(reader, f"{ROOT}/{name}", directory=directory)
            if directory:
                continue
            if name == HELPER:
                _require(item.size == qualification.helper_bytes, "Helper size differs.")
                _require(
                    _stream_helper(reader, item.size, helper_output) == qualification.helper_sha256,
                    "Helper digest differs.",
                )
            elif name == SUMMARY:
                _require(item.size == summary_size, "Qualification summary size differs.")
                _summary(_read_member(reader, item.size, _MAX_CONTROL), qualification)
            else:
                data = profile if name == PROFILE else notices[name]
                _require(
                    item.size == len(data)
                    and _read_member(reader, item.size, max(_MAX_PROFILE, _MAX_NOTICE)) == data,
                    "Material member differs.",
                )
        _require(reader.next() is None, "Unexpected archive member.")


def _verify_component(
    source: BinaryIO,
    qualification: Qualification,
    profile: bytes,
    notices: Mapping[str, bytes],
    helper_output: Path | None,
) -> None:
    try:
        _inspect_component(source, qualification, profile, notices, helper_output)
    except CandidateError:
        raise
    except (OSError, EOFError, ValueError, tarfile.TarError) as error:
        message = "Native component archive is malformed."
        raise CandidateError(message) from error


def _select_candidate(root: Path, component: Path, output: Path) -> tuple[Qualification, Transport]:
    qualification, transport = load_qualification(root)
    if transport is None:
        _fail("Native candidate transport is not configured.")
    _check_source(root, qualification)
    profile = _bounded(root / SOURCE_PROFILE, _MAX_PROFILE)
    notices = _notices(root)
    _require(
        not component.is_symlink() and component.is_file(), "Candidate must be a regular file."
    )
    _require(not output.exists() and not output.is_symlink(), "Output already exists.")
    descriptor = os.open(component, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        _require(stat.S_ISREG(before.st_mode), "Candidate must be a regular file.")
        _require(before.st_size == transport.size, "Candidate transport size differs.")
        hashed = hashlib.sha256()
        remaining = transport.size
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            _require(bool(chunk), "Candidate transport is truncated.")
            hashed.update(chunk)
            remaining -= len(chunk)
        _require(
            not source.read(1) and hashed.hexdigest() == transport.sha256,
            "Candidate transport identity differs.",
        )
        source.seek(0)
        with tempfile.TemporaryDirectory(
            prefix=".gc-native-select-", dir=output.parent
        ) as temporary:
            staged = Path(temporary) / HELPER
            _verify_component(source, qualification, profile, notices, staged)
            after = os.fstat(source.fileno())
            _require(
                (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                == (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ),
                "Candidate changed during verification.",
            )
            _check_source(root, qualification)
            _require(
                load_qualification(root) == (qualification, transport),
                "Qualification changed during verification.",
            )
            _require(_notices(root) == notices, "Notice source changed during verification.")
            staged.chmod(0o755)
            os.link(staged, output)
    return qualification, transport


def select_qualified_candidate(
    root: Path, component: Path, output: Path
) -> tuple[Qualification, Transport]:
    """Publish the qualified helper and return the exact material and transport used."""
    try:
        return _select_candidate(root, component, output)
    except CandidateError:
        raise
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        message = "Native candidate selection was refused."
        raise CandidateError(message) from error


def select_candidate(root: Path, component: Path, output: Path) -> Transport:
    """Verify the pinned component and publish only its qualified helper bytes."""
    _, transport = select_qualified_candidate(root, component, output)
    return transport
