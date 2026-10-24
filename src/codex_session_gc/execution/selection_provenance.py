# Copyright (c) 2026

"""Transport projections of immutable reviewed Guardian associations."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, TypeAdapter, model_validator

from codex_session_gc.domain import GuardianAssociation, ThreadId


class GuardianBinding(BaseModel):
    """Canonical JSON projection; the domain association remains the semantic value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guardian_thread_id: str
    associated_parent_thread_id: str

    @model_validator(mode="after")
    def validate_identities(self) -> Self:
        """Reject noncanonical, self-associated or invalid historical identities."""
        association = self.association
        if (
            str(association.guardian_thread_id.value) != self.guardian_thread_id
            or str(association.associated_parent_thread_id.value)
            != self.associated_parent_thread_id
        ):
            message = "Guardian association identities must be canonical UUIDs."
            raise ValueError(message)
        return self

    @property
    def association(self) -> GuardianAssociation:
        """Recover the shared immutable semantic relation without parent lookup."""
        return GuardianAssociation(
            ThreadId.parse(self.guardian_thread_id),
            ThreadId.parse(self.associated_parent_thread_id),
        )

    @classmethod
    def from_association(cls, association: GuardianAssociation) -> Self:
        """Project a validated domain value into the public transport contract."""
        return cls(
            guardian_thread_id=str(association.guardian_thread_id.value),
            associated_parent_thread_id=str(association.associated_parent_thread_id.value),
        )


_BINDINGS = TypeAdapter(tuple[GuardianBinding, ...])


def association_bindings(
    associations: tuple[GuardianAssociation, ...],
) -> tuple[GuardianBinding, ...]:
    """Serialize associations in canonical Guardian identity order."""
    return tuple(
        GuardianBinding.from_association(item)
        for item in sorted(associations, key=lambda item: item.guardian_thread_id)
    )


def reviewed_associations(value: object) -> tuple[GuardianAssociation, ...]:
    """Decode a complete bounded review declaration without broadening membership."""
    bindings = _BINDINGS.validate_python(value)
    associations = tuple(binding.association for binding in bindings)
    identifiers = tuple(item.guardian_thread_id for item in associations)
    if tuple(sorted(set(identifiers))) != identifiers:
        message = "Guardian associations must be unique and sorted by Guardian identity."
        raise ValueError(message)
    return associations


def check_reviewed_associations(
    associations: tuple[GuardianAssociation, ...], selected: tuple[ThreadId, ...]
) -> None:
    """Keep reviewed context immutable, canonical and within the exact selected IDs."""
    if type(associations) is not tuple or any(
        type(item) is not GuardianAssociation for item in associations
    ):
        message = "Reviewed Guardian context requires immutable typed associations."
        raise TypeError(message)
    canonical = reviewed_associations(association_bindings(associations))
    if canonical != associations or any(
        item.guardian_thread_id not in selected for item in associations
    ):
        message = "Reviewed Guardian associations must be canonical and selected explicitly."
        raise ValueError(message)
