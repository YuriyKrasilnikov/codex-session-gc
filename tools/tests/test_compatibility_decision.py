# Copyright (c) 2026

"""Scoped impact assessments remain exact and explicitly unverified claims."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tools.compatibility import decision, git
from tools.compatibility.report import Request, assess
from tools.tests.test_compatibility import (
    _ENV,
    _GIT,
    _INPUTS,
    _PROFILE,
    _commit,
    _git,
    _run,
    _write,
)

_HELPER = "c" * 64
_EVIDENCE = {"reference": "review/source", "sha256": "d" * 64, "scope": "bounded source"}


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, str, Path]:
    """Create a source workspace and explicit baseline boundary declarations."""
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _write(root, "Cargo.toml", '[workspace]\nmembers = ["crates/core"]\n')
    _write(root, "Cargo.lock", "version = 4\n")
    _write(root, "rust-toolchain.toml", '[toolchain]\nchannel = "stable"\n')
    _write(root, "crates/core/Cargo.toml", '[package]\nname = "core"\nversion = "1.0.0"\n')
    _write(root, "crates/core/src/lib.rs", "// original\n")
    _write(root, "docs/readme.md", "original\n")
    _write(root, "caller/client.rs", "// original caller\n")
    baseline = _commit(root, "Initial source")
    declarations = tmp_path / "boundaries.json"
    declarations.write_text(
        json.dumps(
            {
                "format": "codex-session-gc-compatibility-boundaries-v1",
                "baseline_commit": baseline,
                "baseline_profile_sha256": _PROFILE,
                "baseline_native_inputs_sha256": _INPUTS,
                "boundaries": [
                    {
                        "name": "core",
                        "paths": ["crates/core", "Cargo.lock"],
                        "witnesses": ["witness/core"],
                    },
                    {"name": "caller", "paths": ["caller"], "witnesses": ["witness/caller"]},
                ],
                "outside": ["docs"],
            }
        )
    )
    return root, baseline, declarations


def _report(root: Path, baseline: str, target: str, boundaries: Path) -> dict[str, object]:
    return asyncio.run(
        assess(
            Request(
                _GIT,
                root,
                baseline,
                target,
                boundaries,
                _PROFILE,
                _INPUTS,
                "Cargo.toml",
                ("crates/core/Cargo.toml",),
                _HELPER,
            )
        )
    )


def _assessment(report: dict[str, object], disposition: str = "not_relevant") -> dict[str, object]:
    baseline, target, cargo = report["baseline"], report["target"], report["cargo"]
    if (
        not isinstance(baseline, dict)
        or not isinstance(target, dict)
        or not isinstance(cargo, dict)
    ):
        pytest.fail("Expected complete source report")
    changes = report["changes"]
    report_gaps = report["gaps"]
    if not isinstance(changes, list) or not isinstance(report_gaps, list):
        pytest.fail("Expected observed changes and gaps")
    gap_rows: list[dict[str, object]] = []
    for scope, gaps in (
        ("report", report_gaps),
        ("baseline", cargo["baseline"]["gaps"]),
        ("target", cargo["target"]["gaps"]),
    ):
        gap_rows.extend(
            {
                "scope": scope,
                "code": code,
                "disposition": "resolved",
                "rationale": "Reviewed within the stated scope.",
                "evidence": [_EVIDENCE],
            }
            for code in gaps
        )
    fields = ("path", "status", "old_mode", "new_mode", "old_object", "new_object")
    return {
        "format": "codex-session-gc-impact-assessment-v1",
        "report_sha256": decision.report_sha256(report),
        "baseline_commit": baseline["commit"],
        "target_commit": target["commit"],
        "execution": {
            "helper_sha256": _HELPER,
            "profile_sha256": _PROFILE,
            "native_inputs_sha256": _INPUTS,
        },
        "closures": {
            "baseline_sha256": decision.closure_sha256(cargo["baseline"]),
            "target_sha256": decision.closure_sha256(cargo["target"]),
        },
        "scope": "closed operations on the declared source boundary",
        "review": {**_EVIDENCE, "scope": "closed operations on the declared source boundary"},
        "coverage_complete": True,
        "changes": [
            {
                **{key: item[key] for key in fields},
                "disposition": disposition,
                "rationale": "Scoped source review.",
                "evidence": [_EVIDENCE],
                "boundaries": [
                    {
                        "name": boundary["name"],
                        "disposition": disposition,
                        "rationale": "Scoped boundary review.",
                        "evidence": [
                            {"reference": reference, "sha256": "d" * 64, "scope": "bounded source"}
                            for reference in boundary["witnesses"]
                        ],
                    }
                    for boundary in item["boundaries"]
                ],
            }
            for item in changes
        ],
        "gaps": gap_rows,
    }


def _evaluate(report: dict[str, object], review: dict[str, object]) -> decision.Decision:
    return decision.evaluate(report, decision.parse_assessment(json.dumps(review).encode()))


def test_reviewed_ui_only_candidate_preserves_unverified_boundary(
    source: tuple[Path, str, Path],
) -> None:
    """A complete supplied scoped review can classify an outside-only source delta."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed documentation\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    result = _evaluate(report, _assessment(report))
    if result.impact is not decision.Impact.NO_RELEVANT_DELTA:
        raise AssertionError
    if (
        result.review_origin != "supplied_unverified"
        or result.assessment_sha256 is None
        or result.scope != "closed operations on the declared source boundary"
        or result.review is None
        or result.review_claim_verified
        or result.authorizes_operation
        or report["status"] != "unknown"
    ):
        raise AssertionError


