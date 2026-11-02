# Copyright (c) 2026

"""Strict compatibility claims with separate, non-authorizing identity observations."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, NoReturn

from codex_session_gc.artifacts import SourceIdentity

MAX_RECORD_BYTES = 256 * 1024
_MAX_ITEMS = 1024
_MAX_TEXT = 2048
_VERSION = 1
_CONTROL = 32


class SupportRecordError(ValueError):
    """A compatibility claim is malformed or internally inconsistent."""


class SupportStatus(StrEnum):
    """The status claimed by a support record."""

    UNASSESSED = "unassessed"
    REVIEW_REQUIRED = "review_required"
    SUPPORTED_WITHIN_SCOPE = "supported_within_scope"
    REQUIRES_EXECUTION_CHANGE = "requires_execution_change"


class BindingState(StrEnum):
    """Observed agreement between a record field and current identity."""

    MATCH = "match"
    UNKNOWN = "unknown"
    MISMATCH = "mismatch"


def _fail(message: str) -> NoReturn:
    raise SupportRecordError(message)


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_TEXT
        or any(ord(character) < _CONTROL for character in value)
    ):
        _fail("Invalid evidence text.")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        _fail("Invalid SHA-256 identity.")
    return value


def _repository(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None
        or len(value) > _MAX_TEXT
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        _fail("Invalid repository identity.")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("Duplicate record key.")
        result[key] = value
    return result


def _fields(value: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("Unexpected record fields.")
    return value


@dataclass(frozen=True, slots=True)
class Evidence:
    """A stated evidence source and the scope of its claim."""

    source: str
    scope: str

    def __post_init__(self) -> None:
        """Validate the evidence description."""
        _text(self.source)
        _text(self.scope)


@dataclass(frozen=True, slots=True)
class Witness:
    """A named witness and the boundary it addresses."""

    reference: str
    scope: str

    def __post_init__(self) -> None:
        """Validate the witness reference and scope."""
        _text(self.reference)
        _text(self.scope)


@dataclass(frozen=True, slots=True)
class ApplicabilityCondition:
    """A condition that defines where the support claim applies."""

    description: str

    def __post_init__(self) -> None:
        """Validate the condition description."""
        _text(self.description)


@dataclass(frozen=True, slots=True)
class CoverageGap:
    """An unresolved limit in the evidence supporting a claim."""

    description: str

    def __post_init__(self) -> None:
        """Validate the unresolved gap description."""
        _text(self.description)


@dataclass(frozen=True, slots=True)
class ProducerIdentity:
    """The producer source and, when observed, its binary bytes."""

    repository: str
    source: SourceIdentity
    observed_binary_sha256: str | None

    def __post_init__(self) -> None:
        """Reject ambiguous repositories and invalid nested identities."""
        _repository(self.repository)
        if not isinstance(self.source, SourceIdentity):
            _fail("Invalid producer identity.")
        if self.observed_binary_sha256 is not None:
            _digest(self.observed_binary_sha256)


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Exact identities of the helper, profile and native input manifest."""

    helper_sha256: str
    profile_sha256: str
    native_inputs_sha256: str

    def __post_init__(self) -> None:
        """Reject incomplete or malformed execution identities."""
        _digest(self.helper_sha256)
        _digest(self.profile_sha256)
        _digest(self.native_inputs_sha256)


