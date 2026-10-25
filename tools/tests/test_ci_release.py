# Copyright (c) 2026

"""Source and installed-file checks using real tiny distributions and a synthetic helper."""

from __future__ import annotations

import csv
import io
import json
import py_compile
import tarfile
import zipfile
from pathlib import Path

import pytest
from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

from tests.test_release import _inputs, _run, _write_wheel
from tools import ci_native, ci_release, source_map

DIST_INFO = "codex_session_gc-1.2.3.dist-info"


def _equal(actual: object, expected: object) -> None:
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, observed {actual!r}")


def _sdist(root: Path, path: Path) -> None:
    built = next(SdistBuilder(str(root)).build(directory=str(path.parent)))
    _equal(str(built), str(path))


@pytest.fixture
def artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path, Path]:
    """Build tiny real-Hatch distributions around a synthetic helper and lock export."""
    root = tmp_path / "source"
    root.mkdir()
    tool, wheel, helper, environment = _inputs(root)
    monkeypatch.setenv("PATH", environment["PATH"])
    (root / "uv.lock").write_text("synthetic lock\n")
    bundle = root / "bundle"
    result = _run(tool, wheel, helper, bundle, environment)
    _equal(result.returncode, 0)
    sdist = root / "codex_session_gc-1.2.3.tar.gz"
    _sdist(root, sdist)
    ci_release.inspect_release(root, sdist, wheel, bundle, helper)
    return root, sdist, wheel, bundle, helper


def test_matching_artifact_structure_is_not_a_runtime_qualification(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Bind all structural bytes while retaining the installed/native acceptance boundary."""
    root, sdist, wheel, bundle, helper = artifacts
    result = ci_release.inspect_release(root, sdist, wheel, bundle, helper).evidence
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
    with pytest.raises(source_map.ArtifactError):
        source_map.verify_record(wheel)


def test_sdist_profile_and_legal_bytes_must_match_source(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Source-distribution copies retain exact profile and attribution bytes."""
    root, sdist, _, _, _ = artifacts
    (root / "LICENSE").write_text("changed license\n")
    with pytest.raises(source_map.ArtifactError):
        source_map.verify_sdist(sdist, root)


@pytest.mark.parametrize("name", ["codex-session-gc-native", "requirements-runtime.txt", "NOTICE"])
def test_changed_payload_is_refused(
    artifacts: tuple[Path, Path, Path, Path, Path],
    name: str,
) -> None:
    """Bundle hashes protect executable, dependency requirements and legal payload alike."""
    root, sdist, wheel, bundle, helper = artifacts
    (bundle / name).write_bytes(b"changed")
    with pytest.raises(source_map.ArtifactError):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)


def test_extra_bundle_file_is_not_selected_by_directory_glob(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Only the assembler's declared payload can enter the matched bundle."""
    root, sdist, wheel, bundle, helper = artifacts
    (bundle / "extra").write_bytes(b"unreviewed")
    with pytest.raises(source_map.ArtifactError):
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
    with pytest.raises(source_map.ArtifactError):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)