def test_workspace_membership_gap_requires_scoped_evidence(
    source: tuple[Path, str, Path],
) -> None:
    """Implicit path dependencies need evidence before a supplied review can close the gap."""
    root, _, boundaries = source
    _write(
        root,
        "Cargo.toml",
        '[workspace]\nmembers = ["crates/core"]\n[workspace.dependencies]\nshared = "1"\n',
    )
    _write(
        root,
        "crates/core/Cargo.toml",
        '[package]\nname = "core"\nversion = "1.0.0"\n'
        '[dependencies]\nimplicit = { path = "../implicit" }\n',
    )
    _write(
        root,
        "crates/implicit/Cargo.toml",
        '[package]\nname = "implicit"\nversion = "1.0.0"\n'
        "[dependencies]\nshared = { workspace = true }\n",
    )
    _write(root, "crates/implicit/src/lib.rs", "// source\n")
    baseline = _commit(root, "Add workspace dependency")
    declaration = json.loads(boundaries.read_text())
    declaration["baseline_commit"] = baseline
    boundaries.write_text(json.dumps(declaration))
    _write(root, "docs/readme.md", "reviewed documentation\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    report_gaps = report["gaps"]
    if not isinstance(report_gaps, list):
        pytest.fail("Expected scoped gaps")
    if "workspace_membership_unresolved" not in report_gaps:
        raise AssertionError
    review = _assessment(report)
    if _evaluate(report, review).impact is not decision.Impact.NO_RELEVANT_DELTA:
        raise AssertionError
    gaps = review["gaps"]
    if not isinstance(gaps, list):
        pytest.fail("Expected scoped gaps")
    for gap in gaps:
        if gap["code"] == "workspace_membership_unresolved":
            gap["disposition"] = "unresolved"
            gap["evidence"] = []
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


@pytest.mark.parametrize(
    ("path", "content", "disposition", "expected"),
    [
        (
            "crates/core/migrations/0062.sql",
            "ALTER TABLE state ADD COLUMN parent TEXT;\n",
            "execution_change",
            decision.Impact.REQUIRES_EXECUTION_CHANGE,
        ),
        (
            "Cargo.lock",
            "version = 4\n# changed dependency\n",
            "review_required",
            decision.Impact.REVIEW_REQUIRED,
        ),
        (
            "crates/core/build.rs",
            "fn main() {}\n",
            "review_required",
            decision.Impact.REVIEW_REQUIRED,
        ),
        (
            "caller/client.rs",
            "// changed caller\n",
            "review_required",
            decision.Impact.REVIEW_REQUIRED,
        ),
    ],
)
def test_relevant_contracts_require_explicit_disposition(
    source: tuple[Path, str, Path],
    path: str,
    content: str,
    disposition: str,
    expected: decision.Impact,
) -> None:
    """Migration, dependency, build and caller changes do not imply an automatic rebuild."""
    root, baseline, boundaries = source
    _write(root, path, content)
    target = _commit(root, "Change source boundary")
    report = _report(root, baseline, target, boundaries)
    result = _evaluate(report, _assessment(report, disposition))
    if result.impact is not expected or result.authorizes_operation:
        raise AssertionError


def test_moved_module_requires_both_object_dispositions(source: tuple[Path, str, Path]) -> None:
    """A move retains its deletion and addition until both are reviewed."""
    root, baseline, boundaries = source
    (root / "crates/core/src/lib.rs").rename(root / "crates/core/src/moved.rs")
    target = _commit(root, "Move module")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report, "review_required")
    changes = review["changes"]
    expected_count = 2
    if not isinstance(changes, list) or len(changes) != expected_count:
        raise AssertionError
    changes.pop()
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError
    if (
        _evaluate(report, _assessment(report, "review_required")).impact
        is not decision.Impact.REVIEW_REQUIRED
    ):
        raise AssertionError


