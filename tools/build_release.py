# Copyright (c) 2026
# ruff: noqa: EM101, TRY003, PLR2004, S607
"""Assemble a source-matched wheel/helper bundle without building or executing the helper."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from codex_session_gc.artifacts.bundle import MANIFEST_MAX_BYTES, RELEASE_FORMAT, parse_manifest
from tools import source_map

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


def current_artifacts(
    wheel: Path,
    root: Path,
) -> tuple[str, str, frozenset[Requirement], bytes, dict[str, object], source_map.SourceMap]:
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
    mapped = source_map.verify_wheel(wheel, root)
    return (*identity, mapped)


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


def install_requirements(wheel_name: str, wheel_digest: str) -> bytes:
    """Bind the local wheel requirement to its exact qualified artifact bytes."""
    return (f"-r requirements-runtime.txt\n./{wheel_name} --hash=sha256:{wheel_digest}\n").encode()


def assemble(wheel: Path, helper: Path, output: Path, root: Path) -> None:
    """Publish one absent output directory only after checking every payload identity."""
    if output.exists():
        raise ValueError("Output already exists; refusing to replace a release.")
    version, python_requirement, dependencies, raw_contract, contract, selected_source = (
        current_artifacts(wheel, root)
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
    payload["install-requirements.txt"] = install_requirements(wheel.name, wheel_digest)
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
        copied_version, copied_python, copied_dependencies, copied_contract, _, copied_source = (
            current_artifacts(staging / wheel.name, root)
        )
        if (
            copied_version,
            copied_python,
            copied_dependencies,
            copied_contract,
            copied_source.digest,
        ) != (
            version,
            python_requirement,
            dependencies,
            raw_contract,
            selected_source.digest,
        ):
            raise ValueError("The copied wheel differs from the inspected contract or metadata.")
        manifest = {
            "format": RELEASE_FORMAT,
            "package_version": version,
            "requires_python": python_requirement,
            "native_source_identity": contract["source_identity"],
            "wire_version": contract["wire_version"],
            "wheel_contract_sha256": hashlib.sha256(raw_contract).hexdigest(),
            "wheel_source_map_sha256": selected_source.digest,
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
        manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        parse_manifest(manifest_bytes)
        (staging / "manifest.json").write_bytes(manifest_bytes)
        if output.exists():
            raise ValueError("Output appeared during assembly; refusing replacement.")
        staging.rename(output)


def _manifest_payload(bundle: Path) -> tuple[bytes, dict[str, tuple[int, str]]]:
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Release manifest must be a regular file.")
    with manifest_path.open("rb") as stream:
        raw = stream.read(MANIFEST_MAX_BYTES + 1)
    manifest = parse_manifest(raw)
    if bundle.name != f"codex-session-gc-{manifest.version}-linux-x86_64":
        raise ValueError("Release directory name differs from manifest version.")
    return raw, {name: (entry.size, entry.sha256) for name, entry in manifest.files.items()}


def _release_members(bundle: Path, entries: dict[str, tuple[int, str]]) -> list[str]:
    directories = {
        str(parent)
        for name in entries
        for parent in PurePosixPath(name).parents
        if str(parent) != "."
    }
    actual = {path.relative_to(bundle).as_posix(): path for path in bundle.rglob("*")}
    if set(actual) != set(entries) | directories | {"manifest.json"}:
        raise ValueError("Release directory contains undeclared or missing members.")
    for name, path in actual.items():
        if (
            path.is_symlink()
            or (name in directories and not path.is_dir())
            or (name not in directories and not path.is_file())
        ):
            raise ValueError("Release directory contains an invalid member.")
    for name, (size, sha) in entries.items():
        path = actual[name]
        if path.stat().st_size != size or digest(path) != sha:
            raise ValueError("Release payload changed or differs from its manifest.")
    return sorted(set(entries) | directories)


def _archive_info(
    name: str, *, directory: bool, size: int = 0, executable: bool = False
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.mode = 0o755 if directory or executable else 0o644
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    info.size = size
    return info


def _verify_archive(
    path: Path, root: str, manifest: bytes, entries: dict[str, tuple[int, str]], members: list[str]
) -> None:
    expected = [root, f"{root}/manifest.json", *(f"{root}/{name}" for name in members)]
    with tarfile.open(path, "r:gz") as archive:
        actual = archive.getmembers()
        if [item.name for item in actual] != expected:
            raise ValueError("Release archive member order or names differ.")
        for item in actual:
            relative = item.name.removeprefix(root + "/")
            file_expected = item.name != root and (
                relative == "manifest.json" or relative in entries
            )
            if (
                item.uid != 0
                or item.gid != 0
                or item.mtime != 0
                or item.mode
                != (0o755 if not file_expected or relative == "codex-session-gc-native" else 0o644)
            ):
                raise ValueError("Release archive metadata differs.")
            if not file_expected:
                if not item.isdir():
                    raise ValueError("Release archive directory differs.")
                continue
            if not item.isfile():
                raise ValueError("Release archive file differs.")
            stream = archive.extractfile(item)
            if stream is None:
                raise ValueError("Release archive file is unavailable.")
            with stream:
                hashed = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    hashed.update(chunk)
                sha = hashed.hexdigest()
            size, wanted = (
                (len(manifest), hashlib.sha256(manifest).hexdigest())
                if relative == "manifest.json"
                else entries[relative]
            )
            if item.size != size or sha != wanted:
                raise ValueError("Release archive payload differs from manifest.")


def archive_release(bundle: Path, output: Path) -> tuple[str, int]:
    """Create a deterministic, manifest-first archive and return its SHA-256 and size."""
    if (
        bundle.is_symlink()
        or not bundle.is_dir()
        or output.exists()
        or output.is_symlink()
        or output.resolve().is_relative_to(bundle.resolve())
    ):
        raise ValueError("Archive requires an existing directory and absent external output.")
    root = bundle.name
    manifest, entries = _manifest_payload(bundle)
    members = _release_members(bundle, entries)
    with tempfile.TemporaryDirectory(prefix=".gc-archive-", dir=output.parent) as temporary:
        candidate = Path(temporary) / "release.tar.gz"
        with (
            candidate.open("wb") as raw,
            gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed,
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive,
        ):
            archive.addfile(_archive_info(root, directory=True))
            path = bundle / "manifest.json"
            with path.open("rb") as stream:
                archive.addfile(
                    _archive_info(f"{root}/manifest.json", directory=False, size=len(manifest)),
                    stream,
                )
            for name in members:
                path = bundle / name
                is_directory = name not in entries
                info = _archive_info(
                    f"{root}/{name}",
                    directory=is_directory,
                    size=0 if is_directory else entries[name][0],
                    executable=name == "codex-session-gc-native",
                )
                if is_directory:
                    archive.addfile(info)
                else:
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
        _verify_archive(candidate, root, manifest, entries, members)
        identity = (digest(candidate), candidate.stat().st_size)
        os.link(candidate, output)
        if (digest(output), output.stat().st_size) != identity:
            output.unlink()
            raise ValueError("Published archive identity differs from verified bytes.")
        return identity


def main() -> None:
    """Require source-matched Python files and an independently qualified native helper."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--helper", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.archive is not None and (
        arguments.wheel is not None or arguments.helper is not None
    ):
        parser.error("Archive mode cannot combine wheel or helper inputs.")
    if arguments.archive is None and (arguments.wheel is None or arguments.helper is None):
        parser.error("Assembly requires wheel and helper inputs.")
    try:
        if arguments.archive is not None:
            archive_release(arguments.archive, arguments.output)
        elif arguments.wheel is not None and arguments.helper is not None:
            assemble(
                arguments.wheel,
                arguments.helper,
                arguments.output,
                Path(__file__).resolve().parents[1],
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
