# Copyright (c) 2026

"""Artifact-bound reuse checks with tiny synthetic qualification inputs."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from tools import ci_native

if TYPE_CHECKING:
    from pathlib import Path


_INPUTS = (
    "native/helper/Cargo.toml",
    "native/helper/Cargo.lock",
    "native/helper/rust-toolchain.toml",
    "native/helper/src/main.rs",
    "native/patches/cleanup.patch",
)


def _equal(actual: object, expected: object) -> None:
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, observed {actual!r}")


def _wheel(profile: bytes) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(ci_native.WHEEL_PROFILE, profile)
    return stream.getvalue()


def _bundle(root: Path, helper: bytes, wheel: bytes) -> Path:
    path = root / "bundle.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (
            ("release/codex-session-gc-native", helper),
            ("release/package.whl", wheel),
        ):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return path


@pytest.fixture
def qualified(tmp_path: Path) -> tuple[Path, Path, ci_native.QualifiedRelease]:
    """Bind synthetic release/source bytes; these fixtures qualify no real artifact."""
    root = tmp_path / "source"
    for name in _INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"synthetic {name}\n")
    manifest = {
        "archive_sha256": "a" * 64,
        "inputs": [
            {
                "path": name,
                "bytes": (root / name).stat().st_size,
                "sha256": ci_native.digest(root / name),
            }
            for name in _INPUTS
        ],
    }
    manifest_sha = hashlib.sha256(
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    ).hexdigest()
    provenance = root / ci_native.PROVENANCE
    provenance.parent.mkdir(parents=True)
    provenance.write_text(
        json.dumps(
            {
                "current_delivery_inputs": {
                    "manifest": manifest,
                    "manifest_sha256": manifest_sha,
                }
            }
        )
    )
    helper = b"synthetic helper, never executed"
    helper_sha = hashlib.sha256(helper).hexdigest()
    profile = json.dumps({"binary_sha256": list(bytes.fromhex(helper_sha))}).encode()
    path = root / ci_native.PROFILE
    path.parent.mkdir(parents=True)
    path.write_bytes(profile)
    wheel = _wheel(profile)
    bundle = _bundle(tmp_path, helper, wheel)
    pin = ci_native.QualifiedRelease(
        "https://example.invalid/fixture",
        ci_native.digest(bundle),
        bundle.stat().st_size,
        helper_sha,
        hashlib.sha256(profile).hexdigest(),
        manifest_sha,
        hashlib.sha256(wheel).hexdigest(),
        "release",
        "package.whl",
    )
    return root, bundle, pin


def test_matching_source_and_full_release_select_only_qualified_helper(
    qualified: tuple[Path, Path, ci_native.QualifiedRelease],
    tmp_path: Path,
) -> None:
    """The selected executable has the trusted helper hash; source and bundle are untouched."""
    root, bundle, pin = qualified
    before = ci_native.digest(bundle)
    output = tmp_path / "helper"
    evidence = ci_native.select_helper(root, bundle, output, pin)
    _equal(ci_native.digest(output), pin.helper_sha256)
    _equal(evidence["profile_sha256"], pin.profile_sha256)
    _equal(evidence["native_input_files"], len(_INPUTS))
    _equal(ci_native.digest(bundle), before)
    _equal(output.stat().st_mode & 0o111 != 0, expected=True)


@pytest.mark.parametrize("name", _INPUTS)
def test_each_changed_native_input_refuses_reuse(
    qualified: tuple[Path, Path, ci_native.QualifiedRelease],
    name: str,
) -> None:
    """Every bound source input matters independently of Python application changes."""
    root, _, pin = qualified
    (root / name).write_bytes(b"changed")
    with pytest.raises(ci_native.ReuseError, match="Native input differs"):
        ci_native.verify_source(root, pin)


@pytest.mark.parametrize("name", ["native/helper/build.rs", "native/helper/src/extra.rs"])
def test_new_native_inputs_refuse_reuse(
    qualified: tuple[Path, Path, ci_native.QualifiedRelease],
    name: str,
) -> None:
    """An unlisted build script or source cannot inherit the old qualification."""
    root, _, pin = qualified
    (root / name).write_text("new input")
    with pytest.raises(ci_native.ReuseError, match="file set"):
        ci_native.verify_source(root, pin)


def test_rewritten_current_manifest_cannot_bless_changed_source(
    qualified: tuple[Path, Path, ci_native.QualifiedRelease],
) -> None:
    """A self-consistent new source manifest still differs from the reviewed release anchor."""
    root, _, pin = qualified
    path = root / ci_native.PROVENANCE
    value = json.loads(path.read_bytes())
    manifest = value["current_delivery_inputs"]["manifest"]
    manifest["archive_sha256"] = "b" * 64
    value["current_delivery_inputs"]["manifest_sha256"] = hashlib.sha256(
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    ).hexdigest()
    path.write_text(json.dumps(value))
    with pytest.raises(ci_native.ReuseError, match="manifest changed"):
        ci_native.verify_source(root, pin)


def test_raw_profile_edit_requires_separate_qualification(
    qualified: tuple[Path, Path, ci_native.QualifiedRelease],
) -> None:
    """Equivalent JSON with different bytes cannot retain raw-profile identity."""
    root, _, pin = qualified
    path = root / ci_native.PROFILE
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ci_native.ReuseError, match="Raw profile changed"):
        ci_native.verify_source(root, pin)


def test_transport_hash_checked_before_archive_use(
    qualified: tuple[Path, Path, ci_native.QualifiedRelease],
) -> None:
    """Truncated or altered downloads cannot select an alternate helper."""
    _, bundle, pin = qualified
    bundle.write_bytes(b"not an archive")
    with pytest.raises(ci_native.ReuseError, match="bundle"):
        ci_native.verify_bundle(bundle, pin)


def test_inner_helper_and_profile_identities_are_checked(
    qualified: tuple[Path, Path, ci_native.QualifiedRelease],
    tmp_path: Path,
) -> None:
    """Full transport hashing does not replace the independently bound member identities."""
    root, _, pin = qualified
    bundle = _bundle(tmp_path, b"wrong helper", _wheel((root / ci_native.PROFILE).read_bytes()))
    transport = replace(
        pin, bundle_sha256=ci_native.digest(bundle), bundle_bytes=bundle.stat().st_size
    )
    with pytest.raises(ci_native.ReuseError, match="helper hash"):
        ci_native.verify_bundle(bundle, transport)
    wrong_wheel = _wheel(b"different profile")
    bundle = _bundle(tmp_path, b"synthetic helper, never executed", wrong_wheel)
    transport = replace(
        pin,
        bundle_sha256=ci_native.digest(bundle),
        bundle_bytes=bundle.stat().st_size,
        wheel_sha256=hashlib.sha256(wrong_wheel).hexdigest(),
    )
    with pytest.raises(ci_native.ReuseError, match="raw profile"):
        ci_native.verify_bundle(bundle, transport)


def test_existing_helper_output_is_preserved(
    qualified: tuple[Path, Path, ci_native.QualifiedRelease],
    tmp_path: Path,
) -> None:
    """Artifact preparation never overwrites an existing consumer."""
    root, bundle, pin = qualified
    output = tmp_path / "helper"
    output.write_bytes(b"existing consumer")
    with pytest.raises(ci_native.ReuseError, match="new helper output"):
        ci_native.select_helper(root, bundle, output, pin)
    _equal(output.read_bytes(), b"existing consumer")
