# Copyright (c) 2026

"""Cargo envelopes are derived from immutable synthetic Git snapshots."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tools.compatibility import cargo, git
from tools.compatibility.report import Request, assess
from tools.tests.test_compatibility import _ENV, _GIT, _commit, _git, _run, _write


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a workspace with local dependency kinds and include inputs."""
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _write(
        root,
        "Cargo.toml",
        """[workspace]
members = ["crates/*"]
[workspace.dependencies]
shared = { path = "crates/shared", features = ["base"] }
""",
    )
    _write(
        root,
        "Cargo.lock",
        """version = 4
[[package]]
name = "registry-package"
version = "1.0.0"
source = "registry+https://example.invalid/index"
""",
    )
    _write(root, "rust-toolchain.toml", '[toolchain]\nchannel = "stable"\n')
    _write(
        root,
        "crates/root/Cargo.toml",
        """[package]
name = "root"
version = "1.0.0"
[dependencies]
shared = { workspace = true, features = ["extra"], default-features = false }
optional_dep = { path = "../optional", optional = true }
remote = "1"
[features]
default = ["optional_dep"]
custom = ["shared/extra"]
[build-dependencies]
builder = { path = "../builder" }
[dev-dependencies]
testing = { path = "../testing" }
[target.'cfg(unix)'.dependencies]
platform = { path = "../platform" }
""",
    )
    _write(
        root,
        "crates/root/src/lib.rs",
        """include!("../../extra/nested.rs");
include!(concat!(env!("OUT_DIR"), "/generated.rs"));
include_str!("missing.txt");
""",
    )
    _write(root, "crates/root/build.rs", "fn main() {}\n")
    _write(root, "crates/extra/nested.rs", 'include_bytes!("data.bin");\n')
    _write(root, "crates/extra/data.bin", "data\n")
    for name in ("shared", "optional", "builder", "testing", "platform"):
        _write(
            root, f"crates/{name}/Cargo.toml", f'[package]\nname = "{name}"\nversion = "1.0.0"\n'
        )
        _write(root, f"crates/{name}/src/lib.rs", f"// {name}\n")
    return root, _commit(root, "Create workspace")


def _derive(root: Path, commit: str) -> cargo.CargoEnvelope:
    return asyncio.run(
        cargo.derive(
            _GIT,
            root,
            commit,
            cargo.CargoInputs("Cargo.toml", ("crates/root/Cargo.toml",), cargo.CargoLimits()),
        )
    )


def test_explicit_limits_preserve_partial_envelope(workspace: tuple[Path, str]) -> None:
    """A small caller budget exposes partial nodes and a resource gap."""
    root, commit = workspace
    inputs = cargo.CargoInputs(
        "Cargo.toml", ("crates/root/Cargo.toml",), cargo.CargoLimits(nodes=5)
    )
    envelope = asyncio.run(cargo.derive(_GIT, root, commit, inputs))
    if not envelope.nodes or "source_node_limit" not in envelope.gaps:
        raise AssertionError