@dataclass(frozen=True, slots=True)
class SupportRecord:
    """An external support claim; construction does not establish authority."""

    record_version: int
    target: ProducerIdentity
    execution: ExecutionIdentity
    report_sha256: str
    status: SupportStatus
    witnesses: tuple[Witness, ...]
    conditions: tuple[ApplicabilityCondition, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    coverage_complete: bool
    source_origin: Evidence
    review: Evidence | None

    def __post_init__(self) -> None:
        """Validate claim shape and the declared supported-scope requirements."""
        if type(self.record_version) is not int or self.record_version != _VERSION:
            _fail("Unsupported record version.")
        if (
            not isinstance(self.target, ProducerIdentity)
            or not isinstance(self.execution, ExecutionIdentity)
            or not isinstance(self.status, SupportStatus)
            or not isinstance(self.source_origin, Evidence)
            or (self.review is not None and not isinstance(self.review, Evidence))
            or type(self.coverage_complete) is not bool
        ):
            _fail("Invalid record identity or status.")
        _digest(self.report_sha256)
        if (
            not isinstance(self.witnesses, tuple)
            or len(self.witnesses) > _MAX_ITEMS
            or any(not isinstance(item, Witness) for item in self.witnesses)
            or len(set(self.witnesses)) != len(self.witnesses)
            or not isinstance(self.conditions, tuple)
            or len(self.conditions) > _MAX_ITEMS
            or any(not isinstance(item, ApplicabilityCondition) for item in self.conditions)
            or len(set(self.conditions)) != len(self.conditions)
            or not isinstance(self.coverage_gaps, tuple)
            or len(self.coverage_gaps) > _MAX_ITEMS
            or any(not isinstance(item, CoverageGap) for item in self.coverage_gaps)
            or len(set(self.coverage_gaps)) != len(self.coverage_gaps)
        ):
            _fail("Invalid record evidence collection.")
        if self.status is SupportStatus.SUPPORTED_WITHIN_SCOPE and (
            not self.coverage_complete
            or self.coverage_gaps
            or not self.witnesses
            or self.review is None
        ):
            _fail("Supported scope requires complete declared coverage and reviewed witnesses.")


def _source(value: object) -> SourceIdentity:
    data = _fields(value, {"commit", "ref"})
    try:
        return SourceIdentity(_text(data["commit"]), _text(data["ref"]))
    except ValueError as error:
        message = "Invalid producer source."
        raise SupportRecordError(message) from error


def _evidence(value: object) -> Evidence:
    data = _fields(value, {"source", "scope"})
    return Evidence(_text(data["source"]), _text(data["scope"]))


def _witnesses(value: object) -> tuple[Witness, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        _fail("Invalid witnesses.")
    result = []
    for item in value:
        data = _fields(item, {"reference", "scope"})
        result.append(Witness(_text(data["reference"]), _text(data["scope"])))
    return tuple(result)


def _conditions(value: object) -> tuple[ApplicabilityCondition, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        _fail("Invalid applicability conditions.")
    return tuple(
        ApplicabilityCondition(_text(_fields(item, {"description"})["description"]))
        for item in value
    )


def _coverage_gaps(value: object) -> tuple[CoverageGap, ...]:
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        _fail("Invalid coverage gaps.")
    return tuple(
        CoverageGap(_text(_fields(item, {"description"})["description"])) for item in value
    )


def parse_record(raw: bytes) -> SupportRecord:
    """Parse a bounded JSON record as an unverified external claim."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_RECORD_BYTES:
        _fail("Record size is outside the allowed bound.")
    try:
        data: object = json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        message = "Invalid support record JSON."
        raise SupportRecordError(message) from error
    value = _fields(
        data,
        {
            "record_version",
            "target",
            "execution",
            "report_sha256",
            "status",
            "witnesses",
            "conditions",
            "coverage_gaps",
            "coverage_complete",
            "source_origin",
            "review",
        },
    )
    target = _fields(value["target"], {"repository", "source", "observed_binary_sha256"})
    execution = _fields(
        value["execution"], {"helper_sha256", "profile_sha256", "native_inputs_sha256"}
    )
    raw_status = value["status"]
    if not isinstance(raw_status, str):
        _fail("Invalid support status.")
    try:
        status = SupportStatus(raw_status)
    except (ValueError, TypeError) as error:
        message = "Invalid support status."
        raise SupportRecordError(message) from error
    version = value["record_version"]
    coverage = value["coverage_complete"]
    if type(version) is not int or type(coverage) is not bool:
        _fail("Invalid record version or coverage.")
    review = value["review"]
    return SupportRecord(
        version,
        ProducerIdentity(
            _text(target["repository"]),
            _source(target["source"]),
            None
            if target["observed_binary_sha256"] is None
            else _digest(target["observed_binary_sha256"]),
        ),
        ExecutionIdentity(
            _digest(execution["helper_sha256"]),
            _digest(execution["profile_sha256"]),
            _digest(execution["native_inputs_sha256"]),
        ),
        _digest(value["report_sha256"]),
        status,
        _witnesses(value["witnesses"]),
        _conditions(value["conditions"]),
        _coverage_gaps(value["coverage_gaps"]),
        coverage,
        _evidence(value["source_origin"]),
        None if review is None else _evidence(review),
    )


@dataclass(frozen=True, slots=True)
class ProducerObservation:
    """The producer identity fields currently observed by the caller."""

    repository: str | None = None
    commit: str | None = None
    ref: str | None = None
    binary_sha256: str | None = None

    def __post_init__(self) -> None:
        """Validate every supplied observation without inventing missing fields."""
        if self.repository is not None:
            _repository(self.repository)
        if self.commit is not None and (
            not isinstance(self.commit, str) or re.fullmatch(r"[0-9a-f]{40}", self.commit) is None
        ):
            _fail("Invalid observed commit.")
        if self.ref is not None:
            if not isinstance(self.ref, str):
                _fail("Invalid observed ref.")
            try:
                SourceIdentity("0" * 40, self.ref)
            except ValueError as error:
                message = "Invalid observed ref."
                raise SupportRecordError(message) from error
        if self.binary_sha256 is not None:
            _digest(self.binary_sha256)


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """The execution identity fields currently observed by the caller."""

    helper_sha256: str | None = None
    profile_sha256: str | None = None
    native_inputs_sha256: str | None = None

    def __post_init__(self) -> None:
        """Validate supplied execution identities."""
        for value in (self.helper_sha256, self.profile_sha256, self.native_inputs_sha256):
            if value is not None:
                _digest(value)


@dataclass(frozen=True, slots=True)
class BindingCheck:
    """One observed identity comparison."""

    field: str
    state: BindingState


@dataclass(frozen=True, slots=True)
class BindingObservation:
    """Binding results and a claim, without qualification or authorization."""

    state: BindingState
    checks: tuple[BindingCheck, ...]
    claimed_status: SupportStatus

    @property
    def claim_verified(self) -> Literal[False]:
        """Parsing and binding do not authenticate the record's claim."""
        return False

    @property
    def authorizes_operation(self) -> Literal[False]:
        """A compatibility record does not grant cleanup authority."""
        return False


def _compare(expected: object, observed: object) -> BindingState:
    if expected is None or observed is None:
        return BindingState.UNKNOWN
    return BindingState.MATCH if expected == observed else BindingState.MISMATCH


def observe_bindings(
    record: SupportRecord,
    *,
    target: ProducerObservation | None,
    execution: ExecutionObservation | None,
    report_sha256: str | None,
) -> BindingObservation:
    """Compare independently supplied identities; never authorize an operation."""
    if not isinstance(record, SupportRecord):
        _fail("Invalid support record.")
    if target is not None and not isinstance(target, ProducerObservation):
        _fail("Invalid observed producer identity.")
    if execution is not None and not isinstance(execution, ExecutionObservation):
        _fail("Invalid observed execution identity.")
    if report_sha256 is not None:
        _digest(report_sha256)
    checks = (
        BindingCheck(
            "repository",
            _compare(record.target.repository, None if target is None else target.repository),
        ),
        BindingCheck(
            "source_commit",
            _compare(record.target.source.commit, None if target is None else target.commit),
        ),
        BindingCheck(
            "source_ref", _compare(record.target.source.ref, None if target is None else target.ref)
        ),
        BindingCheck(
            "producer_binary",
            _compare(
                record.target.observed_binary_sha256,
                None if target is None else target.binary_sha256,
            ),
        ),
        BindingCheck(
            "helper",
            _compare(
                record.execution.helper_sha256,
                None if execution is None else execution.helper_sha256,
            ),
        ),
        BindingCheck(
            "profile",
            _compare(
                record.execution.profile_sha256,
                None if execution is None else execution.profile_sha256,
            ),
        ),
        BindingCheck(
            "native_inputs",
            _compare(
                record.execution.native_inputs_sha256,
                None if execution is None else execution.native_inputs_sha256,
            ),
        ),
        BindingCheck("report", _compare(record.report_sha256, report_sha256)),
    )
    states = {check.state for check in checks}
    overall = (
        BindingState.MISMATCH
        if BindingState.MISMATCH in states
        else (BindingState.UNKNOWN if BindingState.UNKNOWN in states else BindingState.MATCH)
    )
    return BindingObservation(overall, checks, record.status)
