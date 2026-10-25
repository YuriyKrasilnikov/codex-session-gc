# Copyright (c) 2026

"""Check source-bound distributions, bundles and optional installed project files."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tools import build_release, ci_native, source_map
from tools.source_map import ArtifactError


@dataclass(frozen=True, slots=True)
class ReleaseInspection:
    """Source-bound artifact evidence and the wheel map for installed comparison."""

    evidence: dict[str, object]
    wheel_map: source_map.SourceMap


def _require(message: str, *, condition: bool) -> None:
    if not condition:
        raise ArtifactError(message)


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts


def inspect_release(
    root: Path,
    sdist: Path,
    wheel: Path,
    bundle: Path,
    helper: Path,
) -> ReleaseInspection:
    """Verify source/package/payload byte agreement without launching installed code."""
    version, _, _, raw_profile, contract, wheel_map = build_release.current_artifacts(wheel, root)
    sdist_map = source_map.verify_sdist(sdist, root)
    helper_hash = ci_native.digest(helper)
    _require(
        "Supplied helper differs from the candidate profile.",
        condition=contract.get("binary_sha256") == list(bytes.fromhex(helper_hash)),
    )
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    _require(
        "Bundle raw profile identity differs.",
        condition=manifest["wheel_contract_sha256"] == hashlib.sha256(raw_profile).hexdigest()
        and manifest["wheel_source_map_sha256"] == wheel_map.digest,
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
    _verify_bundle_source(root, wheel, bundle)
    report: dict[str, object] = {
        "acceptance": (
            "structure_only; native execution and installed interfaces require separate gates"
        ),
        "package_version": version,
        "sdist_sha256": ci_native.digest(sdist),
        "wheel_sha256": ci_native.digest(wheel),
        "helper_sha256": helper_hash,
        "profile_sha256": hashlib.sha256(raw_profile).hexdigest(),
        "wheel_source_map_sha256": wheel_map.digest,
        "sdist_source_map_sha256": sdist_map.digest,
        "bundle_payload_files": len(payload),
    }

    return ReleaseInspection(report, wheel_map)


def _verify_bundle_source(root: Path, wheel: Path, bundle: Path) -> None:
    for name in set(build_release.DOCUMENTS) | set(build_release.license_payload(root)):
        source, copied = root / name, bundle / name
        _require(
            f"Bundle payload differs from the current source: {name}",
            condition=ci_native.digest(copied) == ci_native.digest(source)
            and copied.stat().st_size == source.stat().st_size,
        )
    _require(
        "Bundled runtime requirements differ from the current source lock.",
        condition=(bundle / "requirements-runtime.txt").read_bytes()
        == build_release.runtime_lock(root),
    )
    _require(
        "Bundled install requirements differ from the exact wheel.",
        condition=(bundle / "install-requirements.txt").read_bytes()
        == build_release.install_requirements(wheel.name, ci_native.digest(wheel)),
    )


def main() -> None:
    """Check completed real artifacts after the existing release assembler finishes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--installed-site-packages",
        type=Path,
        help="Also compare installed project files in this site-packages directory.",
    )
    for name in ("sdist", "wheel", "bundle", "helper"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        ci_native.verify_source(args.root)
        checked = inspect_release(args.root, args.sdist, args.wheel, args.bundle, args.helper)
        evidence = checked.evidence
        if args.installed_site_packages is not None:
            generated = source_map.verify_installed(checked.wheel_map, args.installed_site_packages)
            evidence.update(
                acceptance=(
                    "source_and_installed_files_only; behavioral qualification remains separate"
                ),
                installed_project_files=len(checked.wheel_map.files),
                classified_installer_or_bytecode=list(generated),
            )

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
