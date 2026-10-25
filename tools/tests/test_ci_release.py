# Copyright (c) 2026

"""Structural release checks with synthetic package bytes, never runtime qualification."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest

from tests.test_release import _inputs, _run
from tools import build_release, ci_native, ci_release

if TYPE_CHECKING:
    from pathlib import Path


DIST_INFO = "codex_session_gc-1.2.3.dist-info"


def _equal(actual: object, expected: object) -> None:
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, observed {actual!r}")


def _write_wheel(path: Path, payload: dict[str, bytes]) -> None:
    records = io.StringIO(newline="")
    writer = csv.writer(records)
    for name, data in payload.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        writer.writerow([name, f"sha256={encoded}", len(data)])
    writer.writerow([f"{DIST_INFO}/RECORD", "", ""])
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
        archive.writestr(f"{DIST_INFO}/RECORD", records.getvalue())


def _sdist(root: Path, path: Path) -> None:
    files = build_release.license_payload(root)
    files.update(
        {
            str(ci_native.PROFILE): (root / ci_native.PROFILE).read_bytes(),
            "pyproject.toml": (root / "pyproject.toml").read_bytes(),
            "uv.lock": (root / "uv.lock").read_bytes(),
            "PKG-INFO": b"Metadata-Version: 2.4\nName: codex-session-gc\nVersion: 1.2.3\n",
        }
    )
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"codex_session_gc-1.2.3/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


@pytest.fixture
def artifacts(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    """Complete the existing synthetic assembler fixture with RECORD and an sdist."""
    root = tmp_path / "source"
    root.mkdir()
    tool, wheel, helper, environment = _inputs(root)
    (root / "uv.lock").write_text("synthetic lock\n")
    with zipfile.ZipFile(wheel) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    payload[f"{DIST_INFO}/WHEEL"] = (
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    _write_wheel(wheel, payload)
    bundle = root / "bundle"
    result = _run(tool, wheel, helper, bundle, environment)
    _equal(result.returncode, 0)
    sdist = root / "codex_session_gc-1.2.3.tar.gz"
    _sdist(root, sdist)
    return root, sdist, wheel, bundle, helper


def test_matching_artifact_structure_is_not_a_runtime_qualification(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Bind all structural bytes while retaining the installed/native acceptance boundary."""
    root, sdist, wheel, bundle, helper = artifacts
    result = ci_release.inspect_release(root, sdist, wheel, bundle, helper)
    _equal(result["profile_sha256"], ci_native.digest(root / ci_native.PROFILE))
    _equal(result["helper_sha256"], ci_native.digest(helper))
    _equal(str(result["acceptance"]).startswith("structure_only"), expected=True)


def test_modified_wheel_record_is_refused(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """A changed file cannot inherit the RECORD hash of its prior bytes."""
    _, _, wheel, _, _ = artifacts
    with zipfile.ZipFile(wheel) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    payload[f"{DIST_INFO}/WHEEL"] += b"Generator: changed\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
    with pytest.raises(ci_release.ArtifactError, match="RECORD differs"):
        ci_release.verify_record(wheel)


def test_sdist_profile_and_legal_bytes_must_match_source(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Source-distribution copies retain exact profile and attribution bytes."""
    root, sdist, _, _, _ = artifacts
    (root / "LICENSE").write_text("changed license\n")
    with pytest.raises(ci_release.ArtifactError, match="Source distribution differs"):
        ci_release.verify_sdist(sdist, root, "1.2.3")


@pytest.mark.parametrize("name", ["codex-session-gc-native", "requirements-runtime.txt", "NOTICE"])
def test_changed_payload_is_refused(
    artifacts: tuple[Path, Path, Path, Path, Path],
    name: str,
) -> None:
    """Bundle hashes protect executable, dependency requirements and legal payload alike."""
    root, sdist, wheel, bundle, helper = artifacts
    (bundle / name).write_bytes(b"changed")
    with pytest.raises(ci_release.ArtifactError, match="Bundle payload differs"):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)


def test_extra_bundle_file_is_not_selected_by_directory_glob(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Only the assembler's declared payload can enter the matched bundle."""
    root, sdist, wheel, bundle, helper = artifacts
    (bundle / "extra").write_bytes(b"unreviewed")
    with pytest.raises(ci_release.ArtifactError, match="file set"):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)


def test_self_consistent_replacement_wheel_still_differs_from_candidate(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Updating a copied-member hash cannot authorize another candidate wheel."""
    root, sdist, wheel, bundle, helper = artifacts
    path = bundle / wheel.name
    path.write_bytes(b"different wheel")
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["payload"][wheel.name] = {
        "sha256": ci_native.digest(path),
        "bytes": path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ci_release.ArtifactError, match="different wheel or helper"):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)
