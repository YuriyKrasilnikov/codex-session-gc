# Copyright (c) 2026
# ruff: noqa: EM101, TRY003, PLR2004, INP001, S607
"""Assemble an exact wheel/helper bundle without building or qualifying artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path

from packaging.licenses import canonicalize_license_expression
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

DOCUMENTS = (
    "README.md",
    "docs/contract.md",
    "docs/recovery.md",
    "docs/architecture.md",
    "docs/compatibility.md",
    "native/README.md",
    "native/build-inputs/provenance.json",
)
CONTRACT = "codex_session_gc/execution/closed_work_contract.json"


def digest(path: Path) -> str:
    """Hash a complete artifact without materializing a helper in memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def wheel_identity(path: Path) -> tuple[str, str, frozenset[Requirement], bytes, dict[str, object]]:
    """Read artifact metadata and the exact qualified contract packaged in the wheel."""
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or names.count(CONTRACT) != 1:
            raise ValueError("Wheel must contain one metadata record and one native contract.")
        if wheel.getinfo(metadata[0]).file_size > 1024 * 1024:
            raise ValueError("Wheel metadata exceeds the 1 MiB bound.")
        if wheel.getinfo(CONTRACT).file_size > 16 * 1024 * 1024:
            raise ValueError("Wheel contract exceeds the 16 MiB bound.")
        fields = BytesParser().parsebytes(wheel.read(metadata[0]))
        version = fields.get("Version", "")
        requirement = fields.get("Requires-Python", "")
        if fields.get("Name") != "codex-session-gc" or not re.fullmatch(
            r"[0-9A-Za-z.+!-]+", version
        ):
            raise ValueError("Wheel package identity is invalid.")
        if not requirement:
            raise ValueError("Wheel must declare its Python requirement.")
        if path.name != f"codex_session_gc-{version}-py3-none-any.whl":
            raise ValueError("Release requires the generic Python wheel matching its metadata.")
        raw = wheel.read(CONTRACT)
        dependencies = frozenset(
            Requirement(value) for value in fields.get_all("Requires-Dist", [])
        )
    contract = json.loads(raw)
    if not isinstance(contract, dict):
        raise TypeError("Wheel contract must be an object.")
    return version, requirement, dependencies, raw, contract


def license_payload(root: Path) -> dict[str, bytes]:
    """Read the exact notice files declared by the project's standard license metadata."""
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    payload = {}
    for pattern in project["license-files"]:
        matches = [path for path in root.glob(pattern) if path.is_file()]
        if not matches:
            raise ValueError("A declared license-file pattern has no files.")
        for path in matches:
            if path.is_symlink():
                raise ValueError("License inputs must be regular files, not symbolic links.")
            payload[path.relative_to(root).as_posix()] = path.read_bytes()
    if not payload:
        raise ValueError("The project must declare its license and notice files.")
    return payload


def validate_wheel_notices(wheel: Path, root: Path) -> None:
    """Require installed wheel notices and SPDX metadata to match the selected source."""
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    expected = license_payload(root)
    with zipfile.ZipFile(wheel) as archive:
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_path))
        if canonicalize_license_expression(
            metadata.get("License-Expression", "")
        ) != canonicalize_license_expression(project["license"]):
            raise ValueError("Wheel license expression differs from the current source project.")
        if set(metadata.get_all("License-File", [])) != set(expected):
            raise ValueError("Wheel license files differ from the current source project.")
        prefix = metadata_path.removesuffix("METADATA") + "licenses/"
        for name, raw in expected.items():
            if archive.read(prefix + name) != raw:
                raise ValueError("Wheel notice bytes differ from the current source project.")


def current_artifacts(
    wheel: Path,
    root: Path,
) -> tuple[str, str, frozenset[Requirement], bytes, dict[str, object]]:
    """Bind supplied artifacts to the selected source before exporting its dependencies."""
    identity = wheel_identity(wheel)
    version, python_requirement, dependencies, raw_contract, _ = identity
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    if Version(version) != Version(project["version"]):
        raise ValueError("Wheel version differs from the current source project.")
    if SpecifierSet(python_requirement) != SpecifierSet(project["requires-python"]):
        raise ValueError("Wheel Python requirement differs from the current source project.")
    if dependencies != frozenset(Requirement(value) for value in project["dependencies"]):
        raise ValueError("Wheel dependencies differ from the current source project.")
    if raw_contract != (root / "src" / CONTRACT).read_bytes():
        raise ValueError("Wheel raw profile differs from the current source contract.")
    validate_wheel_notices(wheel, root)
    return identity


