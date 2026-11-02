# Copyright (c) 2026

"""Observe a bundled support claim independently of local execution readiness."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from uuid import UUID

from codex_session_gc.compatibility.record import (
    MAX_RECORD_BYTES,
    BindingObservation,
    ExecutionObservation,
    ProducerObservation,
    SupportRecord,
    SupportRecordError,
    observe_bindings,
    parse_record,
)
from codex_session_gc.installation.model import InstallLayout

_RECORD_READ = MAX_RECORD_BYTES + 1
_PROFILE_READ = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BundledSupport:
    """Explicitly select the running application's bundled support record."""


@dataclass(frozen=True, slots=True)
class ImportedSupport:
    """Select exact stored support bytes within one operator-owned installation."""

    layout: InstallLayout
    installation_id: UUID
    record_sha256: str
    context_sha256: str

    def __post_init__(self) -> None:
        """Reject ambiguous or malformed content and verification identities."""
        if (
            type(self.layout) is not InstallLayout
            or type(self.installation_id) is not UUID
            or any(
                not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in (self.record_sha256, self.context_sha256)
            )
        ):
            message = "Imported support requires an installation and two exact SHA-256 identities."
            raise ValueError(message)


type SupportSelection = BundledSupport | ImportedSupport


class RecordOrigin(StrEnum):
    """How the support bytes were supplied to the observer."""

    BUNDLED = "bundled_static_app_data"
    CALLER = "caller_supplied_unverified"


class RecordAvailability(StrEnum):
    """Whether the bounded support record could be parsed."""

    AVAILABLE = "available"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceSupportObservation:
    """A support claim, origin and independent binding observations."""

    origin: RecordOrigin
    availability: RecordAvailability
    detail: str
    record_sha256: str | None
    record: SupportRecord | None
    bindings: BindingObservation | None
    producer_observation: ProducerObservation | None
    producer_origin: str
    profile_declared_native_inputs_sha256: str | None
    profile_native_inputs_origin: str
    claim_verified: bool = False
    authorizes_operation: bool = False
    native_inputs_independently_observed: bool = False

    def __post_init__(self) -> None:
        """Reject authority claims in a read-only support observation."""
        if (
            self.claim_verified is not False
            or self.authorizes_operation is not False
            or self.native_inputs_independently_observed is not False
        ):
            message = "A support observation cannot establish authority."
            raise ValueError(message)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            message = "Duplicate profile field."
            raise ValueError(message)
        result[key] = value
    return result


def _profile_evidence(raw: bytes | None) -> bytes | None:
    if raw is None or not raw or len(raw) > _PROFILE_READ:
        return None
    profile: object = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(profile, dict):
        return None
    evidence = profile.get("qualification_evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("data_base64"), str):
        return None
    decoded = base64.b64decode(evidence["data_base64"], validate=True)
    return decoded if decoded and len(decoded) <= _PROFILE_READ else None


def _profile_native_inputs(raw: bytes | None) -> str | None:
    try:
        decoded = _profile_evidence(raw)
        if decoded is None:
            return None
        qualification: object = json.loads(decoded, object_pairs_hook=_unique)
        if not isinstance(qualification, dict):
            return None
        digest = qualification.get("compiled_source_manifest_sha256")
        return digest if isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) else None
    except ValueError, UnicodeDecodeError, binascii.Error, RecursionError:
        return None


def observe_record(
    raw: bytes,
    *,
    profile_raw: bytes | None,
    helper_sha256: str | None,
    producer: ProducerObservation | None = None,
) -> SourceSupportObservation:
    """Compare caller-supplied claim bytes without verifying their origin."""
    return _observe_parsed(
        parse_record(raw),
        raw,
        RecordOrigin.CALLER,
        _Inputs(profile_raw, helper_sha256, producer),
    )


def observe_unavailable_record(
    selection: ImportedSupport,
    *,
    profile_raw: bytes | None,
    producer: ProducerObservation | None = None,
) -> SourceSupportObservation:
    """Keep an unavailable explicit selection distinct from the bundled record."""
    declared = _profile_native_inputs(profile_raw)
    return SourceSupportObservation(
        origin=RecordOrigin.CALLER,
        availability=RecordAvailability.UNKNOWN,
        detail="The selected support record or its saved verification context is unavailable.",
        record_sha256=selection.record_sha256,
        record=None,
        bindings=None,
        producer_observation=producer,
        producer_origin="not_observed" if producer is None else "caller_supplied_unverified",
        profile_declared_native_inputs_sha256=declared,
        profile_native_inputs_origin=(
            "unavailable" if declared is None else "profile_declaration_not_independent_observation"
        ),
    )


@dataclass(frozen=True, slots=True)
class _Inputs:
    profile_raw: bytes | None
    helper_sha256: str | None
    producer: ProducerObservation | None


def _observe_parsed(
    record: SupportRecord,
    raw: bytes,
    origin: RecordOrigin,
    inputs: _Inputs,
) -> SourceSupportObservation:
    profile_sha = (
        hashlib.sha256(inputs.profile_raw).hexdigest() if inputs.profile_raw is not None else None
    )
    binding = observe_bindings(
        record,
        target=inputs.producer,
        execution=ExecutionObservation(inputs.helper_sha256, profile_sha, None),
        report_sha256=None,
    )
    declared = _profile_native_inputs(inputs.profile_raw)
    return SourceSupportObservation(
        origin=origin,
        availability=RecordAvailability.AVAILABLE,
        detail=(
            "The support record is a scoped claim; "
            "its review and distribution are not verified here."
        ),
        record_sha256=hashlib.sha256(raw).hexdigest(),
        record=record,
        bindings=binding,
        producer_observation=inputs.producer,
        producer_origin=(
            "not_observed" if inputs.producer is None else "caller_supplied_unverified"
        ),
        profile_declared_native_inputs_sha256=declared,
        profile_native_inputs_origin=(
            "unavailable" if declared is None else "profile_declaration_not_independent_observation"
        ),
    )


def observe_bundled_support(
    *,
    profile_raw: bytes | None,
    helper_sha256: str | None,
    producer: ProducerObservation | None = None,
) -> SourceSupportObservation:
    """Read the app's static record; absence or malformed data stays unknown."""
    try:
        resource = files("codex_session_gc.compatibility").joinpath("support.json")
        with resource.open("rb") as stream:
            raw = stream.read(_RECORD_READ)
        record = parse_record(raw)
    except OSError, SupportRecordError:
        declared = _profile_native_inputs(profile_raw)
        return SourceSupportObservation(
            origin=RecordOrigin.BUNDLED,
            availability=RecordAvailability.UNKNOWN,
            detail="The bundled support record is unavailable or malformed.",
            record_sha256=None,
            record=None,
            bindings=None,
            producer_observation=producer,
            producer_origin="not_observed" if producer is None else "caller_supplied_unverified",
            profile_declared_native_inputs_sha256=declared,
            profile_native_inputs_origin=(
                "unavailable"
                if declared is None
                else "profile_declaration_not_independent_observation"
            ),
        )
    return _observe_parsed(
        record, raw, RecordOrigin.BUNDLED, _Inputs(profile_raw, helper_sha256, producer)
    )