def test_reviewed_relevant_change_preserves_execution_identity(
    source: tuple[Path, str, Path],
) -> None:
    """A reviewed relevant caller change differs from an irrelevant source change."""
    root, baseline, boundaries = source
    _write(root, "caller/client.rs", "// changed caller\n")
    target = _commit(root, "Change caller")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report, "reviewed_compatible")
    result = _evaluate(report, review)
    if (
        result.impact is not decision.Impact.REVIEWED_SAME_EXECUTION
        or result.review_claim_verified
        or result.authorizes_operation
        or report["status"] != "unknown"
    ):
        raise AssertionError
    changes = review["changes"]
    if not isinstance(changes, list):
        pytest.fail("Expected changed source")
    changes[0]["evidence"] = []
    with pytest.raises(decision.AssessmentError):
        _evaluate(report, review)


@pytest.mark.parametrize(
    ("disposition", "expected"),
    [
        ("not_relevant", decision.Impact.REVIEWED_SAME_EXECUTION),
        ("review_required", decision.Impact.REVIEW_REQUIRED),
        ("execution_change", decision.Impact.REQUIRES_EXECUTION_CHANGE),
        ("unknown", decision.Impact.UNKNOWN),
    ],
)
def test_reviewed_compatible_does_not_override_other_findings(
    source: tuple[Path, str, Path], disposition: str, expected: decision.Impact
) -> None:
    """An unresolved or incompatible second change retains its stronger outcome."""
    root, baseline, boundaries = source
    _write(root, "caller/client.rs", "// changed caller\n")
    _write(root, "docs/readme.md", "changed documentation\n")
    target = _commit(root, "Change caller and documentation")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report, "reviewed_compatible")
    changes = review["changes"]
    expected_count = 2
    if not isinstance(changes, list) or len(changes) != expected_count:
        pytest.fail("Expected both source changes")
    changes[1]["disposition"] = disposition
    if _evaluate(report, review).impact is not expected:
        raise AssertionError


def test_unresolved_or_missing_gap_and_stale_binding_are_unknown(
    source: tuple[Path, str, Path],
) -> None:
    """Missing dispositions and changed evidence cannot inherit a prior claim."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report)
    gaps = review["gaps"]
    if not isinstance(gaps, list) or not gaps:
        raise AssertionError
    gaps[0]["disposition"] = "unresolved"
    gaps[0]["evidence"] = []
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError
    review = _assessment(report)
    review["target_commit"] = "f" * 40
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError
    review = _assessment(report)
    review["gaps"] = []
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


def test_missing_blob_cannot_be_reviewed_away(source: tuple[Path, str, Path]) -> None:
    """Incomplete physical source observations always retain unknown impact."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "new bytes\n")
    target = _commit(root, "Update documentation")
    blob = _git(root, "rev-parse", f"{target}:docs/readme.md").decode().strip()
    (root / ".git/objects" / blob[:2] / blob[2:]).unlink()
    report = _report(root, baseline, target, boundaries)
    if report.get("changed_blobs_checked") is not False:
        raise AssertionError
    if _evaluate(report, _assessment(report)).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


