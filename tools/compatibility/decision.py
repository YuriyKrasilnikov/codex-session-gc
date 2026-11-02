# Copyright (c) 2026

"""Evaluate supplied source-review claims against exact compatibility evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, NoReturn
from urllib.parse import urlsplit

from tools.compatibility.cargo import WORKSPACE_MEMBERSHIP_UNRESOLVED

if TYPE_CHECKING:
    from pathlib import Path

_MAX_ASSESSMENT = 4 * 1024 * 1024
_MAX_ITEMS = 16384
_MAX_TEXT = 2048
_CONTROL = 32

# Only these semantic questions can be dispositioned by supplied scoped evidence.
# Unknown and physical-completeness gaps are never silently promoted.
_REVIEWABLE_GAPS = frozenset(
    {
        "cargo_dependency_closure_not_established",
        "macro_build_include_and_caller_coverage_not_established",
        "macro_codegen_and_caller_coverage_unresolved",
        "feature_activation_not_evaluated",
        "build_script_effects_unresolved",
        "external_dependency_source_unresolved",
        "locked_external_source_unresolved",
        "dynamic_include_unresolved",
        "include_literal_unresolved",
        "cargo_configuration_effects_unresolved",
        "cargo_replace_unresolved",
        "workspace_selection_unresolved",
        WORKSPACE_MEMBERSHIP_UNRESOLVED,
        "target_path_outside_crate",
        "build_script_outside_crate",
        "package_version_not_literal",
        "package_metadata_normalization_unavailable",
        "unclassified_changed_path",
        "shallow_repository",
    }
)


class AssessmentError(ValueError):
    """A supplied assessment is malformed or does not describe its report."""


class Impact(StrEnum):
    """A scoped impact classification, never support or cleanup authority."""

    UNKNOWN = "unknown"
    NO_RELEVANT_DELTA = "no_relevant_delta"
    REVIEWED_SAME_EXECUTION = "reviewed_same_execution"
    REVIEW_REQUIRED = "review_required"
    REQUIRES_EXECUTION_CHANGE = "requires_execution_change"


class ChangeDisposition(StrEnum):
    """A supplied review's classification of one changed object."""

    NOT_RELEVANT = "not_relevant"
    REVIEWED_COMPATIBLE = "reviewed_compatible"
    REVIEW_REQUIRED = "review_required"
    EXECUTION_CHANGE = "execution_change"
    UNKNOWN = "unknown"


class GapDisposition(StrEnum):
    """A supplied review's disposition of a coverage gap."""

    RESOLVED = "resolved"
    OUTSIDE_SCOPE = "outside_scope"
    UNRESOLVED = "unresolved"


def _fail(message: str) -> NoReturn:
    raise AssessmentError(message)


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_TEXT
        or "://" in value
        or any(ord(character) < _CONTROL for character in value)
    ):
        _fail("Invalid assessment text.")
    return value


def _reference(value: object) -> str:
    if not isinstance(value, str) or "://" not in value:
        return _text(value)
    if len(value) > _MAX_TEXT or any(ord(character) < _CONTROL for character in value):
        _fail("Invalid evidence reference.")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        message = "Invalid evidence reference."
        raise AssessmentError(message) from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
    ):
        _fail("Invalid evidence reference.")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _fail("Invalid assessment digest.")
    return value


