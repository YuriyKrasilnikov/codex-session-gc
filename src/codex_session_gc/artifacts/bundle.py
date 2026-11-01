# Copyright (c) 2026

"""Shared typed manifest for one source-qualified install bundle."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NoReturn

RELEASE_FORMAT = "codex-session-gc-release-v2"
MANIFEST_MAX_BYTES = 16 * 1024 * 1024
CONTROL_FILE_MAX_BYTES = 1024 * 1024
WHEEL_METADATA_MAX_BYTES = 1024 * 1024
WHEEL_CONTRACT_MAX_BYTES = 16 * 1024 * 1024
WHEEL_DIRECTORY_MAX_BYTES = 8 * 1024 * 1024
WHEEL_MAX_ENTRIES = 10_000


class BundleManifestError(ValueError):
    """A bundle manifest is malformed or cannot describe this release format."""


@dataclass(frozen=True, slots=True)
class BundleFile:
    """Declared byte identity of a regular bundle member."""

    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class BundleManifest:
    """Validated bundle identity and declared regular-file members."""

    version: str
    files: dict[str, BundleFile]
    raw: dict[str, object]


def _fail(message: str) -> NoReturn:
    raise BundleManifestError(message)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("Duplicate manifest key.")
        result[key] = value
    return result


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in (".", "..") for part in path.parts)
        and "\\" not in value
        and value != "manifest.json"
    )


def _validate_control_sizes(files: dict[str, BundleFile]) -> None:
    for name in (
        "native/build-inputs/provenance.json",
        "install-requirements.txt",
        "requirements-runtime.txt",
    ):
        if files[name].size > CONTROL_FILE_MAX_BYTES:
            _fail("A control file exceeds its memory bound.")


def parse_manifest(data: bytes) -> BundleManifest:
    """Validate the complete bounded v2 manifest, without inspecting payload bytes."""
    if not data or len(data) > MANIFEST_MAX_BYTES:
        _fail("Manifest size is outside the declared bound.")
    try:
        raw: object = json.loads(data, object_pairs_hook=_object)
    except (UnicodeDecodeError, ValueError) as error:
        message = "Invalid bundle manifest JSON."
        raise BundleManifestError(message) from error
    if not isinstance(raw, dict) or set(raw) != {
        "format",
        "package_version",
        "requires_python",
        "native_source_identity",
        "wire_version",
        "wheel_contract_sha256",
        "wheel_source_map_sha256",
        "source_provenance",
        "payload",
    }:
        _fail("Unexpected manifest fields.")
    version = raw.get("package_version")
    if (
        raw.get("format") != RELEASE_FORMAT
        or not isinstance(version, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version) is None
        or ".." in version
        or not isinstance(raw.get("requires_python"), str)
        or not raw.get("requires_python")
        or not isinstance(raw.get("native_source_identity"), str)
        or not raw.get("native_source_identity")
        or type(raw.get("wire_version")) is not int
        or not _digest(raw.get("wheel_contract_sha256"))
        or not _digest(raw.get("wheel_source_map_sha256"))
        or not isinstance(raw.get("source_provenance"), dict)
    ):
        _fail("Invalid manifest identity.")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        _fail("Manifest payload must be an object.")
    files = {}
    for name, entry in payload.items():
        if (
            not isinstance(name, str)
            or not _path(name)
            or not isinstance(entry, dict)
            or set(entry) != {"sha256", "bytes"}
            or not _digest(entry.get("sha256"))
            or type(entry.get("bytes")) is not int
            or entry["bytes"] < 0
        ):
            _fail("Invalid manifest payload entry.")
        files[name] = BundleFile(entry["sha256"], entry["bytes"])
    wheel = f"codex_session_gc-{version}-py3-none-any.whl"
    if (
        not {
            wheel,
            "codex-session-gc-native",
            "native/build-inputs/provenance.json",
            "install-requirements.txt",
            "requirements-runtime.txt",
        }
        <= files.keys()
    ):
        _fail("Required manifest payload is absent.")
    _validate_control_sizes(files)
    return BundleManifest(version, files, raw)
