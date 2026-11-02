# Copyright (c) 2026

"""Compatibility evidence uses real disposable Git repositories."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.installation.process import ProcessLimits, run_owned
from tools.compatibility import git
from tools.compatibility.report import ObservationLimits, Request, assess

if TYPE_CHECKING:
    from collections.abc import Mapping

_GIT = Path("/usr/bin/git")
_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}
_LIMITS = ProcessLimits(seconds=30, output_bytes=1024 * 1024)
_PROFILE = "a" * 64
_INPUTS = "b" * 64


def test_batched_git_blobs_preserve_bytes_and_refuse_invalid_objects(tmp_path: Path) -> None:
    """Binary, empty, repeated and large blobs retain exact order and identity."""
    root = tmp_path / "git"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    bodies = (b"", bytes(range(256)), b"x" * (1024 * 1024 + 7))
    for index, body in enumerate(bodies):
        (root / f"{index}.bin").write_bytes(body)
    commit = _commit(root, "Store binary fixtures")
    entries = asyncio.run(git.tree(_GIT, root, commit))
    observed = {entry.path: (entry.object_id, entry.size) for entry in entries}
    objects = tuple((observed[f"{index}.bin"][0], len(body)) for index, body in enumerate(bodies))
    request = (*objects, objects[1])
    actual = asyncio.run(git.blobs(_GIT, root, request, seconds=30))
    if actual != (*bodies, bodies[1]):
        raise AssertionError
    for invalid in ((("f" * 40, 1),), ((commit, 1),), ((objects[1][0], 1),)):
        with pytest.raises(git.SourceError):
            asyncio.run(git.blobs(_GIT, root, invalid, seconds=30))


def test_batched_git_timeout_refuses(tmp_path: Path) -> None:
    """A stalled batch process is terminated and reported as unavailable."""
    executable = tmp_path / "git-fixture"
    executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    executable.chmod(0o700)
    with pytest.raises(git.SourceError):
        asyncio.run(git.blobs(executable, tmp_path, (("a" * 40, 1),), seconds=0.05))


def test_invocation_cache_overflow_preserves_verified_blob(tmp_path: Path) -> None:
    """A full observation cache refuses new bytes and retains existing evidence."""
    root = tmp_path / "git"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _write(root, "one", "abc")
    _write(root, "two", "def")
    commit = _commit(root, "Store files")
    entries = asyncio.run(git.tree(_GIT, root, commit))
    objects = {item.path: (item.object_id, item.size) for item in entries}
    context = git.ObservationContext(_GIT, root, cache_bytes=3, seconds=30)

    async def observe() -> None:
        if await context.read(((objects["one"][0], 3),)) != (b"abc",):
            raise AssertionError
        with pytest.raises(git.SourceError, match="cache limit"):
            await context.read(((objects["two"][0], 3),))
        if await context.read(((objects["one"][0], 3),)) != (b"abc",):
            raise AssertionError

    asyncio.run(observe())


def test_explicit_object_budget_reports_unknown(repository: tuple[Path, str, Path]) -> None:
    """A caller-selected object bound cannot turn incomplete observation into a decision."""
    root, baseline, _ = repository
    _write(root, "storage/sql.rs", "changed storage\n")
    target = _commit(root, "Change storage")
    request = Request(
        _GIT,
        root,
        baseline,
        target,
        None,
        None,
        None,
        limits=ObservationLimits(objects=1),
    )
    report = asyncio.run(assess(request))
    observation = report["changed_blob_observation"]
    if (
        report["status"] != "unknown"
        or not isinstance(observation, dict)
        or observation.get("failure") != "object_count_limit"
    ):
        raise AssertionError


def _run(cwd: Path, arguments: list[str], environment: Mapping[str, str] = _ENV) -> bytes:
    return asyncio.run(run_owned(arguments, cwd=cwd, environment=dict(environment), limits=_LIMITS))


def _git(cwd: Path, *arguments: str) -> bytes:
    return _run(cwd, [str(_GIT), *arguments])


def _write(root: Path, name: str, content: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _commit(root: Path, title: str) -> str:
    _git(root, "add", "--all")
    _git(root, "commit", "--quiet", "-m", title)
    return _git(root, "rev-parse", "HEAD").decode().strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str, Path]:
    """Make a tiny source repository and a separate immutable baseline profile."""
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    for name in (
        "docs/readme.md",
        "ui/main.rs",
        "storage/sql.rs",
        "protocol/types.rs",
        "writer/lib.rs",
        "callers/client.rs",
        "Cargo.toml",
        "Cargo.lock",
        "src/original.rs",
    ):
        _write(root, name, f"baseline {name}\n")
    baseline = _commit(root, "Initial source")
    profile = tmp_path / "raw-profile.json"
    profile.write_bytes(b'{"immutable":"profile"}\n')
    return root, baseline, profile


def _declarations(path: Path, baseline: str) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "codex-session-gc-compatibility-boundaries-v1",
                "baseline_commit": baseline,
                "baseline_profile_sha256": _PROFILE,
                "baseline_native_inputs_sha256": _INPUTS,
                "boundaries": [
                    {"name": "storage", "paths": ["storage"], "witnesses": ["tests/storage"]},
                    {"name": "protocol", "paths": ["protocol"], "witnesses": ["tests/protocol"]},
                    {"name": "writer", "paths": ["writer"], "witnesses": ["tests/writer"]},
                    {"name": "caller", "paths": ["callers"], "witnesses": ["tests/caller"]},
                ],
                "outside": ["docs", "ui"],
            }
        )
    )


def _report(
    root: Path, baseline: str | None, target: str | None, boundary: Path | None
) -> dict[str, object]:
    return asyncio.run(assess(Request(_GIT, root, baseline, target, boundary, _PROFILE, _INPUTS)))


def test_declared_outside_changes_do_not_claim_semantic_proof(
    repository: tuple[Path, str, Path], tmp_path: Path
) -> None:
    """Documentation and UI changes remain observed while closure is unresolved."""
    root, baseline, profile = repository
    before = hashlib.sha256(profile.read_bytes()).hexdigest()
    _write(root, "docs/readme.md", "changed docs\n")
    _write(root, "ui/main.rs", "changed interface\n")
    target = _commit(root, "Update documents and interface")
    boundary = tmp_path / "boundaries.json"
    _declarations(boundary, baseline)
    report = _report(root, baseline, target, boundary)
    rows = report["changes"]
    expected_count = 2
    if (
        not isinstance(rows, list)
        or len(rows) != expected_count
        or not all(row["declared_outside"] for row in rows)
    ):
        raise AssertionError
    if report["status"] != "unknown" or report["helper_inputs_unchanged"] != "unknown":
        raise AssertionError
    if hashlib.sha256(profile.read_bytes()).hexdigest() != before:
        raise AssertionError


@pytest.mark.parametrize(
    "name", ["storage/sql.rs", "protocol/types.rs", "writer/lib.rs", "callers/client.rs"]
)
def test_consumed_boundary_records_exact_object_ids_and_witness(
    repository: tuple[Path, str, Path], tmp_path: Path, name: str
) -> None:
    """Changes to declared semantic inputs retain their Git identities and witness."""
    root, baseline, _ = repository
    _write(root, name, "changed semantic input\n")
    target = _commit(root, "Change source boundary")
    boundary = tmp_path / "boundaries.json"
    _declarations(boundary, baseline)
    report = _report(root, baseline, target, boundary)
    rows = report["changes"]
    if not isinstance(rows, list) or len(rows) != 1:
        raise AssertionError
    row = rows[0]
    if row["path"] != name or not row["boundaries"] or row["old_object"] == row["new_object"]:
        raise AssertionError


@pytest.mark.parametrize("name", ["Cargo.toml", "Cargo.lock", "src/generated.inc"])
def test_dependency_and_unresolved_source_are_gaps(
    repository: tuple[Path, str, Path], tmp_path: Path, name: str
) -> None:
    """Dependency, lock and generated inputs remain unclassified without a closure."""
    root, baseline, _ = repository
    _write(root, name, "changed dependency or included source\n")
    target = _commit(root, "Change dependency input")
    boundary = tmp_path / "boundaries.json"
    _declarations(boundary, baseline)
    report = _report(root, baseline, target, boundary)
    gaps = report["gaps"]
    if not isinstance(gaps, list) or "unclassified_changed_path" not in gaps:
        raise AssertionError
    if report["status"] != "unknown":
        raise AssertionError


def test_moved_module_is_an_addition_and_deletion(
    repository: tuple[Path, str, Path],
) -> None:
    """A moved file retains both immutable paths without heuristic rename claims."""
    root, baseline, _ = repository
    (root / "src/original.rs").rename(root / "src/moved.rs")
    target = _commit(root, "Move module")
    report = _report(root, baseline, target, None)
    rows = report["changes"]
    if not isinstance(rows, list) or {(row["path"], row["status"]) for row in rows} != {
        ("src/original.rs", "D"),
        ("src/moved.rs", "A"),
    }:
        raise AssertionError


def test_missing_ref_never_becomes_an_empty_diff(repository: tuple[Path, str, Path]) -> None:
    """Unavailable source is distinct from a complete comparison with no changes."""
    root, baseline, _ = repository
    report = _report(root, baseline, "refs/tags/unavailable", None)
    if report["status"] != "unknown" or report["changes"] is not None:
        raise AssertionError


def test_cli_uses_immutable_commits_despite_dirty_worktree(
    repository: tuple[Path, str, Path], tmp_path: Path
) -> None:
    """The JSON command reads committed trees and leaves local working files alone."""
    root, baseline, _ = repository
    _write(root, "docs/readme.md", "committed\n")
    target = _commit(root, "Update committed document")
    _write(root, "docs/readme.md", "uncommitted\n")
    boundary = tmp_path / "boundaries.json"
    _declarations(boundary, baseline)
    project = Path(__file__).resolve().parents[2]
    environment = {
        **_ENV,
        "PYTHONPATH": f"{project / 'src'}:{project}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    output = _run(
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
            "--boundaries",
            str(boundary),
            "--profile-sha256",
            _PROFILE,
            "--native-inputs-sha256",
            _INPUTS,
        ],
        environment,
    )
    report = json.loads(output)
    if (
        report["source_mode"] != "immutable_git_objects_worktree_not_read"
        or len(report["changes"]) != 1
    ):
        raise AssertionError
    expected_blob = _git(root, "rev-parse", f"{target}:docs/readme.md").decode().strip()
    if report["changes"][0]["new_object"] != expected_blob:
        raise AssertionError
    if (root / "docs/readme.md").read_text() != "uncommitted\n":
        raise AssertionError


def test_mismatched_review_declaration_retains_observed_diff(
    repository: tuple[Path, str, Path], tmp_path: Path
) -> None:
    """A declaration for another profile cannot classify this source comparison."""
    root, baseline, _ = repository
    _write(root, "protocol/types.rs", "changed protocol\n")
    target = _commit(root, "Change protocol")
    boundary = tmp_path / "boundaries.json"
    _declarations(boundary, baseline)
    value = json.loads(boundary.read_bytes())
    value["baseline_profile_sha256"] = "c" * 64
    boundary.write_text(json.dumps(value))
    report = _report(root, baseline, target, boundary)
    gaps = report["gaps"]
    if not isinstance(gaps, list) or "baseline_boundary_declarations_invalid" not in gaps:
        raise AssertionError
    changes = report["changes"]
    if not isinstance(changes, list) or len(changes) != 1 or changes[0]["boundaries"]:
        raise AssertionError