def _commit(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        _fail("Invalid assessment commit.")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("Duplicate assessment field.")
        result[key] = value
    return result


def _object(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        _fail("Unexpected assessment fields.")
    return value


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
    except (TypeError, ValueError, RecursionError) as error:
        message = "Evidence cannot be canonically encoded."
        raise AssessmentError(message) from error


def encode_report(report: dict[str, object]) -> bytes:
    """Encode an observed report deterministically without changing its facts."""
    return _canonical(report)


def report_sha256(report: dict[str, object]) -> str:
    """Hash the exact canonical observed report, without assessing its claims."""
    return hashlib.sha256(encode_report(report)).hexdigest()


def closure_sha256(closure: object) -> str:
    """Hash one complete observed Cargo-envelope projection."""
    return hashlib.sha256(_canonical(closure)).hexdigest()


@dataclass(frozen=True, slots=True)
class Evidence:
    """A scoped evidence reference and its exact byte digest."""

    reference: str
    sha256: str
    scope: str

    def __post_init__(self) -> None:
        """Validate the identity and scope of this evidence reference."""
        _reference(self.reference)
        _digest(self.sha256)
        _text(self.scope)


@dataclass(frozen=True, slots=True)
class BoundaryReview:
    """A supplied disposition and witnesses for one affected boundary."""

    name: str
    disposition: ChangeDisposition
    rationale: str
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        """Validate the named boundary review."""
        _text(self.name)
        _text(self.rationale)
        if not isinstance(self.disposition, ChangeDisposition):
            _fail("Invalid boundary disposition.")
        _validate_evidence(
            self.evidence, required=self.disposition is not ChangeDisposition.UNKNOWN
        )


@dataclass(frozen=True, slots=True)
class ReviewedChange:
    """A supplied disposition of one exact changed Git object."""

    path: str
    status: str
    old_mode: str
    new_mode: str
    old_object: str | None
    new_object: str | None
    disposition: ChangeDisposition
    rationale: str
    evidence: tuple[Evidence, ...]
    boundaries: tuple[BoundaryReview, ...]

    def __post_init__(self) -> None:
        """Validate the exact changed object and its supplied disposition."""
        _text(self.path)
        if (
            not isinstance(self.status, str)
            or self.status not in {"A", "D", "M", "T"}
            or not isinstance(self.old_mode, str)
            or re.fullmatch(r"[0-7]{6}", self.old_mode) is None
            or not isinstance(self.new_mode, str)
            or re.fullmatch(r"[0-7]{6}", self.new_mode) is None
            or not isinstance(self.disposition, ChangeDisposition)
        ):
            _fail("Invalid changed object metadata.")
        _optional_object_id(self.old_object)
        _optional_object_id(self.new_object)
        _text(self.rationale)
        _validate_evidence(
            self.evidence, required=self.disposition is not ChangeDisposition.UNKNOWN
        )
        if (
            not isinstance(self.boundaries, tuple)
            or len(self.boundaries) > _MAX_ITEMS
            or any(not isinstance(item, BoundaryReview) for item in self.boundaries)
            or len({item.name for item in self.boundaries}) != len(self.boundaries)
        ):
            _fail("Invalid affected boundary reviews.")


@dataclass(frozen=True, slots=True)
class ReviewedGap:
    """A supplied disposition of a scoped coverage gap."""

    scope: str
    code: str
    disposition: GapDisposition
    rationale: str
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        """Validate this scoped coverage disposition."""
        if (
            not isinstance(self.scope, str)
            or self.scope not in {"report", "baseline", "target"}
            or not isinstance(self.disposition, GapDisposition)
        ):
            _fail("Invalid coverage gap disposition.")
        _text(self.code)
        _text(self.rationale)
        _validate_evidence(
            self.evidence, required=self.disposition is not GapDisposition.UNRESOLVED
        )


@dataclass(frozen=True, slots=True)
class Assessment:
    """An unverified review claim tied to exact source and execution inputs."""

    report_sha256: str
    baseline_commit: str
    target_commit: str
    helper_sha256: str
    profile_sha256: str
    native_inputs_sha256: str
    baseline_closure_sha256: str
    target_closure_sha256: str
    scope: str
    review: Evidence
    coverage_complete: bool
    changes: tuple[ReviewedChange, ...]
    gaps: tuple[ReviewedGap, ...]

    def __post_init__(self) -> None:
        """Validate all source, execution, review and coverage bindings."""
        for value in (
            self.report_sha256,
            self.helper_sha256,
            self.profile_sha256,
            self.native_inputs_sha256,
            self.baseline_closure_sha256,
            self.target_closure_sha256,
        ):
            _digest(value)
        _commit(self.baseline_commit)
        _commit(self.target_commit)
        _text(self.scope)
        if (
            not isinstance(self.review, Evidence)
            or type(self.coverage_complete) is not bool
            or not isinstance(self.changes, tuple)
            or len(self.changes) > _MAX_ITEMS
            or any(not isinstance(item, ReviewedChange) for item in self.changes)
            or not isinstance(self.gaps, tuple)
            or len(self.gaps) > _MAX_ITEMS
            or any(not isinstance(item, ReviewedGap) for item in self.gaps)
        ):
            _fail("Invalid assessment bindings or dispositions.")


@dataclass(frozen=True, slots=True)
class Decision:
    """Impact from supplied review claims, not a verified support decision."""

    impact: Impact
    report_sha256: str
    reasons: tuple[str, ...]
    assessment_sha256: str | None = None
    scope: str | None = None
    review: Evidence | None = None

    @property
    def review_origin(self) -> Literal["supplied_unverified"]:
        """The review is supplied data, not an authenticated authority."""
        return "supplied_unverified"

    @property
    def review_claim_verified(self) -> Literal[False]:
        """A supplied assessment does not authenticate its review claim."""
        return False

    @property
    def execution_identity_verified(self) -> Literal[False]:
        """The supplied execution identities are compared, not independently observed."""
        return False

    @property
    def authorizes_operation(self) -> Literal[False]:
        """A source-impact result never authorizes cleanup or qualification."""
        return False


def _evidence(value: object) -> Evidence:
    item = _object(value, {"reference", "sha256", "scope"})
    return Evidence(_reference(item["reference"]), _digest(item["sha256"]), _text(item["scope"]))


def _validate_evidence(items: tuple[Evidence, ...], *, required: bool) -> None:
    if (
        not isinstance(items, tuple)
        or (required and not items)
        or len(items) > _MAX_ITEMS
        or any(not isinstance(item, Evidence) for item in items)
        or len(set(items)) != len(items)
    ):
        _fail("Invalid assessment evidence references.")


def _evidence_list(value: object, *, required: bool) -> tuple[Evidence, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        _fail("Invalid assessment evidence references.")
    items = tuple(_evidence(item) for item in value)
    _validate_evidence(items, required=required)
    return items


def _optional_object_id(value: object) -> str | None:
    return None if value is None else _commit(value)


def _boundary(value: object) -> BoundaryReview:
    item = _object(value, {"name", "disposition", "rationale", "evidence"})
    try:
        disposition = ChangeDisposition(_text(item["disposition"]))
    except ValueError as error:
        message = "Invalid boundary disposition."
        raise AssessmentError(message) from error
    return BoundaryReview(
        _text(item["name"]),
        disposition,
        _text(item["rationale"]),
        _evidence_list(item["evidence"], required=disposition is not ChangeDisposition.UNKNOWN),
    )


def _boundaries(value: object) -> tuple[BoundaryReview, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        _fail("Invalid affected boundary reviews.")
    return tuple(_boundary(item) for item in value)


def _change(value: object) -> ReviewedChange:
    item = _object(
        value,
        {
            "path",
            "status",
            "old_mode",
            "new_mode",
            "old_object",
            "new_object",
            "disposition",
            "rationale",
            "evidence",
            "boundaries",
        },
    )
    status = _text(item["status"])
    old_mode = _text(item["old_mode"])
    new_mode = _text(item["new_mode"])
    if (
        status not in {"A", "D", "M", "T"}
        or re.fullmatch(r"[0-7]{6}", old_mode) is None
        or re.fullmatch(r"[0-7]{6}", new_mode) is None
    ):
        _fail("Invalid changed object metadata.")
    try:
        disposition = ChangeDisposition(_text(item["disposition"]))
    except ValueError as error:
        message = "Invalid change disposition."
        raise AssessmentError(message) from error
    return ReviewedChange(
        _text(item["path"]),
        status,
        old_mode,
        new_mode,
        _optional_object_id(item["old_object"]),
        _optional_object_id(item["new_object"]),
        disposition,
        _text(item["rationale"]),
        _evidence_list(item["evidence"], required=disposition is not ChangeDisposition.UNKNOWN),
        _boundaries(item["boundaries"]),
    )


def _gap(value: object) -> ReviewedGap:
    item = _object(value, {"scope", "code", "disposition", "rationale", "evidence"})
    scope = _text(item["scope"])
    if scope not in {"report", "baseline", "target"}:
        _fail("Invalid coverage gap scope.")
    try:
        disposition = GapDisposition(_text(item["disposition"]))
    except ValueError as error:
        message = "Invalid coverage gap disposition."
        raise AssessmentError(message) from error
    return ReviewedGap(
        scope,
        _text(item["code"]),
        disposition,
        _text(item["rationale"]),
        _evidence_list(item["evidence"], required=disposition is not GapDisposition.UNRESOLVED),
    )


def parse_assessment(raw: bytes) -> Assessment:
    """Parse a bounded supplied review claim without authenticating its author."""
    if not isinstance(raw, bytes) or not raw or len(raw) > _MAX_ASSESSMENT:
        _fail("Assessment size is outside its bound.")
    try:
        parsed: object = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        message = "Invalid assessment JSON."
        raise AssessmentError(message) from error
    data = _object(
        parsed,
        {
            "format",
            "report_sha256",
            "baseline_commit",
            "target_commit",
            "execution",
            "closures",
            "scope",
            "review",
            "coverage_complete",
            "changes",
            "gaps",
        },
    )
    if (
        data["format"] != "codex-session-gc-impact-assessment-v1"
        or type(data["coverage_complete"]) is not bool
    ):
        _fail("Unsupported assessment format or coverage claim.")
    execution = _object(
        data["execution"], {"helper_sha256", "profile_sha256", "native_inputs_sha256"}
    )
    closures = _object(data["closures"], {"baseline_sha256", "target_sha256"})
    changes, gaps = data["changes"], data["gaps"]
    if (
        not isinstance(changes, list)
        or not isinstance(gaps, list)
        or len(changes) > _MAX_ITEMS
        or len(gaps) > _MAX_ITEMS
    ):
        _fail("Assessment collections exceed their bound.")
    return Assessment(
        _digest(data["report_sha256"]),
        _commit(data["baseline_commit"]),
        _commit(data["target_commit"]),
        _digest(execution["helper_sha256"]),
        _digest(execution["profile_sha256"]),
        _digest(execution["native_inputs_sha256"]),
        _digest(closures["baseline_sha256"]),
        _digest(closures["target_sha256"]),
        _text(data["scope"]),
        _evidence(data["review"]),
        data["coverage_complete"],
        tuple(_change(item) for item in changes),
        tuple(_gap(item) for item in gaps),
    )


def read_assessment(path: Path) -> Assessment:
    """Read one bounded regular assessment without authenticating it."""
    if path.is_symlink() or not path.is_file():
        _fail("Assessment is not a regular file.")
    with path.open("rb") as source:
        return parse_assessment(source.read(_MAX_ASSESSMENT + 1))


def _report_object(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _observation_matches(value: object, expected: set[str]) -> bool:
    if not isinstance(value, dict) or set(value) != {"object_ids", "complete", "failure"}:
        return False
    identities = value["object_ids"]
    return (
        value["complete"] is True
        and value["failure"] is None
        and isinstance(identities, (list, tuple))
        and all(isinstance(item, str) for item in identities)
        and len(identities) == len(set(identities))
        and set(identities) == expected
    )


def _physical_observations_match(
    report: dict[str, object],
    old: dict[str, object],
    new: dict[str, object],
    changes: list[object],
) -> bool:
    changed: set[str] = set()
    for item in changes:
        if not isinstance(item, dict) or "160000" in (item.get("old_mode"), item.get("new_mode")):
            return False
        for key in ("old_object", "new_object"):
            value = item.get(key)
            if value is not None:
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
                    return False
                changed.add(value)
    closure = _closure_ids(old, new)
    return (
        closure is not None
        and _observation_matches(report.get("changed_blob_observation"), changed)
        and _observation_matches(report.get("closure_blob_observation"), closure)
    )


def _closure_ids(old: dict[str, object], new: dict[str, object]) -> set[str] | None:
    closure: set[str] = set()
    for envelope in (old, new):
        nodes = envelope.get("nodes")
        if not isinstance(nodes, (list, tuple)) or not nodes:
            return None
        for node in nodes:
            if not isinstance(node, dict):
                return None
            object_id = node.get("object_id")
            if not isinstance(object_id, str) or re.fullmatch(r"[0-9a-f]{40}", object_id) is None:
                return None
            closure.add(object_id)
    return closure


def _report_context(
    report: dict[str, object],
    assessment: Assessment,
    observed_sha: str,
) -> tuple[dict[str, object], dict[str, object], list[object], list[object]] | None:
    baseline = _report_object(report.get("baseline"))
    target = _report_object(report.get("target"))
    cargo = _report_object(report.get("cargo"))
    if baseline is None or target is None or cargo is None:
        return None
    old, new = _report_object(cargo.get("baseline")), _report_object(cargo.get("target"))
    changes, gaps = report.get("changes"), report.get("gaps")
    if old is None or new is None or not isinstance(changes, list) or not isinstance(gaps, list):
        return None
    if (
        assessment.review.scope != assessment.scope
        or report.get("format") != "codex-session-gc-compatibility-report-v1"
        or report.get("source_mode") != "immutable_git_objects_worktree_not_read"
        or observed_sha != assessment.report_sha256
        or baseline.get("commit") != assessment.baseline_commit
        or target.get("commit") != assessment.target_commit
        or old.get("commit") != assessment.baseline_commit
        or new.get("commit") != assessment.target_commit
        or closure_sha256(old) != assessment.baseline_closure_sha256
        or closure_sha256(new) != assessment.target_closure_sha256
        or report.get("baseline_helper_sha256") != assessment.helper_sha256
        or report.get("baseline_profile_sha256") != assessment.profile_sha256
        or report.get("baseline_native_inputs_sha256") != assessment.native_inputs_sha256
        or report.get("changed_blobs_checked") is not True
        or report.get("closure_blobs_checked") is not True
        or report.get("boundary_authority")
        != "caller_supplied_declarations_not_independently_verified"
        or report.get("execution_identity_authority")
        != "caller_supplied_not_independently_observed"
        or not isinstance(old.get("nodes"), (list, tuple))
        or not old.get("nodes")
        or not isinstance(new.get("nodes"), (list, tuple))
        or not new.get("nodes")
        or not _physical_observations_match(report, old, new, changes)
    ):
        return None
    return old, new, changes, gaps


def _coverage_reason(
    old: dict[str, object],
    new: dict[str, object],
    report_gaps: list[object],
    assessment: Assessment,
) -> str | None:
    old_gaps, new_gaps = old.get("gaps"), new.get("gaps")
    if (
        not isinstance(old_gaps, (list, tuple))
        or not isinstance(new_gaps, (list, tuple))
        or any(not isinstance(item, str) for item in [*report_gaps, *old_gaps, *new_gaps])
    ):
        return "coverage_gaps_invalid"
    expected = {
        (scope, code)
        for scope, entries in (
            ("report", report_gaps),
            ("baseline", old_gaps),
            ("target", new_gaps),
        )
        for code in entries
    }
    reviewed = {(item.scope, item.code) for item in assessment.gaps}
    if len(reviewed) != len(assessment.gaps) or reviewed != expected:
        return "coverage_dispositions_incomplete"
    if not assessment.coverage_complete or any(
        item.disposition is GapDisposition.UNRESOLVED or item.code not in _REVIEWABLE_GAPS
        for item in assessment.gaps
    ):
        return "coverage_unresolved"
    return None


def _boundary_reviews_match(value: object, reviewed: tuple[BoundaryReview, ...]) -> bool:
    if not isinstance(value, list):
        return False
    observed: dict[str, set[str]] = {}
    for boundary in value:
        if not isinstance(boundary, dict) or set(boundary) != {"name", "witnesses"}:
            return False
        name, witnesses = boundary["name"], boundary["witnesses"]
        if not isinstance(name, str) or name in observed or not isinstance(witnesses, list):
            return False
        if any(not isinstance(item, str) for item in witnesses):
            return False
        observed[name] = set(witnesses)
    if set(observed) != {item.name for item in reviewed}:
        return False
    return all(
        observed[item.name] <= {evidence.reference for evidence in item.evidence}
        for item in reviewed
    )


def _changes_match(changes: list[object], assessment: Assessment) -> bool:
    if len(changes) != len(assessment.changes):
        return False
    identities = set()
    for item, reviewed in zip(changes, assessment.changes, strict=True):
        if not isinstance(item, dict):
            return False
        actual = tuple(
            item.get(key)
            for key in (
                "path",
                "status",
                "old_mode",
                "new_mode",
                "old_object",
                "new_object",
            )
        )
        expected = (
            reviewed.path,
            reviewed.status,
            reviewed.old_mode,
            reviewed.new_mode,
            reviewed.old_object,
            reviewed.new_object,
        )
        if (
            actual != expected
            or expected in identities
            or not _boundary_reviews_match(item.get("boundaries"), reviewed.boundaries)
        ):
            return False
        identities.add(expected)
    return True


def assessment_sha256(assessment: Assessment) -> str:
    """Hash the canonical validated review claim, without authenticating it."""
    return hashlib.sha256(
        _canonical(
            {
                "format": "codex-session-gc-impact-assessment-v1",
                "assessment": asdict(assessment),
            }
        )
    ).hexdigest()


def _decision(impact: Impact, report_sha: str, reason: str, assessment: Assessment) -> Decision:
    return Decision(
        impact,
        report_sha,
        (reason,),
        assessment_sha256(assessment),
        assessment.scope,
        assessment.review,
    )


def _impact(dispositions: set[ChangeDisposition]) -> tuple[Impact, str]:
    if ChangeDisposition.UNKNOWN in dispositions:
        return Impact.UNKNOWN, "changed_object_unresolved"
    if ChangeDisposition.EXECUTION_CHANGE in dispositions:
        return Impact.REQUIRES_EXECUTION_CHANGE, "supplied_review_claims_execution_change"
    if ChangeDisposition.REVIEW_REQUIRED in dispositions:
        return Impact.REVIEW_REQUIRED, "changed_boundary_requires_review"
    if ChangeDisposition.REVIEWED_COMPATIBLE in dispositions:
        return Impact.REVIEWED_SAME_EXECUTION, "supplied_review_claims_same_execution"
    return Impact.NO_RELEVANT_DELTA, "supplied_review_claims_complete_scoped_coverage"


def evaluate(report: dict[str, object], assessment: Assessment) -> Decision:
    """Evaluate exact coverage dispositions as unverified review claims."""
    observed_sha = report_sha256(report)
    context = _report_context(report, assessment, observed_sha)
    if context is None:
        return _decision(
            Impact.UNKNOWN, observed_sha, "source_or_binding_missing_or_mismatched", assessment
        )
    old, new, changes, gaps = context
    reason = _coverage_reason(old, new, gaps, assessment)
    if reason is not None:
        return _decision(Impact.UNKNOWN, observed_sha, reason, assessment)
    if not _changes_match(changes, assessment):
        return _decision(
            Impact.UNKNOWN, observed_sha, "changed_object_dispositions_incomplete", assessment
        )
    dispositions = {item.disposition for item in assessment.changes}
    dispositions.update(
        boundary.disposition for item in assessment.changes for boundary in item.boundaries
    )
    impact, detail = _impact(dispositions)
    return _decision(impact, observed_sha, detail, assessment)
