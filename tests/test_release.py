# Copyright (c) 2026
# ruff: noqa: S101, S603, PLR2004

"""Release assembly preserves artifact identities and refuses mismatched inputs."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parents[1]
_DOCUMENTS = (
    "README.md",
    "docs/contract.md",
    "docs/recovery.md",
    "docs/architecture.md",
    "docs/compatibility.md",
    "native/README.md",
    "native/build-inputs/provenance.json",
)
_LICENSE_FILES = (
    "LICENSE",
    "NOTICE",
    "third_party/NATIVE_NOTICES.txt",
    "third_party/RUST_LIBRARY_NOTICES.html",
    "third_party/NOTICE_SOURCES.json",
)


def _inputs(root: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    """Construct a tiny wheel and external lock-export producer without package builds."""
    (root / "tools").mkdir()
    tool = root / "tools/build_release.py"
    tool.write_bytes((_PROJECT / "tools/build_release.py").read_bytes())
    for name in _DOCUMENTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "fixture documentation\n" if path.suffix != ".json" else '{"commit":"test"}\n'
        )
    for name in _LICENSE_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture license attribution\n" if path.suffix != ".json" else "{}\n")
    helper = root / "helper"
    helper.write_bytes(b"synthetic qualified helper bytes")
    wheel = root / "codex_session_gc-1.2.3-py3-none-any.whl"
    contract = {
        "binary_sha256": list(hashlib.sha256(helper.read_bytes()).digest()),
        "source_identity": "synthetic-source",
        "wire_version": 6,
    }
    source_contract = root / "src/codex_session_gc/execution/closed_work_contract.json"
    source_contract.parent.mkdir(parents=True)
    source_contract.write_text(json.dumps(contract))
    (root / "pyproject.toml").write_text(
        '[project]\nname="codex-session-gc"\nversion="1.2.3"\n'
        'requires-python=">=3.14"\ndependencies=["fixture-dependency==1.0"]\n'
        'license="Apache-2.0"\n' + "license-files=" + json.dumps(list(_LICENSE_FILES)) + "\n"
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "codex_session_gc-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: codex-session-gc\n"
            "Version: 1.2.3\nRequires-Python: >=3.14\n"
            "Requires-Dist: fixture-dependency==1.0\n"
            "License-Expression: Apache-2.0\n"
            + "".join(f"License-File: {name}\n" for name in _LICENSE_FILES),
        )
        archive.writestr(
            "codex_session_gc/execution/closed_work_contract.json", json.dumps(contract)
        )
        for name in _LICENSE_FILES:
            archive.writestr(
                "codex_session_gc-1.2.3.dist-info/licenses/" + name, (root / name).read_bytes()
            )
    binary = root / "bin"
    binary.mkdir()
    uv = binary / "uv"
    locked = "fixture-dependency==1.0 --hash=sha256:" + "a" * 64 + "\n"
    uv.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({locked!r})\n")
    uv.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = str(binary) + os.pathsep + os.defpath
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return tool, wheel, helper, environment


def _run(
    tool: Path, wheel: Path, helper: Path, output: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(tool),
            "--wheel",
            str(wheel),
            "--helper",
            str(helper),
            "--output",
            str(output),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_bundle_binds_payload_and_local_wheel_requirement(tmp_path: Path) -> None:
    """Artifact version, hashes, permissions and deterministic manifests survive assembly."""
    tool, wheel, helper, env = _inputs(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    for output in (first, second):
        result = _run(tool, wheel, helper, output, env)
        assert result.returncode == 0, result.stderr
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    manifest = json.loads((first / "manifest.json").read_bytes())
    assert manifest["package_version"] == "1.2.3"
    assert manifest["requires_python"] == ">=3.14"
    assert manifest["native_source_identity"] == "synthetic-source"
    expected = {
        *_DOCUMENTS,
        *_LICENSE_FILES,
        wheel.name,
        "codex-session-gc-native",
        "requirements-runtime.txt",
        "install-requirements.txt",
    }
    assert set(manifest["payload"]) == expected
    for name, identity in manifest["payload"].items():
        raw = (first / name).read_bytes()
        assert identity == {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    assert (first / "install-requirements.txt").read_text() == (
        "-r requirements-runtime.txt\n"
        f"./{wheel.name} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n"
    )
    assert (first / "codex-session-gc-native").stat().st_mode & 0o777 == 0o755


def test_wrong_helper_refuses_without_output(tmp_path: Path) -> None:
    """An otherwise valid wheel cannot authorize different native bytes."""
    tool, wheel, helper, env = _inputs(tmp_path)
    helper.write_bytes(b"different helper")
    output = tmp_path / "release"
    result = _run(tool, wheel, helper, output, env)
    assert result.returncode == 2
    assert "qualified native digest" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "mismatch",
    [
        "version",
        "python",
        "dependencies",
        "raw-profile",
        "stale-pair",
        "license-expression",
        "license-file",
        "notice-bytes",
        "source-notice-change",
    ],
)
def test_wheel_must_match_the_selected_source_before_assembly(
    tmp_path: Path, mismatch: str
) -> None:
    """Matching historical artifacts cannot be bundled with a different current source."""
    tool, wheel, helper, env = _inputs(tmp_path)
    contract_name = "codex_session_gc/execution/closed_work_contract.json"
    with zipfile.ZipFile(wheel) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    metadata_name = next(name for name in entries if name.endswith("/METADATA"))
    metadata = entries[metadata_name].decode()
    if mismatch == "version":
        metadata = metadata.replace("Version: 1.2.3", "Version: 0.9.0")
        wheel = tmp_path / "codex_session_gc-0.9.0-py3-none-any.whl"
    elif mismatch == "python":
        metadata = metadata.replace(">=3.14", ">=3.13")
    elif mismatch == "dependencies":
        metadata = metadata.replace("fixture-dependency==1.0", "fixture-dependency==2.0")
    elif mismatch == "raw-profile":
        entries[contract_name] = json.dumps(json.loads(entries[contract_name]), indent=2).encode()
    elif mismatch == "stale-pair":
        helper.write_bytes(b"historical matching helper")
        contract = json.loads(entries[contract_name])
        contract["binary_sha256"] = list(hashlib.sha256(helper.read_bytes()).digest())
        entries[contract_name] = json.dumps(contract).encode()
    elif mismatch == "license-expression":
        metadata = metadata.replace("License-Expression: Apache-2.0", "License-Expression: MIT")
    elif mismatch == "license-file":
        metadata = metadata.replace("License-File: NOTICE\n", "")
    elif mismatch == "notice-bytes":
        entries["codex_session_gc-1.2.3.dist-info/licenses/NOTICE"] = b"different attribution"
    else:
        (tmp_path / "NOTICE").write_bytes(b"new source attribution")
    entries[metadata_name] = metadata.encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, raw in entries.items():
            archive.writestr(name, raw)
    output = tmp_path / "release"
    result = _run(tool, wheel, helper, output, env)
    assert result.returncode == 2
    assert "current source" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("problem", ["unhashed-export", "existing-output"])
def test_invalid_inputs_leave_existing_data_untouched(tmp_path: Path, problem: str) -> None:
    """No unpinned runtime dependencies or replacement of a prior release is allowed."""
    tool, wheel, helper, env = _inputs(tmp_path)
    output = tmp_path / "release"
    if problem == "unhashed-export":
        (tmp_path / "bin/uv").write_text(f"#!{sys.executable}\nprint('dependency==1.0')\n")
    else:
        output.mkdir()
        (output / "keep").write_bytes(b"existing release")
    result = _run(tool, wheel, helper, output, env)
    assert result.returncode == 2
    if output.exists():
        assert {path.name: path.read_bytes() for path in output.iterdir()} == {
            "keep": b"existing release"
        }
    else:
        assert problem == "unhashed-export"