def test_timeout_and_unknown_gap_cannot_be_reviewed_away(source: tuple[Path, str, Path]) -> None:
    """Resource truncation and unrecognized gaps are fail-closed."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    cargo = report["cargo"]
    if not isinstance(cargo, dict):
        pytest.fail("Expected Cargo envelopes")
    cargo["baseline"]["gaps"] = [*cargo["baseline"]["gaps"], "derivation_timeout"]
    report_gaps = report["gaps"]
    if not isinstance(report_gaps, list):
        pytest.fail("Expected coverage gaps")
    report_gaps.append("derivation_timeout")
    if _evaluate(report, _assessment(report)).impact is not decision.Impact.UNKNOWN:
        raise AssertionError
    report = _report(root, baseline, target, boundaries)
    report_gaps = report["gaps"]
    if not isinstance(report_gaps, list):
        pytest.fail("Expected coverage gaps")
    report_gaps.append("new_unrecognized_gap")
    if _evaluate(report, _assessment(report)).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


def test_review_schema_rejects_ambiguous_and_unsupported_fields(
    source: tuple[Path, str, Path],
) -> None:
    """A review claim must be bounded, unambiguous and include scoped evidence."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report)
    review["extra"] = True
    with pytest.raises(decision.AssessmentError, match="fields"):
        decision.parse_assessment(json.dumps(review).encode())
    with pytest.raises(decision.AssessmentError, match=r"Invalid|Duplicate"):
        decision.parse_assessment(b'{"format":"x","format":"y"}')
    review = _assessment(report)
    changes = review["changes"]
    if not isinstance(changes, list):
        pytest.fail("Expected changed objects")
    changes[0]["evidence"] = []
    with pytest.raises(decision.AssessmentError, match="evidence"):
        decision.parse_assessment(json.dumps(review).encode())


def test_cli_preserves_observed_report_and_labels_review_claim(
    source: tuple[Path, str, Path], tmp_path: Path
) -> None:
    """CLI decision does not replace raw source evidence or grant authority."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    review = tmp_path / "assessment.json"
    review.write_text(json.dumps(_assessment(report)))
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
            str(boundaries),
            "--profile-sha256",
            _PROFILE,
            "--native-inputs-sha256",
            _INPUTS,
            "--helper-sha256",
            _HELPER,
            "--workspace",
            "Cargo.toml",
            "--cargo-root",
            "crates/core/Cargo.toml",
            "--assessment",
            str(review),
        ],
        environment,
    )
    actual = json.loads(output)
    if actual["report"] != json.loads(decision.encode_report(report)):
        raise AssertionError
    if (
        actual["decision"]["impact"] != "no_relevant_delta"
        or actual["decision"]["review_origin"] != "supplied_unverified"
        or actual["decision"]["review_claim_verified"]
        or actual["decision"]["execution_identity_verified"]
        or actual["decision"]["authorizes_operation"]
    ):
        raise AssertionError


def test_affected_boundary_and_declared_witness_cannot_be_omitted(
    source: tuple[Path, str, Path],
) -> None:
    """Every observed affected boundary needs its named witness disposition."""
    root, baseline, boundaries = source
    _write(root, "caller/client.rs", "// changed caller\n")
    target = _commit(root, "Change caller")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report, "review_required")
    changes = review["changes"]
    if not isinstance(changes, list):
        pytest.fail("Expected changed objects")
    changes[0]["boundaries"] = []
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError
    review = _assessment(report, "review_required")
    changes = review["changes"]
    if not isinstance(changes, list):
        pytest.fail("Expected changed objects")
    changes[0]["boundaries"][0]["evidence"][0]["reference"] = "different/witness"
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


def test_review_scope_and_closure_bindings_are_exact(source: tuple[Path, str, Path]) -> None:
    """A review for another scope or closure cannot classify the observed source."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report)
    review["review"] = {**_EVIDENCE, "scope": "unrelated scope"}
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError
    review = _assessment(report)
    closures = review["closures"]
    if not isinstance(closures, dict):
        pytest.fail("Expected closure identities")
    closures["target_sha256"] = "1" * 64
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


