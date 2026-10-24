# Copyright (c) 2026

"""One exhaustive wire-6 physical manifest codec for review, dispatch and observation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, model_validator

from codex_session_gc.domain import (
    CurrentRolloutPointer,
    FileStatIdentity,
    HistoryMode,
    HistoryPrefix,
    InventoryCompleteness,
    LogicalForkBoundary,
    OwnershipEdge,
    PhysicalIdentity,
    PhysicalInventory,
    PhysicalRepresentation,
    RolloutId,
    RolloutMetadata,
    SelectedPhysicalManifest,
    ThreadId,
)
from codex_session_gc.execution.parent_relation import (
    parse_parent_relation,
    validate_relation_source,
)


class _Value(BaseModel):
    """Frozen exhaustive transport value."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class WireStat(_Value):
    """Native-compatible descriptor stat fields."""

    dev: StrictInt
    ino: StrictInt
    size: StrictInt
    mtime_ns: StrictInt
    ctime_ns: StrictInt

    def value(self) -> FileStatIdentity:
        """Validate the source integer ranges independently of Pydantic coercion."""
        return FileStatIdentity(self.dev, self.ino, self.size, self.mtime_ns, self.ctime_ns)


class WirePrefix(_Value):
    """Exact immutable provider and decoded prefix coordinates."""

    provider_rollout_id: str
    end_ordinal_exclusive: StrictInt
    end_byte_offset: StrictInt

    def value(self) -> HistoryPrefix:
        """Keep source u64 coordinates independent from storage extents."""
        return HistoryPrefix(
            _rollout(self.provider_rollout_id), self.end_ordinal_exclusive, self.end_byte_offset
        )


class WireFork(_Value):
    """Logical historical provenance, never a physical dependency edge."""

    forked_from_id: str
    forked_from_ordinal_exclusive: StrictInt | None

    def value(self) -> LogicalForkBoundary:
        """Do not infer a missing fork boundary from its physical history base."""
        return LogicalForkBoundary(_thread(self.forked_from_id), self.forked_from_ordinal_exclusive)


class WireRollout(_Value):
    """One exact actual physical representation with an implicit logical target owner."""

    rollout_id: str
    representation: PhysicalRepresentation
    rollout_path: str
    physical_identity: WireStat
    header_sha256: tuple[StrictInt, ...]
    history_mode: HistoryMode
    history_prefix: WirePrefix | None
    logical_fork: WireFork | None

    def value(self, owner: ThreadId) -> RolloutMetadata:
        """Validate the complete physical value without touching a filesystem."""
        return RolloutMetadata(
            ownership=OwnershipEdge(owner, _rollout(self.rollout_id)),
            history_mode=self.history_mode,
            physical=PhysicalIdentity(
                Path(self.rollout_path),
                self.representation,
                self.physical_identity.value(),
                bytes(self.header_sha256),
            ),
            history_prefix=None if self.history_prefix is None else self.history_prefix.value(),
            logical_fork=None if self.logical_fork is None else self.logical_fork.value(),
        )


class WireTarget(_Value):
    """Current logical SQL/provenance plus every admitted owned immutable rollout."""

    thread_id: str
    current_rollout_path: str
    current_rollout_id: str
    source: str | dict[str, object]
    agent_role: str | None
    provider: str
    updated_at_ms: StrictInt
    archived: StrictBool
    parent_relation: dict[str, object]
    rollouts: tuple[WireRollout, ...]

    @model_validator(mode="after")
    def physical_bound(self) -> Self:
        """Reject missing, duplicate, ambiguous or inconsistent exact physical membership."""
        owner = _thread(self.thread_id)
        items = tuple(item.value(owner) for item in self.rollouts)
        if tuple(sorted({item.rollout_id for item in self.rollouts})) != tuple(
            item.rollout_id for item in self.rollouts
        ):
            message = "Physical rollout identities must be unique and canonically sorted."
            raise ValueError(message)
        inventory = PhysicalInventory(
            items,
            (
                CurrentRolloutPointer(
                    OwnershipEdge(owner, _rollout(self.current_rollout_id)),
                    Path(self.current_rollout_path),
                ),
            ),
            InventoryCompleteness.COMPLETE,
        )
        SelectedPhysicalManifest((owner,), inventory)
        validate_relation_source(parse_parent_relation(self.parent_relation), self.source)
        if not self.provider or not -(1 << 63) <= self.updated_at_ms < 1 << 63:
            message = "Current target SQL identity is unavailable."
            raise ValueError(message)
        return self


