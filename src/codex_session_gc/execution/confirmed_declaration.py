# Copyright (c) 2026

"""Immutable confirmed-operation declarations shared by dispatch and observation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator

from codex_session_gc.domain import GuardianAssociation, UtcCutoff
from codex_session_gc.execution.closed_work_request import DatabaseKind, QualifiedHelperContract
from codex_session_gc.execution.physical_manifest import WireTarget, decode_targets
from codex_session_gc.execution.selection_provenance import (
    GuardianBinding,
    association_bindings,
    reviewed_associations,
)


class Declaration(BaseModel):
    """Exact immutable inputs returned before an explicit destructive tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    home_identity: tuple[StrictInt, StrictInt]
    roots: tuple[str, ...]
    selected: tuple[str, ...]
    cutoff: str
    profile_sha256: str
    schema_version: StrictInt
    guardian_associations: tuple[GuardianBinding, ...]
    physical_manifest: tuple[WireTarget, ...]
    digest: str

    @field_validator("operation_id")
    @classmethod
    def canonical_operation(cls, value: str) -> str:
        """Accept only the canonical representation used by receipt filenames."""
        if str(UUID(value)) != value:
            message = "Operation identity must be a canonical UUID."
            raise ValueError(message)
        return value

    @field_validator("roots", "selected")
    @classmethod
    def canonical_selection(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Preserve exact sorted identities without duplicate or coerced membership."""
        if tuple(sorted({str(UUID(item)) for item in value})) != value:
            message = "Selected identities must be unique canonical UUIDs in sorted order."
            raise ValueError(message)
        return value

    @field_validator("guardian_associations")
    @classmethod
    def canonical_associations(
        cls, value: tuple[GuardianBinding, ...]
    ) -> tuple[GuardianBinding, ...]:
        """Bind exact historical relations without acquiring their parent threads."""
        reviewed_associations(value)
        return value

    @field_validator("physical_manifest")
    @classmethod
    def exact_manifest(cls, value: tuple[WireTarget, ...]) -> tuple[WireTarget, ...]:
        """Validate one exact physical bound shared by preview, dispatch and observation."""
        decode_targets(value)
        return value

    @field_validator("cutoff")
    @classmethod
    def canonical_cutoff(cls, value: str) -> str:
        """Keep the UTC instant identical across preparation, dispatch and observation."""
        if UtcCutoff(datetime.fromisoformat(value)).timestamp.isoformat() != value:
            message = "The confirmed cutoff must be a canonical UTC instant."
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def exact_selected_manifest(self) -> Declaration:
        """Every reviewed logical candidate has exactly its declared physical target."""
        if tuple(target.thread_id for target in self.physical_manifest) != self.selected:
            message = "The physical manifest must match the exact candidate IDs."
            raise ValueError(message)
        return self

    def content(self) -> dict[str, Any]:
        """Return JSON-compatible binding data, excluding its derived digest."""
        return self.model_dump(mode="json", exclude={"digest"})

    def calculated_digest(self) -> str:
        """Bind payload bytes; this is never evidence of human consent."""
        return hashlib.sha256(
            json.dumps(self.content(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def schema_version(helper: QualifiedHelperContract) -> int:
    """Identify the exact declared state migration version; full identity remains hashed."""
    state = next(database for database in helper.databases if database.kind is DatabaseKind.STATE)
    return max((migration[0] for migration in state.migrations), default=0)


@dataclass(frozen=True)
class ReviewedSelection:
    """Observed selection inputs before binding an operation, home and profile identity."""

    roots: tuple[str, ...]
    selected: tuple[str, ...]
    cutoff: str
    physical_manifest: tuple[WireTarget, ...]
    guardian_associations: tuple[GuardianAssociation, ...] = ()


def create_declaration(
    selection: ReviewedSelection,
    *,
    operation_id: UUID,
    home_identity: tuple[int, int],
    helper: QualifiedHelperContract,
) -> Declaration:
    """Bind supplied observed inputs without allocating an operation or touching storage."""
    declaration = Declaration(
        operation_id=str(operation_id),
        home_identity=home_identity,
        roots=selection.roots,
        selected=selection.selected,
        cutoff=selection.cutoff,
        profile_sha256=helper.evidence.digest.hex(),
        schema_version=schema_version(helper),
        guardian_associations=association_bindings(selection.guardian_associations),
        physical_manifest=selection.physical_manifest,
        digest="",
    )
    return declaration.model_copy(update={"digest": declaration.calculated_digest()})


def validate_current_declaration(
    declaration: Declaration, home_identity: tuple[int, int], helper: QualifiedHelperContract
) -> None:
    """Require the reviewed home and profile without refreshing any bound input."""
    if (
        declaration.home_identity != home_identity
        or declaration.profile_sha256 != helper.evidence.digest.hex()
        or declaration.schema_version != schema_version(helper)
    ):
        message = "The startup home or qualified profile identity has changed."
        raise ValueError(message)
    saved_declaration(declaration.model_dump(mode="json"), UUID(declaration.operation_id), helper)


def saved_declaration(
    value: object, attempt_id: UUID, helper: QualifiedHelperContract
) -> Declaration:
    """Validate the exact saved binding without granting or refreshing deletion authority."""
    declaration = Declaration.model_validate(value)
    if (
        str(UUID(declaration.operation_id)) != declaration.operation_id
        or UUID(declaration.operation_id) != attempt_id
        or declaration.digest != declaration.calculated_digest()
        or declaration.profile_sha256 != helper.evidence.digest.hex()
        or declaration.schema_version != schema_version(helper)
    ):
        message = "The saved confirmed-operation binding differs from its attempt or profile."
        raise ValueError(message)
    return declaration