def test_partial_blob_observation_preserves_verified_ids(
    source: tuple[Path, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded read failure retains prior blob evidence but cannot classify impact."""
    root, baseline, boundaries = source
    for index in range(70):
        _write(root, f"docs/change-{index}.md", f"content {index}\n")
    target = _commit(root, "Update documentation")
    original = git.blobs
    calls = 0
    timeout_call = 2
    first_batch: list[str] = []

    async def interrupted(
        executable: Path, repository: Path, objects: tuple[tuple[str, int], ...], *, seconds: float
    ) -> tuple[bytes, ...]:
        nonlocal calls
        calls += 1
        if calls == timeout_call:
            raise TimeoutError
        result = await original(executable, repository, objects, seconds=seconds)
        if calls == 1:
            first_batch.extend(object_id for object_id, _ in objects)
        return result

    monkeypatch.setattr(git, "blobs", interrupted)
    report = _report(root, baseline, target, boundaries)
    observation = report["changed_blob_observation"]
    if not isinstance(observation, dict):
        pytest.fail("Expected bounded blob observation")
    if observation["complete"] or observation["failure"] != "object_timeout":
        raise AssertionError
    if not first_batch or tuple(observation["object_ids"]) != tuple(first_batch):
        raise AssertionError
    if _evaluate(report, _assessment(report)).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


def test_evidence_references_reject_credentials(source: tuple[Path, str, Path]) -> None:
    """A review can cite public HTTPS evidence but cannot carry credential URLs."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report)
    review["review"] = {
        "reference": "https://example.org/source#L1",
        "sha256": "d" * 64,
        "scope": "closed operations on the declared source boundary",
    }
    decision.parse_assessment(json.dumps(review).encode())
    review["review"] = {
        "reference": "https://user:secret@example.org/source",
        "sha256": "d" * 64,
        "scope": "closed operations on the declared source boundary",
    }
    with pytest.raises(decision.AssessmentError, match="reference"):
        decision.parse_assessment(json.dumps(review).encode())


def test_inconsistent_blob_observation_cannot_be_relabelled_complete(
    source: tuple[Path, str, Path],
) -> None:
    """A claimed complete observation must name every actual changed object."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    observation = report["changed_blob_observation"]
    if not isinstance(observation, dict):
        pytest.fail("Expected blob observation")
    observation["object_ids"] = []
    if _evaluate(report, _assessment(report)).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


def test_lfs_pointer_is_not_a_complete_source_blob(source: tuple[Path, str, Path]) -> None:
    """A Git LFS pointer is not a verified copy of its external source content."""
    root, baseline, boundaries = source
    _write(
        root,
        "docs/readme.md",
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "1" * 64 + "\nsize 1\n",
    )
    target = _commit(root, "Replace document with LFS pointer")
    report = _report(root, baseline, target, boundaries)
    observation = report["changed_blob_observation"]
    if not isinstance(observation, dict) or observation["failure"] != "git_lfs_content_unavailable":
        raise AssertionError
    if _evaluate(report, _assessment(report)).impact is not decision.Impact.UNKNOWN:
        raise AssertionError


@pytest.mark.parametrize("field", ["helper_sha256", "profile_sha256", "native_inputs_sha256"])
def test_each_execution_binding_must_match(source: tuple[Path, str, Path], field: str) -> None:
    """A supplied review for different execution bytes cannot classify this report."""
    root, baseline, boundaries = source
    _write(root, "docs/readme.md", "changed\n")
    target = _commit(root, "Update documentation")
    report = _report(root, baseline, target, boundaries)
    review = _assessment(report)
    execution = review["execution"]
    if not isinstance(execution, dict):
        pytest.fail("Expected execution identities")
    execution[field] = "1" * 64
    if _evaluate(report, review).impact is not decision.Impact.UNKNOWN:
        raise AssertionError
