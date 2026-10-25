# Copyright (c) 2026

"""Check real package/bundle structure independently of native execution or installation."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import stat
import sys
import tarfile
import zipfile
from email.parser import BytesParser
from io import TextIOWrapper
from pathlib import Path, PurePosixPath

from packaging.version import Version

from tools import build_release, ci_native

RECORD_COLUMNS = 3


class ArtifactError(ValueError):
    """An assembled artifact differs from its declared source or payload bytes."""


def _require(message: str, *, condition: bool) -> None:
    if not condition:
        raise ArtifactError(message)


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def verify_record(wheel: Path) -> None:
    """Check every wheel member against its RECORD hash and byte length."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        _require(
            "Duplicate or unsafe wheel members.",
            condition=len(names) == len(set(names)) and all(_safe_name(n) for n in names),
        )
        files = {info.filename for info in archive.infolist() if not info.is_dir()}
        records = [name for name in files if name.endswith(".dist-info/RECORD")]
        _require("Expected one wheel RECORD.", condition=len(records) == 1)
        record = records[0]
        metadata_root = record.rsplit("/", 1)[0]
        _require(
            "Wheel identity metadata is missing.",
            condition=(f"{metadata_root}/WHEEL" in files and f"{metadata_root}/METADATA" in files),
        )
        fields = BytesParser().parsebytes(archive.read(f"{metadata_root}/WHEEL"))
        _require(
            "Expected a generic pure-Python wheel.",
            condition=(
                fields["Wheel-Version"] == "1.0"
                and fields["Root-Is-Purelib"] == "true"
                and fields.get_all("Tag") == ["py3-none-any"]
            ),
        )
        seen = set()
        with (
            archive.open(record) as raw,
            TextIOWrapper(raw, encoding="utf-8", newline="") as stream,
        ):
            for row in csv.reader(stream):
                _require("Invalid wheel RECORD row.", condition=len(row) == RECORD_COLUMNS)
                name, declared_hash, declared_size = row
                _require(
                    "Unlisted or duplicate RECORD entry.",
                    condition=name in files and name not in seen,
                )
                seen.add(name)
                if name == record:
                    _require(
                        "RECORD cannot hash itself.", condition=declared_hash == declared_size == ""
                    )
                    continue
                info = archive.getinfo(name)
                _require(
                    "Wheel member is a symbolic link.",
                    condition=not stat.S_ISLNK(info.external_attr >> 16),
                )
                result = hashlib.sha256()
                size = 0
                with archive.open(name) as source:
                    while block := source.read(1024 * 1024):
                        result.update(block)
                        size += len(block)
                actual = "sha256=" + base64.urlsafe_b64encode(result.digest()).decode().rstrip("=")
                _require(
                    f"Wheel RECORD differs for {name}.",
                    condition=declared_hash == actual and declared_size == str(size),
                )
        _require("Wheel files are missing from RECORD.", condition=seen == files)


def verify_sdist(sdist: Path, root: Path, version: str) -> None:
    """Bind source-distribution profile, metadata and legal payload to the selected checkout."""
    prefix = f"codex_session_gc-{version}"
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        _require(
            "Duplicate, linked or unsafe source-distribution member.",
            condition=(
                len(names) == len(set(names))
                and all(
                    _safe_name(item.name)
                    and (item.isdir() or item.isfile())
                    and PurePosixPath(item.name).parts[0] == prefix
                    for item in archive.getmembers()
                )
            ),
        )
        expected = build_release.license_payload(root)
        expected[str(ci_native.PROFILE)] = (root / ci_native.PROFILE).read_bytes()
        expected["pyproject.toml"] = (root / "pyproject.toml").read_bytes()
        expected["uv.lock"] = (root / "uv.lock").read_bytes()
        for name, data in expected.items():
            source = archive.extractfile(f"{prefix}/{name}")
            if source is None:
                message = f"Missing source-distribution file: {name}"
                raise ArtifactError(message)
            with source:
                _require(
                    f"Source distribution differs for {name}.", condition=source.read() == data
                )
        source = archive.extractfile(f"{prefix}/PKG-INFO")
        if source is None:
            message = "Source distribution metadata is unavailable."
            raise ArtifactError(message)
        with source:
            metadata = BytesParser().parsebytes(source.read())
        _require(
            "Source distribution version differs.",
            condition=Version(str(metadata["Version"])) == Version(version),
        )


def inspect_release(
    root: Path,
    sdist: Path,
    wheel: Path,
    bundle: Path,
    helper: Path,
) -> dict[str, str | int]:
    """Verify source/package/payload byte agreement without launching installed code."""
    version, _, _, raw_profile, contract = build_release.current_artifacts(wheel, root)
    verify_record(wheel)
    verify_sdist(sdist, root, version)
    helper_hash = ci_native.digest(helper)
    _require(
        "Supplied helper differs from the candidate profile.",
        condition=contract.get("binary_sha256") == list(bytes.fromhex(helper_hash)),
    )
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    _require(
        "Bundle raw profile identity differs.",
        condition=manifest["wheel_contract_sha256"] == hashlib.sha256(raw_profile).hexdigest(),
    )
    payload = manifest["payload"]
    _require("Bundle payload must be an object.", condition=isinstance(payload, dict))
    names = {path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()}
    expected = (
        set(build_release.DOCUMENTS)
        | set(build_release.license_payload(root))
        | {
            wheel.name,
            "codex-session-gc-native",
            "requirements-runtime.txt",
            "install-requirements.txt",
        }
    )
    _require(
        "Bundle payload file set differs.",
        condition=set(payload) == expected == names - {"manifest.json"},
    )
    for name, entry in payload.items():
        path = bundle / name
        _require(
            f"Bundle payload path is unsafe: {name}",
            condition=_safe_name(name) and not path.is_symlink(),
        )
        _require(
            f"Bundle payload differs for {name}.",
            condition=entry["sha256"] == ci_native.digest(path)
            and entry["bytes"] == path.stat().st_size,
        )
    _require(
        "Bundle contains a different wheel or helper.",
        condition=(
            ci_native.digest(bundle / wheel.name) == ci_native.digest(wheel)
            and ci_native.digest(bundle / "codex-session-gc-native") == helper_hash
        ),
    )
    _require(
        "Bundle provenance differs from the selected source.",
        condition=(
            manifest["source_provenance"] == json.loads((root / ci_native.PROVENANCE).read_bytes())
        ),
    )
    return {
        "acceptance": (
            "structure_only; native execution and installed interfaces require separate gates"
        ),
        "package_version": version,
        "sdist_sha256": ci_native.digest(sdist),
        "wheel_sha256": ci_native.digest(wheel),
        "helper_sha256": helper_hash,
        "profile_sha256": hashlib.sha256(raw_profile).hexdigest(),
        "bundle_payload_files": len(payload),
    }


def main() -> None:
    """Check completed real artifacts after the existing release assembler finishes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    for name in ("sdist", "wheel", "bundle", "helper"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        ci_native.verify_source(args.root)
        evidence = inspect_release(args.root, args.sdist, args.wheel, args.bundle, args.helper)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        parser.exit(1, f"Release structure refused: {error}\n")
    sys.stdout.write(json.dumps(evidence, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