def runtime_lock(root: Path) -> bytes:
    """Use a hash-bearing production export, never install or resolve dependencies."""
    result = subprocess.run(  # noqa: S603
        [
            "uv",
            "export",
            "--project",
            str(root),
            "--locked",
            "--offline",
            "--no-dev",
            "--no-emit-project",
            "--no-annotate",
            "--no-header",
            "--no-config",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    raw = result.stdout
    if not raw or len(raw) > 1024 * 1024:
        raise ValueError("Runtime export is empty or exceeds the 1 MiB bound.")
    logical = raw.decode().replace("\\\n", " ")
    for raw_line in logical.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_.,-]+\])?==[^\s;]+"
            r"(?:\s*;[^\n]*?)?\s+(?:--hash=sha256:[a-f0-9]{64}\s*)+",
            line,
        ):
            raise ValueError(
                "Runtime export must contain only pinned, SHA-256 hashed requirements."
            )
    return raw


def assemble(wheel: Path, helper: Path, output: Path, root: Path) -> None:
    """Publish one absent output directory only after checking every payload identity."""
    if output.exists():
        raise ValueError("Output already exists; refusing to replace a release.")
    version, python_requirement, dependencies, raw_contract, contract = current_artifacts(
        wheel, root
    )
    declared = contract.get("binary_sha256")
    if (
        not isinstance(declared, list)
        or len(declared) != 32
        or any(type(value) is not int or not 0 <= value <= 255 for value in declared)
    ):
        raise ValueError("Wheel contract has no exact native SHA-256 identity.")
    helper_digest = digest(helper)
    if bytes(declared).hex() != helper_digest:
        raise ValueError("Helper differs from the wheel's qualified native digest.")
    if (
        not isinstance(contract.get("source_identity"), str)
        or type(contract.get("wire_version")) is not int
    ):
        raise ValueError("Wheel contract lacks source or wire identity.")
    runtime = runtime_lock(root)
    provenance = json.loads((root / "native/build-inputs/provenance.json").read_bytes())
    wheel_digest = digest(wheel)
    payload = {name: (root / name).read_bytes() for name in DOCUMENTS}
    payload.update(license_payload(root))
    payload["requirements-runtime.txt"] = runtime
    payload["install-requirements.txt"] = (
        f"-r requirements-runtime.txt\n./{wheel.name} --hash=sha256:{wheel_digest}\n"
    ).encode()
    if wheel.name in payload or wheel.name in {"manifest.json", "codex-session-gc-native"}:
        raise ValueError("Wheel filename conflicts with the bundle payload.")
    with tempfile.TemporaryDirectory(prefix=".gc-release-", dir=output.parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        for name, data in payload.items():
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(0o644)
        shutil.copyfile(wheel, staging / wheel.name)
        shutil.copyfile(helper, staging / "codex-session-gc-native")
        (staging / wheel.name).chmod(0o644)
        (staging / "codex-session-gc-native").chmod(0o755)
        if (
            digest(staging / wheel.name) != wheel_digest
            or digest(staging / "codex-session-gc-native") != helper_digest
        ):
            raise ValueError("An input artifact changed during bundle assembly.")
        copied_version, copied_python, copied_dependencies, copied_contract, _ = current_artifacts(
            staging / wheel.name, root
        )
        if (copied_version, copied_python, copied_dependencies, copied_contract) != (
            version,
            python_requirement,
            dependencies,
            raw_contract,
        ):
            raise ValueError("The copied wheel differs from the inspected contract or metadata.")
        manifest = {
            "format": "codex-session-gc-release-v1",
            "package_version": version,
            "requires_python": python_requirement,
            "native_source_identity": contract["source_identity"],
            "wire_version": contract["wire_version"],
            "wheel_contract_sha256": hashlib.sha256(raw_contract).hexdigest(),
            "source_provenance": provenance,
            "payload": {
                str(path.relative_to(staging)): {
                    "sha256": digest(path),
                    "bytes": path.stat().st_size,
                }
                for path in sorted(staging.rglob("*"))
                if path.is_file()
            },
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        )
        if output.exists():
            raise ValueError("Output appeared during assembly; refusing replacement.")
        staging.rename(output)


def main() -> None:
    """Accept already built artifacts; qualification remains a separate prerequisite."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        assemble(
            arguments.wheel, arguments.helper, arguments.output, Path(__file__).resolve().parents[1]
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        zipfile.BadZipFile,
        subprocess.SubprocessError,
    ) as error:
        parser.exit(2, f"Bundle refused: {error}\n")


if __name__ == "__main__":
    main()