def test_self_consistent_code_substitution_still_differs_from_selected_source(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Artifact hashes cannot authorize Python code that differs from selected source."""
    root, sdist, wheel, bundle, helper = artifacts
    source = root / "src/codex_session_gc/api.py"
    source.write_bytes(b"SELECTED = 'original'\n")
    with zipfile.ZipFile(wheel) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    payload.pop(f"{DIST_INFO}/RECORD")
    payload["codex_session_gc/api.py"] = b"SELECTED = 'substituted'\n"
    _write_wheel(wheel, payload)
    copied = bundle / wheel.name
    copied.write_bytes(wheel.read_bytes())
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["payload"][wheel.name] = {
        "sha256": ci_native.digest(copied),
        "bytes": copied.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(source_map.ArtifactError):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)


def _mutated(payload: dict[str, bytes], name: str, change: str) -> dict[str, bytes]:
    altered = dict(payload)
    prefix, basename = name.rsplit("/", 1)
    if change == "missing":
        del altered[name]
    elif change == "extra":
        altered[f"{prefix}/extra_{basename}"] = altered[name]
    elif change == "renamed":
        altered[f"{prefix}/renamed_{basename}"] = altered.pop(name)
    elif change == "substituted":
        altered[name] = (
            b'{"fixture":false}\n' if name.endswith(".json") else b"SELECTED = 'changed'\n"
        )
    else:
        pytest.fail(f"Unknown mutation: {change}")
    return altered


def _replace_payload(bundle: Path, name: str, data: bytes) -> None:
    path = bundle / name
    path.write_bytes(data)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["payload"][name] = {"sha256": ci_native.digest(path), "bytes": len(data)}
    manifest_path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("member", ["api.py", "resources/settings.json"])
@pytest.mark.parametrize("change", ["missing", "extra", "renamed", "substituted"])
def test_source_owned_wheel_members_are_bound_beyond_record_and_bundle_hashes(
    artifacts: tuple[Path, Path, Path, Path, Path], member: str, change: str
) -> None:
    """All Python/resource membership and bytes remain bound after consistent rehashing."""
    root, sdist, wheel, bundle, helper = artifacts
    with zipfile.ZipFile(wheel) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    _write_wheel(wheel, _mutated(payload, f"codex_session_gc/{member}", change))
    _replace_payload(bundle, wheel.name, wheel.read_bytes())

    with pytest.raises(source_map.ArtifactError):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)


@pytest.mark.parametrize("member", ["api.py", "resources/settings.json"])
@pytest.mark.parametrize("change", ["missing", "extra", "renamed", "substituted"])
def test_source_owned_sdist_members_cannot_be_changed_independently_of_checkout(
    artifacts: tuple[Path, Path, Path, Path, Path], member: str, change: str
) -> None:
    """Source-distribution code/resources cannot diverge while metadata remains valid."""
    root, sdist, wheel, bundle, helper = artifacts
    payload: dict[str, bytes] = {}
    with tarfile.open(sdist, "r:gz") as archive:
        for item in archive.getmembers():
            if not item.isfile():
                continue
            stream = archive.extractfile(item)
            if stream is None:
                pytest.fail("A fixture file has no source stream")
            with stream:
                payload[item.name] = stream.read()
    altered = _mutated(payload, f"codex_session_gc-1.2.3/src/codex_session_gc/{member}", change)
    with tarfile.open(sdist, "w:gz") as archive:
        for name, data in altered.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))

    with pytest.raises(source_map.ArtifactError):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)


def _installed(wheel: Path, destination: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(name))


@pytest.mark.parametrize("member", ["api.py", "resources/settings.json"])
@pytest.mark.parametrize("change", ["missing", "extra", "renamed", "substituted"])
def test_installed_python_and_resources_must_match_the_verified_wheel_map(
    artifacts: tuple[Path, Path, Path, Path, Path], member: str, change: str
) -> None:
    """An installed file cannot inherit qualification from an unchanged wheel alone."""
    root, _, wheel, _, _ = artifacts
    selected = source_map.verify_wheel(wheel, root)
    installed = root.parent / "installed"
    _installed(wheel, installed)
    _equal(source_map.verify_installed(selected, installed), (selected.record_path,))
    path = installed / "codex_session_gc" / member
    if change == "missing":
        path.unlink()
    elif change == "extra":
        path.with_name("extra_" + path.name).write_bytes(path.read_bytes())
    elif change == "renamed":
        path.rename(path.with_name("renamed_" + path.name))
    else:
        path.write_bytes(
            b'{"fixture":false}\n' if path.suffix == ".json" else b"SELECTED = 'changed'\n"
        )

    with pytest.raises(source_map.ArtifactError):
        source_map.verify_installed(selected, installed)


def test_installer_metadata_and_source_derived_bytecode_are_reported_separately(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Known generated files are classified without opening a general extra-file exception."""
    root, _, wheel, _, _ = artifacts
    selected = source_map.verify_wheel(wheel, root)
    installed = root.parent / "installed"
    _installed(wheel, installed)
    metadata = installed / DIST_INFO
    for name in ("INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"):
        (metadata / name).write_bytes(b"{}\n")
    compiled = py_compile.compile(str(installed / "codex_session_gc/api.py"), doraise=True)
    if compiled is None:
        pytest.fail("Expected source-derived bytecode output")

    generated = source_map.verify_installed(selected, installed)

    _equal(f"{DIST_INFO}/INSTALLER" in generated, expected=True)
    _equal(Path(compiled).relative_to(installed).as_posix() in generated, expected=True)
    (installed / "codex_session_gc/__pycache__/undeclared.cpython-314.pyc").write_bytes(b"unknown")
    with pytest.raises(source_map.ArtifactError):
        source_map.verify_installed(selected, installed)


@pytest.mark.parametrize(
    "name", ["README.md", "NOTICE", "requirements-runtime.txt", "install-requirements.txt"]
)
def test_self_consistent_bundle_payload_cannot_replace_source_or_generated_requirements(
    artifacts: tuple[Path, Path, Path, Path, Path], name: str
) -> None:
    """Updating the manifest hash does not replace the source or lock agreement."""
    root, sdist, wheel, bundle, helper = artifacts
    _replace_payload(bundle, name, b"self-consistent replacement\n")

    with pytest.raises(source_map.ArtifactError):
        ci_release.inspect_release(root, sdist, wheel, bundle, helper)


def test_real_hatch_wheel_from_verified_sdist_has_the_same_source_map(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """The backend's actual sdist-to-wheel transformation conforms to the source projection."""
    root, sdist, _, _, _ = artifacts
    source_map.verify_sdist(sdist, root)
    extracted = root.parent / "unpacked"
    with tarfile.open(sdist, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    project = extracted / "codex_session_gc-1.2.3"
    output = root.parent / "from-sdist"
    output.mkdir()
    wheel = Path(next(WheelBuilder(str(project)).build(directory=str(output))))

    mapped = source_map.verify_wheel(wheel, root)

    _equal(mapped.digest, source_map.source_map(project, source_map.Target.WHEEL).digest)
    _equal(
        (project / "tools/source_map.py").read_bytes(), (root / "tools/source_map.py").read_bytes()
    )


def test_generated_entry_points_are_bound_to_the_selected_project_declaration(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """A correctly rehashed entry-point replacement cannot change the declared command."""
    root, _, _, _, _ = artifacts
    configuration = root / "pyproject.toml"
    configuration.write_text(
        configuration.read_text().replace(
            "[build-system]",
            '[project.scripts]\nfixture-gc="codex_session_gc.api:main"\n[build-system]',
        )
    )
    output = root.parent / "entry-wheel"
    output.mkdir()
    wheel = Path(next(WheelBuilder(str(root)).build(directory=str(output))))
    source_map.verify_wheel(wheel, root)
    with zipfile.ZipFile(wheel) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    payload[f"{DIST_INFO}/entry_points.txt"] = (
        b"[console_scripts]\nother=codex_session_gc.api:main\n"
    )
    _write_wheel(wheel, payload)

    with pytest.raises(source_map.ArtifactError):
        source_map.verify_wheel(wheel, root)


def test_generated_metadata_directory_is_not_an_exception_for_extra_python(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Only named generated metadata can accompany the source-owned package payload."""
    root, _, wheel, _, _ = artifacts
    with zipfile.ZipFile(wheel) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    payload[f"{DIST_INFO}/extra.py"] = b"UNDECLARED = True\n"
    _write_wheel(wheel, payload)

    with pytest.raises(source_map.ArtifactError):
        source_map.verify_wheel(wheel, root)


def test_source_mapping_refuses_build_hooks_before_executing_their_code(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """Unqualified generated-file mechanisms cannot expand the observed source mapping."""
    root, _, _, _, _ = artifacts
    marker = root / "hook-ran"
    (root / "hook.py").write_text(
        "from pathlib import Path\n" + f"Path({str(marker)!r}).write_text('ran')\n"
    )
    configuration = root / "pyproject.toml"
    configuration.write_text(
        configuration.read_text() + '\n[tool.hatch.build.hooks.custom]\npath="hook.py"\n'
    )

    with pytest.raises(source_map.ArtifactError):
        source_map.source_map(root, source_map.Target.WHEEL)
    _equal(marker.exists(), expected=False)


def test_external_force_inclusion_is_not_project_owned_source(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """The builder's selected external path does not become an authorized source input."""
    root, _, _, _, _ = artifacts
    external = root.parent / "external.py"
    external.write_bytes(b"UNDECLARED = True\n")
    configuration = root / "pyproject.toml"
    configuration.write_text(
        configuration.read_text() + "\n[tool.hatch.build.targets.wheel.force-include]\n"
        '"../external.py"="codex_session_gc/external.py"\n'
    )

    with pytest.raises(source_map.ArtifactError):
        source_map.source_map(root, source_map.Target.WHEEL)
    _equal(external.read_bytes(), b"UNDECLARED = True\n")


def test_malformed_record_is_a_structured_artifact_refusal(
    artifacts: tuple[Path, Path, Path, Path, Path],
) -> None:
    """A CSV parser refusal remains in the artifact checker's error contract."""
    root, _, wheel, _, _ = artifacts
    with zipfile.ZipFile(wheel) as archive:
        payload = {name: archive.read(name) for name in archive.namelist()}
    payload[f"{DIST_INFO}/RECORD"] = b"x" * (csv.field_size_limit() + 1)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr(name, data)

    with pytest.raises(source_map.ArtifactError):
        source_map.verify_wheel(wheel, root)