def _thread(value: str) -> ThreadId:
    identifier = ThreadId.parse(value)
    if str(identifier.value) != value:
        message = "A manifest thread identity must be canonical."
        raise ValueError(message)
    return identifier


def _rollout(value: str) -> RolloutId:
    identifier = RolloutId.parse(value)
    if str(identifier.value) != value:
        message = "A manifest rollout identity must be canonical."
        raise ValueError(message)
    return identifier


def encode_rollout(item: RolloutMetadata) -> dict[str, object]:
    """Serialize the already-validated physical bound without a second ownership registry."""
    physical = item.physical
    if physical is None:
        message = "A selected physical representation is unknown."
        raise ValueError(message)
    prefix, fork = item.history_prefix, item.logical_fork
    return {
        "rollout_id": str(item.ownership.rollout_id.value),
        "representation": physical.representation.value,
        "rollout_path": str(physical.path),
        "physical_identity": {
            "dev": physical.stat.dev,
            "ino": physical.stat.ino,
            "size": physical.stat.stored_bytes,
            "mtime_ns": physical.stat.mtime_ns,
            "ctime_ns": physical.stat.ctime_ns,
        },
        "header_sha256": list(physical.decoded_header_sha256),
        "history_mode": item.history_mode.value,
        "history_prefix": None
        if prefix is None
        else {
            "provider_rollout_id": str(prefix.provider_rollout_id.value),
            "end_ordinal_exclusive": prefix.end_ordinal_exclusive,
            "end_byte_offset": prefix.end_byte_offset,
        },
        "logical_fork": None
        if fork is None
        else {
            "forked_from_id": str(fork.forked_from_id.value),
            "forked_from_ordinal_exclusive": fork.forked_from_ordinal_exclusive,
        },
    }


def decode_targets(value: object) -> tuple[WireTarget, ...]:
    """The shared exhaustive decoder admits only the current nested manifest shape."""
    if not isinstance(value, (list, tuple)):
        message = "A physical manifest requires a finite target list."
        raise TypeError(message)
    targets = tuple(WireTarget.model_validate(item) for item in value)
    owners = tuple(item.thread_id for item in targets)
    if tuple(sorted(set(owners))) != owners:
        message = "Logical target identities must be nonempty, unique and canonically sorted."
        raise ValueError(message)
    physicals = tuple(
        item.value(_thread(target.thread_id)) for target in targets for item in target.rollouts
    )
    PhysicalInventory(physicals, (), InventoryCompleteness.COMPLETE)
    return targets


def history_cache_keys(targets: tuple[WireTarget, ...]) -> tuple[str, ...]:
    """Include deterministic initial IDs even when their old physical files are absent."""
    return tuple(
        sorted(
            {target.thread_id for target in targets}
            | {item.rollout_id for target in targets for item in target.rollouts}
        )
    )


def manifest_bytes(value: object) -> bytes:
    """Freeze the exhaustive manifest as canonical immutable review bytes."""
    targets = decode_targets(value)
    return json.dumps(
        [target.model_dump(mode="json") for target in targets],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def validate_reviewed_manifest(value: bytes, selected: tuple[ThreadId, ...]) -> None:
    """Reject mutable, noncanonical or widened review context before storage access."""
    if type(value) is not bytes:
        message = "Reviewed physical context requires immutable canonical bytes."
        raise TypeError(message)
    targets = decode_targets(json.loads(value))
    if manifest_bytes(targets) != value or tuple(target.thread_id for target in targets) != tuple(
        str(owner.value) for owner in sorted(selected)
    ):
        message = "The reviewed physical manifest differs from its exact logical selection."
        raise ValueError(message)