def test_assessment_reuses_verified_objects_across_phases_and_commits(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One invocation fetches a shared blob once across all consumers."""
    root, baseline = workspace
    _write(root, "crates/root/src/new.rs", "// added\n")
    target = _commit(root, "Add source")
    actual = git.blobs
    counts: dict[str, int] = {}

    async def counted(
        executable: Path, repository: Path, objects: tuple[tuple[str, int], ...], *, seconds: float
    ) -> tuple[bytes, ...]:
        for object_id, _ in objects:
            counts[object_id] = counts.get(object_id, 0) + 1
        return await actual(executable, repository, objects, seconds=seconds)

    monkeypatch.setattr(git, "blobs", counted)
    report = asyncio.run(
        assess(
            Request(
                _GIT,
                root,
                baseline,
                target,
                None,
                None,
                None,
                "Cargo.toml",
                ("crates/root/Cargo.toml",),
            )
        )
    )
    if not report["changed_blobs_checked"] or not report["closure_blobs_checked"]:
        raise AssertionError
    if not counts or max(counts.values()) != 1:
        raise AssertionError


def test_identical_include_blobs_resolve_relative_to_each_path(
    workspace: tuple[Path, str],
) -> None:
    """Shared source bytes do not merge relative include path interpretation."""
    root, _ = workspace
    source = 'include_str!("local.txt");\n'
    _write(root, "crates/root/a/source.rs", source)
    _write(root, "crates/root/b/source.rs", source)
    _write(root, "crates/root/a/local.txt", "first\n")
    _write(root, "crates/root/b/local.txt", "second\n")
    commit = _commit(root, "Add relative includes")
    paths = {node.path for node in _derive(root, commit).nodes}
    if not {"crates/root/a/local.txt", "crates/root/b/local.txt"} <= paths:
        raise AssertionError


def test_all_local_dependency_kinds_and_recursive_includes(workspace: tuple[Path, str]) -> None:
    """Workspace, optional, build, dev and target paths are all retained."""
    root, commit = workspace
    envelope = _derive(root, commit)
    paths = {node.path for node in envelope.nodes}
    expected = {
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "crates/root/build.rs",
        "crates/shared/Cargo.toml",
        "crates/optional/Cargo.toml",
        "crates/builder/Cargo.toml",
        "crates/testing/Cargo.toml",
        "crates/platform/Cargo.toml",
        "crates/extra/nested.rs",
        "crates/extra/data.bin",
    }
    if not expected <= paths:
        raise AssertionError
    features = {feature.name: feature.values for feature in envelope.features}
    if features.get("default") != ("optional_dep",) or features.get("custom") != ("shared/extra",):
        raise AssertionError
    edges = {edge.dependency: edge for edge in envelope.edges}
    if not edges["shared"].workspace_inherited or not edges["optional_dep"].optional:
        raise AssertionError
    if (
        edges["shared"].declared_features != ("extra",)
        or edges["shared"].workspace_features != ("base",)
        or edges["shared"].default_features is not False
    ):
        raise AssertionError
    if edges["builder"].kind != "build" or edges["testing"].kind != "dev":
        raise AssertionError
    if edges["platform"].target != "cfg(unix)":
        raise AssertionError
    required_gaps = {
        "macro_codegen_and_caller_coverage_unresolved",
        "feature_activation_not_evaluated",
        "build_script_effects_unresolved",
        "dynamic_include_unresolved",
        "referenced_source_missing",
        "external_dependency_source_unresolved",
        "locked_external_source_unresolved",
    }
    expected_members = 6
    if (
        not required_gaps <= set(envelope.gaps)
        or len(envelope.workspace_member_candidates) != expected_members
    ):
        raise AssertionError
    if any("https://" in gap for gap in envelope.gaps):
        raise AssertionError


def test_new_dependency_appears_only_in_target_and_dirty_tree_is_ignored(
    workspace: tuple[Path, str],
) -> None:
    """A newly introduced local dependency changes the target envelope by Git identity."""
    root, baseline = workspace
    _write(root, "crates/new/Cargo.toml", '[package]\nname = "new"\nversion = "1"\n')
    _write(root, "crates/new/src/lib.rs", "// committed\n")
    manifest = root / "crates/root/Cargo.toml"
    manifest.write_text(manifest.read_text() + '\n[dev-dependencies.new]\npath = "../new"\n')
    target = _commit(root, "Add local dependency")
    _write(root, "crates/new/src/lib.rs", "// dirty\n")
    result = asyncio.run(
        assess(
            Request(
                _GIT,
                root,
                baseline,
                target,
                None,
                None,
                None,
                "Cargo.toml",
                ("crates/root/Cargo.toml",),
            )
        )
    )
    envelopes = result["cargo"]
    if not isinstance(envelopes, dict):
        pytest.fail("Cargo envelope is absent")
    old = {node["path"] for node in envelopes["baseline"]["nodes"]}
    new = {node["path"] for node in envelopes["target"]["nodes"]}
    if "crates/new/src/lib.rs" in old or "crates/new/src/lib.rs" not in new:
        raise AssertionError
    changes = result["changes"]
    if not isinstance(changes, list) or not any(
        item["path"] == "crates/new/src/lib.rs" and item["cargo_envelope"]["target"]
        for item in changes
    ):
        raise AssertionError
    if (
        result["status"] != "unknown"
        or (root / "crates/new/src/lib.rs").read_text() != "// dirty\n"
    ):
        raise AssertionError


def test_missing_workspace_member_and_symlink_dependency_are_gaps(
    workspace: tuple[Path, str],
) -> None:
    """Unresolved workspace members and symlink paths never become an empty closure."""
    root, _ = workspace
    (root / "crates/link").symlink_to("shared", target_is_directory=True)
    manifest = root / "Cargo.toml"
    manifest.write_text(manifest.read_text().replace('"crates/*"', '"crates/*", "absent/*"'))
    crate = root / "crates/root/Cargo.toml"
    crate.write_text(crate.read_text() + '\n[dev-dependencies.linked]\npath = "../link"\n')
    commit = _commit(root, "Add unresolved dependencies")
    gaps = set(_derive(root, commit).gaps)
    if (
        "workspace_member_pattern_unresolved" not in gaps
        or "symlink_or_submodule_source" not in gaps
    ):
        raise AssertionError


def test_invalid_roots_and_missing_commit_refuse(workspace: tuple[Path, str]) -> None:
    """Absent source objects and invalid roots are explicit gaps."""
    root, commit = workspace
    missing_roots = asyncio.run(
        cargo.derive(_GIT, root, commit, cargo.CargoInputs("Cargo.toml", (), cargo.CargoLimits()))
    )
    missing_commit = asyncio.run(
        cargo.derive(
            _GIT,
            root,
            "f" * 40,
            cargo.CargoInputs("Cargo.toml", ("crates/root/Cargo.toml",), cargo.CargoLimits()),
        )
    )
    if "cargo_roots_invalid_or_missing" not in missing_roots.gaps:
        raise AssertionError
    if "git_tree_unavailable_or_budget_exceeded" not in missing_commit.gaps:
        raise AssertionError


def test_version_only_is_a_parsed_fact_not_compatibility(workspace: tuple[Path, str]) -> None:
    """A package-version-only edit is reported without a semantic conclusion."""
    root, baseline = workspace
    path = root / "crates/root/Cargo.toml"
    path.write_text(path.read_text().replace('version = "1.0.0"', 'version = "1.0.1"'))
    target = _commit(root, "Change package version")
    result = asyncio.run(
        assess(
            Request(
                _GIT,
                root,
                baseline,
                target,
                None,
                None,
                None,
                "Cargo.toml",
                ("crates/root/Cargo.toml",),
            )
        )
    )
    rows = result["changes"]
    if not isinstance(rows, list) or len(rows) != 1:
        raise AssertionError
    if rows[0].get("metadata_fact") != "parsed_package_version_only_not_semantic_equivalence":
        raise AssertionError
    if result["status"] != "unknown" or result["execution_contract_change"] != "unreviewed":
        raise AssertionError


def test_cli_exposes_both_snapshot_envelopes(workspace: tuple[Path, str]) -> None:
    """The command line exposes bounded Git-object envelopes and their gaps."""
    root, baseline = workspace
    _write(root, "crates/root/src/lib.rs", "// changed\n")
    target = _commit(root, "Change source")
    project = Path(__file__).resolve().parents[2]
    environment = {
        **_ENV,
        "PYTHONPATH": f"{project / 'src'}:{project}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    raw = _run(
        project,
        [
            sys.executable,
            "-m",
            "tools.compatibility",
            "--git",
            str(_GIT),
            "--repository",
            str(root),
            "--baseline",
            baseline,
            "--target",
            target,
            "--workspace",
            "Cargo.toml",
            "--cargo-root",
            "crates/root/Cargo.toml",
        ],
        environment,
    )
    report = json.loads(raw)
    if report["status"] != "unknown" or set(report["cargo"]) != {"baseline", "target"}:
        raise AssertionError
    if "macro_codegen_and_caller_coverage_unresolved" not in report["gaps"]:
        raise AssertionError


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("Cargo.lock", "version = 4\n# changed lock\n"),
        ("rust-toolchain.toml", '[toolchain]\nchannel = "nightly"\n'),
        ("crates/extra/data.bin", "changed included data\n"),
    ],
)
def test_lock_toolchain_and_included_data_are_observed(
    workspace: tuple[Path, str], name: str, replacement: str
) -> None:
    """Each changed control or included object appears in both snapshot envelopes."""
    root, baseline = workspace
    _write(root, name, replacement)
    target = _commit(root, "Change consumed source material")
    result = asyncio.run(
        assess(
            Request(
                _GIT,
                root,
                baseline,
                target,
                None,
                None,
                None,
                "Cargo.toml",
                ("crates/root/Cargo.toml",),
            )
        )
    )
    rows = result["changes"]
    if not isinstance(rows, list) or len(rows) != 1:
        raise AssertionError
    if rows[0]["path"] != name or rows[0]["cargo_envelope"] != {"baseline": True, "target": True}:
        raise AssertionError
    if result["captured_cargo_envelope_objects_equal"] is not None:
        raise AssertionError


def test_timeout_preserves_observed_partial_graph(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out derivation retains facts already obtained from the Git tree."""
    root, commit = workspace
    blocked = _git(root, "rev-parse", f"{commit}:crates/shared/Cargo.toml").decode().strip()
    actual = git.blobs

    async def interrupted(
        executable: Path, repository: Path, objects: tuple[tuple[str, int], ...], *, seconds: float
    ) -> tuple[bytes, ...]:
        if any(object_id == blocked for object_id, _ in objects):
            raise TimeoutError
        return await actual(executable, repository, objects, seconds=seconds)

    monkeypatch.setattr(git, "blobs", interrupted)
    envelope = _derive(root, commit)
    if "derivation_timeout" not in envelope.gaps:
        raise AssertionError
    if "git_tree_unavailable_or_budget_exceeded" in envelope.gaps:
        raise AssertionError
    if not envelope.nodes or not envelope.edges or not envelope.features or not envelope.manifests:
        raise AssertionError
    if not envelope.workspace_member_candidates:
        raise AssertionError


def test_report_timeout_keeps_partial_comparison_unknown(
    workspace: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial envelope never turns absent paths or equality into negative facts."""
    root, baseline = workspace
    _write(root, "docs/readme.md", "new documentation\n")
    target = _commit(root, "Add documentation")
    blocked = _git(root, "rev-parse", f"{baseline}:crates/shared/Cargo.toml").decode().strip()
    actual = git.blobs

    async def interrupted(
        executable: Path, repository: Path, objects: tuple[tuple[str, int], ...], *, seconds: float
    ) -> tuple[bytes, ...]:
        if any(object_id == blocked for object_id, _ in objects):
            raise TimeoutError
        return await actual(executable, repository, objects, seconds=seconds)

    monkeypatch.setattr(git, "blobs", interrupted)
    report = asyncio.run(
        assess(
            Request(
                _GIT,
                root,
                baseline,
                target,
                None,
                None,
                None,
                "Cargo.toml",
                ("crates/root/Cargo.toml",),
            )
        )
    )
    rows = report["changes"]
    if report["status"] != "unknown" or report["captured_cargo_envelope_objects_equal"] is not None:
        raise AssertionError
    if not isinstance(rows, list) or len(rows) != 1:
        raise AssertionError
    if rows[0]["cargo_envelope"] != {"baseline": None, "target": None}:
        raise AssertionError
    gaps = report["gaps"]
    if not isinstance(gaps, list) or "derivation_timeout" not in gaps:
        raise AssertionError
