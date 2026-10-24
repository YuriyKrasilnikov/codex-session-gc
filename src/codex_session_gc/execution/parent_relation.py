# Copyright (c) 2026

"""Tagged root, spawn and Guardian relations for native target declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from codex_session_gc.domain import ThreadId

if TYPE_CHECKING:
    from codex_session_gc.inventory.observation import RolloutObservation


@dataclass(frozen=True, slots=True)
class RootParent:
    """An ordinary root has no spawn or Guardian parent relation."""


@dataclass(frozen=True, slots=True)
class SpawnParent:
    """Actual spawn ownership represented independently of Guardian association."""

    parent_thread_id: ThreadId

    def __post_init__(self) -> None:
        """Require a validated semantic identity."""
        _typed_parent(self.parent_thread_id)


@dataclass(frozen=True, slots=True)
class GuardianParent:
    """A historical parent association imposes no ownership or closure requirement."""

    associated_parent_thread_id: ThreadId

    def __post_init__(self) -> None:
        """Require a validated semantic identity."""
        _typed_parent(self.associated_parent_thread_id)


type ParentRelation = RootParent | SpawnParent | GuardianParent


def _typed_parent(value: ThreadId) -> None:
    if type(value) is not ThreadId:
        message = "A parent relation requires a typed thread identity."
        raise TypeError(message)


def parent_relation(observation: RolloutObservation) -> ParentRelation:
    """Project selected facts without conflating two independent parent meanings."""
    if observation.metadata_guardian_parent is not None:
        if (
            observation.metadata_spawn_parent is not None
            or observation.metadata_origin != "guardian"
        ):
            message = "Guardian association cannot coexist with spawn ownership."
            raise ValueError(message)
        return GuardianParent(observation.metadata_guardian_parent)
    if observation.metadata_origin == "guardian":
        message = "Selected Guardian provenance requires a known historical association."
        raise ValueError(message)
    if observation.metadata_spawn_parent is not None:
        return SpawnParent(observation.metadata_spawn_parent)
    return RootParent()


def relation_payload(relation: ParentRelation) -> dict[str, object]:
    """Produce the exact exhaustive tagged wire representation."""
    if isinstance(relation, GuardianParent):
        return {
            "kind": "guardian_association",
            "associated_parent_thread_id": str(relation.associated_parent_thread_id.value),
        }
    if isinstance(relation, SpawnParent):
        return {"kind": "spawn", "parent_thread_id": str(relation.parent_thread_id.value)}
    return {"kind": "root"}


def parse_parent_relation(value: object) -> ParentRelation:
    """Reject mixed, missing or unknown relation fields; there is no old-wire decoder."""
    if isinstance(value, dict):
        if value == {"kind": "root"}:
            return RootParent()
        if set(value) == {"kind", "parent_thread_id"} and value["kind"] == "spawn":
            return SpawnParent(_identity(value["parent_thread_id"]))
        if (
            set(value) == {"kind", "associated_parent_thread_id"}
            and value["kind"] == "guardian_association"
        ):
            return GuardianParent(_identity(value["associated_parent_thread_id"]))
    message = "The target requires exactly one supported tagged parent relation."
    raise ValueError(message)


def _identity(value: object) -> ThreadId:
    identifier = ThreadId.parse(value)
    if str(identifier.value) != value:
        message = "A wire parent identity must be a canonical UUID."
        raise ValueError(message)
    return identifier


@dataclass(frozen=True, slots=True)
class _SourceProvenance:
    guardian: bool = False
    embedded_parent: ThreadId | None = None


def _source_provenance(source: object) -> _SourceProvenance:
    """Decode supported native source variants independently of spawn ownership."""
    if source in ("cli", "vscode", "exec", "mcp"):
        return _SourceProvenance()
    if isinstance(source, dict) and len(source) == 1:
        if isinstance(source.get("custom"), str) or source.get("internal") in (
            "memory_consolidation",
            "guardian",
        ):
            return _SourceProvenance()
        subagent = source.get("subagent")
        if subagent in ("review", "compact", "memory_consolidation"):
            return _SourceProvenance()
        if isinstance(subagent, dict) and len(subagent) == 1:
            other = subagent.get("other")
            if isinstance(other, str):
                return _SourceProvenance(guardian=other == "guardian")
            spawn = subagent.get("thread_spawn")
            if isinstance(spawn, dict):
                return _SourceProvenance(embedded_parent=_identity(spawn.get("parent_thread_id")))
    message = "The target source is not a supported native source variant."
    raise ValueError(message)


def validate_relation_source(relation: ParentRelation, source: object) -> None:
    """Validate source provenance without inferring ownership from an optional parent."""
    provenance = _source_provenance(source)
    if isinstance(relation, GuardianParent):
        valid = provenance.guardian
    elif isinstance(relation, SpawnParent):
        valid = not provenance.guardian and provenance.embedded_parent in (
            None,
            relation.parent_thread_id,
        )
    else:
        valid = not provenance.guardian and provenance.embedded_parent is None
    if not valid:
        message = "The target source disagrees with its tagged parent relation."
        raise ValueError(message)
