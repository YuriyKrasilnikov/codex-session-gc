# Copyright (c) 2026

"""Native components are admitted only against source qualification pins."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.artifacts.ustar import UstarReader
from tools import ci_native, native_candidate

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def candidate(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Create a reviewed synthetic qualification, helper and component."""
    root = tmp_path / "source"
    names = (
        "native/helper/Cargo.toml",
        "native/helper/Cargo.lock",
        "native/helper/rust-toolchain.toml",
        "native/helper/src/main.rs",
        "native/patches/test.patch",
    )
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"synthetic {name}\n")
    for name in ("LICENSE", "NOTICE"):
        (root / name).write_text(f"synthetic {name}\n")
    (root / "pyproject.toml").write_text('[project]\nlicense-files = ["LICENSE", "NOTICE"]\n')
    manifest = {
        "inputs": [
            {
                "path": name,
                "bytes": (root / name).stat().st_size,
                "sha256": ci_native.digest(root / name),
            }
            for name in names
        ],
    }
    input_sha = hashlib.sha256(
        (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    ).hexdigest()
    helper = tmp_path / "helper"
    helper.write_bytes(b"synthetic native helper")
    helper_sha = ci_native.digest(helper)
    profile = root / ci_native.PROFILE
    profile.parent.mkdir(parents=True)
    qualification_identity = "e" * 64
    partitions = [
        {"kind": "native-contract", "cases": 2, "exit_code": 0},
        {"kind": "attachment", "cases": 1, "exit_code": 0},
    ]
    evidence = base64.b64encode(
        json.dumps(
            {
                "qualification_identity": qualification_identity,
                "compiled_source_manifest_sha256": input_sha,
                "helper_sha256": helper_sha,
                "helper_bytes": helper.stat().st_size,
                "partitions": partitions,
            }
        ).encode(),
    ).decode()
    profile.write_text(
        json.dumps(
            {
                "binary_sha256": list(bytes.fromhex(helper_sha)),
                "qualification_evidence": {
                    "content_type": (
                        "application/vnd.codex-session-gc.direct-native-qualification+json"
                    ),
                    "version": 1,
                    "data_base64": evidence,
                },
            }
        )
    )
    profile_sha = ci_native.digest(profile)
    checks = [
        hashlib.sha256(
            json.dumps(partition, sort_keys=True, separators=(",", ":")).encode(),
        ).hexdigest()
        for partition in partitions
    ]
    summary = tmp_path / "qualification.json"
    summary.write_text(
        json.dumps(
            {
                "format": native_candidate.SUMMARY_FORMAT,
                "native_input_manifest_sha256": input_sha,
                "profile_sha256": profile_sha,
                "helper_sha256": helper_sha,
                "helper_bytes": helper.stat().st_size,
                "qualification_identity": qualification_identity,
                "checks": checks,
            }
        )
    )
    provenance = root / ci_native.PROVENANCE
    provenance.parent.mkdir(parents=True)
    provenance.write_text(
        json.dumps(
            {
                "build_qualification": {
                    "helper_sha256": helper_sha,
                    "helper_bytes": helper.stat().st_size,
                    "contract_sha256": profile_sha,
                    "qualification_identity": qualification_identity,
                    "partitions": partitions,
                    "native_candidate": {"summary_sha256": ci_native.digest(summary)},
                },
                "current_delivery_inputs": {"manifest": manifest, "manifest_sha256": input_sha},
            }
        )
    )
    component = tmp_path / "component.tar.gz"
    sha, size = native_candidate.assemble_candidate(root, helper, summary, component)
    value = json.loads(provenance.read_bytes())
    value["build_qualification"]["native_candidate"]["transport"] = {
        "asset_id": 123,
        "sha256": sha,
        "bytes": size,
    }
    provenance.write_text(json.dumps(value))
    return root, component, helper, summary


def _update_transport(root: Path, component: Path) -> None:
    path = root / ci_native.PROVENANCE
    value = json.loads(path.read_bytes())
    transport = value["build_qualification"]["native_candidate"]["transport"]
    transport["sha256"] = ci_native.digest(component)
    transport["bytes"] = component.stat().st_size
    path.write_text(json.dumps(value))


def _rewrite(component: Path, *, missing: bool) -> None:
    with tarfile.open(component, "r:gz") as archive:
        entries = []
        for item in archive.getmembers():
            stream = archive.extractfile(item) if item.isfile() else None
            entries.append((item, stream.read() if stream is not None else b""))
    with tarfile.open(component, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for item, data in entries:
            if item.name.endswith("/NOTICE"):
                if missing:
                    continue
                item.name = "native-material/../NOTICE"
            archive.addfile(item, io.BytesIO(data) if item.isfile() else None)


def test_exact_component_selects_qualified_helper(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """The transported bytes select exactly the qualified helper and locator."""
    root, component, helper, _ = candidate
    output = tmp_path / "selected"
    transport = native_candidate.select_candidate(root, component, output)
    expected_asset = 123
    if output.read_bytes() != helper.read_bytes() or transport.asset_id != expected_asset:
        raise AssertionError
    if transport.api_url != (
        "https://api.github.com/repos/YuriyKrasilnikov/codex-session-gc/releases/assets/123"
    ):
        raise AssertionError


@pytest.mark.parametrize(
    "name", ["helper", "profile", "inputs", "summary", "transport_size", "asset_id"]
)
def test_changed_qualification_refuses(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path, name: str
) -> None:
    """Every qualified material and transport identity is independently required."""
    root, component, _, _ = candidate
    path = root / ci_native.PROVENANCE
    value = json.loads(path.read_bytes())
    build = value["build_qualification"]
    if name == "helper":
        build["helper_sha256"] = "a" * 64
    elif name == "profile":
        build["contract_sha256"] = "b" * 64
    elif name == "inputs":
        value["current_delivery_inputs"]["manifest_sha256"] = "c" * 64
    elif name == "summary":
        build["native_candidate"]["summary_sha256"] = "d" * 64
    elif name == "transport_size":
        build["native_candidate"]["transport"]["bytes"] += 1
    else:
        build["native_candidate"]["transport"]["asset_id"] = 0
    path.write_text(json.dumps(value))
    with pytest.raises(native_candidate.CandidateError, match=r"."):
        native_candidate.select_candidate(root, component, tmp_path / "selected")


@pytest.mark.parametrize("missing", [False, True])
def test_bad_archive_path_or_missing_member_refuses(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path, *, missing: bool
) -> None:
    """Even an exact transport pin cannot authorize an invalid archive layout."""
    root, component, _, _ = candidate
    _rewrite(component, missing=missing)
    _update_transport(root, component)
    with pytest.raises(native_candidate.CandidateError, match=r"."):
        native_candidate.select_candidate(root, component, tmp_path / "selected")


def test_change_during_observation_refuses(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation after transport hashing cannot publish the helper."""
    root, component, _, _ = candidate
    actual = UstarReader.next
    changed = False

    def mutate(reader: UstarReader) -> tarfile.TarInfo | None:
        nonlocal changed
        result = actual(reader)
        if not changed:
            changed = True
            with component.open("r+b") as stream:
                stream.seek(0, 2)
                stream.write(b"changed")
        return result

    monkeypatch.setattr(UstarReader, "next", mutate)
    output = tmp_path / "selected"
    with pytest.raises(native_candidate.CandidateError, match=r"."):
        native_candidate.select_candidate(root, component, output)
    if output.exists():
        raise AssertionError


def test_unconfigured_candidate_refuses(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """A source without the reviewed candidate record cannot activate this route."""
    root, component, _, _ = candidate
    path = root / ci_native.PROVENANCE
    value = json.loads(path.read_bytes())
    del value["build_qualification"]["native_candidate"]
    path.write_text(json.dumps(value))
    with pytest.raises(native_candidate.CandidateError, match="absent"):
        native_candidate.select_candidate(root, component, tmp_path / "selected")


@pytest.mark.parametrize("asset_id", [0, -1, True, "123"])
def test_invalid_asset_locator_refuses(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path, asset_id: object
) -> None:
    """Only positive integer asset IDs can locate reviewed material."""
    root, component, _, _ = candidate
    path = root / ci_native.PROVENANCE
    value = json.loads(path.read_bytes())
    value["build_qualification"]["native_candidate"]["transport"]["asset_id"] = asset_id
    path.write_text(json.dumps(value))
    with pytest.raises(native_candidate.CandidateError, match="positive"):
        native_candidate.select_candidate(root, component, tmp_path / "selected")


@pytest.mark.parametrize("kind", ["missing_provenance", "missing_profile", "wrong_profile"])
def test_qualification_identity_must_match_profile(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path, kind: str
) -> None:
    """The reviewed qualification ID must appear unchanged in the raw profile."""
    root, component, _, _ = candidate
    path = root / ci_native.PROVENANCE
    provenance = json.loads(path.read_bytes())
    if kind == "missing_provenance":
        del provenance["build_qualification"]["qualification_identity"]
    else:
        profile_path = root / ci_native.PROFILE
        profile = json.loads(profile_path.read_bytes())
        evidence = profile["qualification_evidence"]
        payload = json.loads(base64.b64decode(evidence["data_base64"]))
        if kind == "missing_profile":
            del payload["qualification_identity"]
        else:
            payload["qualification_identity"] = "f" * 64
        evidence["data_base64"] = base64.b64encode(json.dumps(payload).encode()).decode()
        profile_path.write_text(json.dumps(profile))
        provenance["build_qualification"]["contract_sha256"] = ci_native.digest(profile_path)
    path.write_text(json.dumps(provenance))
    with pytest.raises(native_candidate.CandidateError, match=r"identity"):
        native_candidate.select_candidate(root, component, tmp_path / "selected")


def test_summary_cannot_rewrite_qualification_identity(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """A public summary cannot claim a different qualified native identity."""
    root, _, helper, summary = candidate
    data = json.loads(summary.read_bytes())
    data["qualification_identity"] = "f" * 64
    summary.write_text(json.dumps(data))
    path = root / ci_native.PROVENANCE
    value = json.loads(path.read_bytes())
    value["build_qualification"]["native_candidate"] = {"summary_sha256": ci_native.digest(summary)}
    path.write_text(json.dumps(value))
    with pytest.raises(native_candidate.CandidateError, match="summary identity"):
        native_candidate.assemble_candidate(root, helper, summary, tmp_path / "bad.tar.gz")


def test_summary_checks_follow_reviewed_partitions(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """A summary cannot retain different qualification evidence than provenance."""
    root, component, _, _ = candidate
    path = root / ci_native.PROVENANCE
    value = json.loads(path.read_bytes())
    value["build_qualification"]["partitions"][0]["cases"] += 1
    path.write_text(json.dumps(value))
    with pytest.raises(native_candidate.CandidateError, match="checks"):
        native_candidate.select_candidate(root, component, tmp_path / "selected")


def test_repacked_wrong_helper_cannot_inherit_source_pin(
    candidate: tuple[Path, Path, Path, Path], tmp_path: Path
) -> None:
    """A new transport hash cannot authorize different native helper bytes."""
    root, component, _, _ = candidate
    with tarfile.open(component, "r:gz") as archive:
        members = []
        for item in archive.getmembers():
            stream = archive.extractfile(item) if item.isfile() else None
            data = stream.read() if stream is not None else b""
            if item.name.endswith("/codex-session-gc-native"):
                data = bytes([data[0] ^ 1]) + data[1:]
            members.append((item, data))
    with tarfile.open(component, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for item, data in members:
            archive.addfile(item, io.BytesIO(data) if item.isfile() else None)
    _update_transport(root, component)
    with pytest.raises(native_candidate.CandidateError, match="Helper digest"):
        native_candidate.select_candidate(root, component, tmp_path / "selected")
