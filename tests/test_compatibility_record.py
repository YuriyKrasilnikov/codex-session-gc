# Copyright (c) 2026

"""External support records remain claims until identities are observed."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from typing import TYPE_CHECKING, cast

import pytest

from codex_session_gc.compatibility.record import (
    MAX_RECORD_BYTES,
    BindingState,
    ExecutionObservation,
    ProducerObservation,
    SupportRecordError,
    SupportStatus,
    observe_bindings,
    parse_record,
)

if TYPE_CHECKING:
    from pathlib import Path

_COMMIT = "a" * 40
_REF = "refs/tags/v1.2.3"
_BINARY = "b" * 64
_HELPER = "c" * 64
_PROFILE = "d" * 64
_INPUTS = "e" * 64
_REPORT = "f" * 64


def _record() -> dict[str, object]:
    return {
        "record_version": 1,
        "target": {
            "repository": "example/project",
            "source": {"commit": _COMMIT, "ref": _REF},
            "observed_binary_sha256": _BINARY,
        },
        "execution": {
            "helper_sha256": _HELPER,
            "profile_sha256": _PROFILE,
            "native_inputs_sha256": _INPUTS,
        },
        "report_sha256": _REPORT,
        "status": "supported_within_scope",
        "coverage_complete": True,
        "witnesses": [{"reference": "evidence/storage", "scope": "bounded storage"}],
        "conditions": [{"description": "Related operations must be closed."}],
        "coverage_gaps": [],
        "source_origin": {"source": "source/input", "scope": "producer source"},
        "review": {"source": "review/record", "scope": "bounded storage"},
    }


def _parse(value: object) -> object:
    return parse_record(json.dumps(value).encode())


def test_exact_bindings_never_authenticate_or_authorize(tmp_path: Path) -> None:
    """Even fully matching identities preserve the external claim boundary."""
    profile = tmp_path / "profile.json"
    profile.write_bytes(b'{"raw":"profile"}\n')
    before = hashlib.sha256(profile.read_bytes()).hexdigest()
    record = parse_record(json.dumps(_record()).encode())
    result = observe_bindings(
        record,
        target=ProducerObservation("example/project", _COMMIT, _REF, _BINARY),
        execution=ExecutionObservation(_HELPER, _PROFILE, _INPUTS),
        report_sha256=_REPORT,
    )
    if (
        result.state is not BindingState.MATCH
        or result.claimed_status is not SupportStatus.SUPPORTED_WITHIN_SCOPE
        or result.claim_verified
        or result.authorizes_operation
        or hashlib.sha256(profile.read_bytes()).hexdigest() != before
    ):
        raise AssertionError


@pytest.mark.parametrize(
    "field", ["repository", "commit", "ref", "binary", "helper", "profile", "inputs", "report"]
)
def test_each_binding_mismatch_is_visible(field: str) -> None:
    """Every producer, execution and report identity has an independent check."""
    record = parse_record(json.dumps(_record()).encode())
    target = ProducerObservation("example/project", _COMMIT, _REF, _BINARY)
    execution = ExecutionObservation(_HELPER, _PROFILE, _INPUTS)
    report = _REPORT
    if field == "repository":
        target = replace(target, repository="other/project")
    elif field == "commit":
        target = replace(target, commit="1" * 40)
    elif field == "ref":
        target = replace(target, ref="refs/tags/v2")
    elif field == "binary":
        target = replace(target, binary_sha256="1" * 64)
    elif field == "helper":
        execution = replace(execution, helper_sha256="1" * 64)
    elif field == "profile":
        execution = replace(execution, profile_sha256="1" * 64)
    elif field == "inputs":
        execution = replace(execution, native_inputs_sha256="1" * 64)
    else:
        report = "1" * 64
    result = observe_bindings(record, target=target, execution=execution, report_sha256=report)
    if result.state is not BindingState.MISMATCH or result.claim_verified:
        raise AssertionError


def test_unknown_fields_and_mismatch_precedence() -> None:
    """Missing observed values stay unknown and cannot hide a known mismatch."""
    record = parse_record(json.dumps(_record()).encode())
    result = observe_bindings(
        record,
        target=ProducerObservation(repository="example/project", commit=_COMMIT),
        execution=ExecutionObservation(helper_sha256=_HELPER),
        report_sha256=None,
    )
    if result.state is not BindingState.UNKNOWN:
        raise AssertionError
    if {item.field for item in result.checks if item.state is BindingState.UNKNOWN} != {
        "source_ref",
        "producer_binary",
        "profile",
        "native_inputs",
        "report",
    }:
        raise AssertionError
    mismatch = observe_bindings(
        record,
        target=ProducerObservation(repository="other/project"),
        execution=None,
        report_sha256=None,
    )
    if mismatch.state is not BindingState.MISMATCH:
        raise AssertionError


def test_unobserved_binary_never_inherits_a_match() -> None:
    """A record without a producer binary digest cannot claim a byte binding."""
    data = _record()
    target = cast("dict[str, object]", data["target"])
    target["observed_binary_sha256"] = None
    record = parse_record(json.dumps(data).encode())
    result = observe_bindings(
        record,
        target=ProducerObservation("example/project", _COMMIT, _REF, _BINARY),
        execution=ExecutionObservation(_HELPER, _PROFILE, _INPUTS),
        report_sha256=_REPORT,
    )
    if result.state is not BindingState.UNKNOWN:
        raise AssertionError


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "status",
        "coverage",
        "coverage_gaps",
        "witnesses",
        "review",
        "origin",
        "unknown_field",
        "nested_unknown",
    ],
)
def test_invalid_claims_refuse(mutation: str) -> None:
    """Unsupported versions, inconsistent support and undeclared fields are refused."""
    data = json.loads(json.dumps(_record()))
    if mutation == "version":
        data["record_version"] = True
    elif mutation == "status":
        data["status"] = "fully_supported"
    elif mutation == "coverage":
        data["coverage_complete"] = False
    elif mutation == "coverage_gaps":
        data["coverage_gaps"] = [{"description": "unresolved caller coverage"}]
    elif mutation == "witnesses":
        data["witnesses"] = []
    elif mutation == "review":
        data["review"] = None
    elif mutation == "origin":
        data["source_origin"]["scope"] = ""
    elif mutation == "unknown_field":
        data["permission"] = True
    else:
        data["target"]["unexpected"] = "value"
    with pytest.raises(SupportRecordError, match=r"."):
        _parse(data)


def test_duplicate_keys_and_oversized_records_refuse() -> None:
    """Ambiguous JSON and unbounded input never produce a claim."""
    with pytest.raises(SupportRecordError, match=r"Duplicate|Invalid"):
        parse_record(b'{"status":"unassessed","status":"review_required"}')
    with pytest.raises(SupportRecordError, match="size"):
        parse_record(b" " * (MAX_RECORD_BYTES + 1))


def test_record_evidence_is_immutable() -> None:
    """Evidence and witness collections cannot be changed after validation."""
    record = parse_record(json.dumps(_record()).encode())
    status_field = "status"
    scope_field = "scope"
    with pytest.raises(FrozenInstanceError):
        setattr(record, status_field, SupportStatus.UNASSESSED)
    with pytest.raises(FrozenInstanceError):
        setattr(record.witnesses[0], scope_field, "changed")


def test_supported_scope_preserves_applicability_condition() -> None:
    """A supported claim can require closed related operations without a coverage gap."""
    record = parse_record(json.dumps(_record()).encode())
    if (
        record.status is not SupportStatus.SUPPORTED_WITHIN_SCOPE
        or len(record.conditions) != 1
        or record.conditions[0].description != "Related operations must be closed."
        or record.coverage_gaps
    ):
        raise AssertionError


def test_unresolved_coverage_is_distinct_and_immutable() -> None:
    """A review-required record retains unresolved gaps separately from conditions."""
    data = _record()
    data["status"] = "review_required"
    data["coverage_complete"] = False
    data["coverage_gaps"] = [{"description": "Caller coverage is unresolved."}]
    record = parse_record(json.dumps(data).encode())
    if not record.conditions or not record.coverage_gaps:
        raise AssertionError
    field = "description"
    with pytest.raises(FrozenInstanceError):
        setattr(record.conditions[0], field, "changed")
    with pytest.raises(FrozenInstanceError):
        setattr(record.coverage_gaps[0], field, "changed")
