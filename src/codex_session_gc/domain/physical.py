# Copyright (c) 2026

"""Immutable physical observations and exact effect bounds, independent of readers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from codex_session_gc.domain.model import (
    InvalidRecordError,
    OwnershipEdge,
    RolloutId,
    ThreadId,
)


class PhysicalRepresentation(StrEnum):
    """Stored encoding does not change logical or immutable rollout identity."""

    PLAIN = "plain"
    ZSTD = "zstd"


class HistoryMode(StrEnum):
    """Observed history organization; unknown is never inferred as Legacy."""

    LEGACY = "legacy"
    PAGINATED = "paginated"
    UNKNOWN = "unknown"


class InventoryCompleteness(StrEnum):
    """The observer's declared namespace coverage, not a concurrency certificate."""

    COMPLETE = "complete"
    UNKNOWN = "unknown"


def _require(*, condition: bool, message: str) -> None:
    if not condition:
        raise InvalidRecordError(message)


def _integer(value: int, bits: int, *, signed: bool = False) -> None:
    lower = -(1 << (bits - 1)) if signed else 0
    upper = (1 << (bits - int(signed))) - 1
    _require(
        condition=type(value) is int and lower <= value <= upper,
        message="Integer exceeds its typed range.",
    )


def _path(value: Path) -> None:
    _require(
        condition=isinstance(value, Path) and value.is_absolute() and ".." not in value.parts,
        message="A physical path must be absolute and contain no parent traversal.",
    )


@dataclass(frozen=True, slots=True)
class HistoryPrefix:
    """Metadata coordinates of an immutable provider; no body-integrity claim."""

    provider_rollout_id: RolloutId
    end_ordinal_exclusive: int
    end_byte_offset: int

    def __post_init__(self) -> None:
        """Keep physical ordinal and decoded-byte coordinates exactly u64."""
        _require(
            condition=type(self.provider_rollout_id) is RolloutId,
            message="A prefix requires a rollout ID.",
        )
        _integer(self.end_ordinal_exclusive, 64)
        _integer(self.end_byte_offset, 64)


@dataclass(frozen=True, slots=True)
class LogicalForkBoundary:
    """Logical fork provenance survives replacement of its physical history base."""

    forked_from_id: ThreadId
    forked_from_ordinal_exclusive: int | None

    def __post_init__(self) -> None:
        """Do not infer a missing logical fork coordinate from a physical prefix."""
        _require(
            condition=type(self.forked_from_id) is ThreadId,
            message="A logical fork requires a thread ID.",
        )
        if self.forked_from_ordinal_exclusive is not None:
            _integer(self.forked_from_ordinal_exclusive, 64)


@dataclass(frozen=True, slots=True)
class FileStatIdentity:
    """Descriptor stat evidence in the native identity field ranges."""

    dev: int
    ino: int
    stored_bytes: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        """Unknown extents must stay absent observations rather than zero-sized files."""
        for value in (self.dev, self.ino, self.stored_bytes):
            _integer(value, 64)
        for value in (self.mtime_ns, self.ctime_ns):
            _integer(value, 64, signed=True)


@dataclass(frozen=True, slots=True)
class PhysicalIdentity:
    """One observed representation; holding a descriptor remains the reader's duty."""

    path: Path
    representation: PhysicalRepresentation
    stat: FileStatIdentity
    decoded_header_sha256: bytes

    def __post_init__(self) -> None:
        """Bind encoding, actual path/stat and an immutable decoded first-line digest."""
        _path(self.path)
        _require(
            condition=type(self.representation) is PhysicalRepresentation,
            message="Unknown representation.",
        )
        _require(
            condition=type(self.stat) is FileStatIdentity,
            message="A physical identity requires typed stat.",
        )
        digest_size = 32
        _require(
            condition=type(self.decoded_header_sha256) is bytes
            and len(self.decoded_header_sha256) == digest_size,
            message="A decoded header requires a SHA-256 digest.",
        )
        suffix = ".jsonl" if self.representation is PhysicalRepresentation.PLAIN else ".jsonl.zst"
        _require(
            condition=self.path.name.endswith(suffix),
            message="Representation and path suffix disagree.",
        )


def ownership_from_path(path: Path, representation: PhysicalRepresentation) -> OwnershipEdge:
    """Parse the existing canonical logical-owner/immutable-rollout basename."""
    _path(path)
    suffix = ".jsonl" if representation is PhysicalRepresentation.PLAIN else ".jsonl.zst"
    _require(condition=path.name.endswith(suffix), message="Representation suffix disagrees.")
    stem = path.name.removesuffix(suffix)
    match = re.fullmatch(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(.+)", stem)
    if match is None:
        message = "A rollout requires a canonical physical filename."
        raise InvalidRecordError(message)
    try:
        datetime.strptime(match[1], "%Y-%m-%dT%H-%M-%S").replace(tzinfo=UTC)
        owner, separator, rollout = match[2].partition("_")
        ownership = OwnershipEdge(
            ThreadId.parse(owner), RolloutId.parse(rollout if separator else owner)
        )
    except ValueError as error:
        message = "A rollout filename has an invalid timestamp or identity."
        raise InvalidRecordError(message) from error
    _require(
        condition=owner == str(ownership.thread_id.value), message="Owner UUID is not canonical."
    )
    _require(
        condition=(rollout if separator else owner) == str(ownership.rollout_id.value),
        message="Rollout UUID is not canonical.",
    )
    return ownership


def _filename(ownership: OwnershipEdge, physical: PhysicalIdentity) -> None:
    _require(
        condition=ownership_from_path(physical.path, physical.representation) == ownership,
        message="Filename logical owner or immutable rollout ID disagrees.",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RolloutMetadata:
    """Observed ownership, physical representation and independent history provenance."""

    ownership: OwnershipEdge
    history_mode: HistoryMode
    physical: PhysicalIdentity | None
    history_prefix: HistoryPrefix | None
    logical_fork: LogicalForkBoundary | None

    def __post_init__(self) -> None:
        """Validate facts without traversing providers or inventing missing physical data."""
        _require(
            condition=type(self.ownership) is OwnershipEdge,
            message="Rollout ownership must be typed.",
        )
        _require(
            condition=type(self.history_mode) is HistoryMode,
            message="History mode must be explicit.",
        )
        _require(
            condition=self.history_prefix is None or type(self.history_prefix) is HistoryPrefix,
            message="History prefix must be typed or explicitly absent.",
        )
        _require(
            condition=self.logical_fork is None or type(self.logical_fork) is LogicalForkBoundary,
            message="Logical fork must be typed or explicitly absent.",
        )
        if self.physical is not None:
            _require(
                condition=type(self.physical) is PhysicalIdentity,
                message="Physical identity must be typed.",
            )
            _filename(self.ownership, self.physical)


@dataclass(frozen=True, slots=True)
class CurrentRolloutPointer:
    """SQLite's raw path remains distinct from the actual observed representation."""

    ownership: OwnershipEdge
    path: Path

    def __post_init__(self) -> None:
        """Keep current selection explicit without requiring the file to remain present."""
        _require(
            condition=type(self.ownership) is OwnershipEdge,
            message="Current ownership must be typed.",
        )
        _path(self.path)


def _pointer_matches_observation(raw_path: Path, physical: PhysicalIdentity) -> bool:
    """Align only two canonical names of one already-observed immutable rollout."""
    plain = (
        physical.path
        if physical.representation is PhysicalRepresentation.PLAIN
        else physical.path.with_suffix("")
    )
    compressed = plain.with_name(plain.name + ".zst")
    return raw_path in (plain, compressed)


@dataclass(frozen=True, slots=True)
class PhysicalInventory:
    """Detached physical observations; duplicate identities remain ambiguous."""

    rollouts: tuple[RolloutMetadata, ...]
    current_pointers: tuple[CurrentRolloutPointer, ...]
    completeness: InventoryCompleteness

    def __post_init__(self) -> None:
        """Reject conflicting IDs, paths, descriptor aliases and current pointers."""
        _require(
            condition=type(self.rollouts) is tuple,
            message="Rollout observations must be immutable.",
        )
        _require(
            condition=type(self.current_pointers) is tuple,
            message="Current pointers must be immutable.",
        )
        _require(
            condition=type(self.completeness) is InventoryCompleteness,
            message="Coverage must be explicit.",
        )
        ids, paths, descriptors = set(), set(), set()
        by_id = {}
        for item in self.rollouts:
            _require(
                condition=type(item) is RolloutMetadata,
                message="Inventory requires typed rollout metadata.",
            )
            identifier = item.ownership.rollout_id
            _require(
                condition=identifier not in ids,
                message="Duplicate or competing rollout representations.",
            )
            ids.add(identifier)
            by_id[identifier] = item
            if item.physical is not None:
                physical = item.physical
                key = physical.stat.dev, physical.stat.ino
                _require(
                    condition=physical.path not in paths,
                    message="A physical path has competing owners.",
                )
                _require(
                    condition=key not in descriptors,
                    message="One descriptor identity has multiple paths.",
                )
                paths.add(physical.path)
                descriptors.add(key)
        owners = set()
        for pointer in self.current_pointers:
            _require(
                condition=type(pointer) is CurrentRolloutPointer,
                message="Current pointer must be typed.",
            )
            _require(
                condition=pointer.ownership.thread_id not in owners,
                message="Duplicate current owner pointer.",
            )
            owners.add(pointer.ownership.thread_id)
            observed = by_id.get(pointer.ownership.rollout_id)
            if observed is not None:
                _require(
                    condition=observed.ownership == pointer.ownership,
                    message="Current pointer owner disagrees.",
                )
                _require(
                    condition=observed.physical is None
                    or _pointer_matches_observation(pointer.path, observed.physical),
                    message="Current pointer path disagrees with observed rollout.",
                )


@dataclass(frozen=True, slots=True)
class SelectedPhysicalManifest:
    """All observed physical effects for exact selected logical owners, never providers."""

    selected_threads: tuple[ThreadId, ...]
    inventory: PhysicalInventory

    def __post_init__(self) -> None:
        """Refuse incomplete ownership observations and effects outside selected owners."""
        _require(
            condition=type(self.selected_threads) is tuple, message="Selection must be immutable."
        )
        _require(
            condition=bool(self.selected_threads),
            message="An effect manifest requires selected owners.",
        )
        for owner in self.selected_threads:
            _require(
                condition=type(owner) is ThreadId, message="Selection requires logical thread IDs."
            )
        _require(
            condition=len(set(self.selected_threads)) == len(self.selected_threads),
            message="Duplicate selection.",
        )
        _require(
            condition=type(self.inventory) is PhysicalInventory,
            message="Manifest inventory must be typed.",
        )
        _require(
            condition=self.inventory.completeness is InventoryCompleteness.COMPLETE,
            message="Incomplete namespace observation cannot certify an exact physical manifest.",
        )
        selected = self.physical_rollouts
        _require(
            condition={item.ownership.thread_id for item in selected} == set(self.selected_threads),
            message="A selected owner has no observed physical rollout.",
        )
        for item in selected:
            _require(
                condition=item.physical is not None,
                message="Selected physical identity is unresolved.",
            )
            _require(
                condition=item.history_mode is not HistoryMode.UNKNOWN,
                message="Selected history mode is unknown.",
            )
        selected_owners = frozenset(self.selected_threads)
        pointers = {
            pointer.ownership.thread_id: pointer
            for pointer in self.inventory.current_pointers
            if pointer.ownership.thread_id in selected_owners
        }
        _require(
            condition=set(pointers) == set(self.selected_threads),
            message="Selected current pointer is unknown.",
        )
        owned = {item.ownership.rollout_id for item in selected}
        _require(
            condition=all(pointer.ownership.rollout_id in owned for pointer in pointers.values()),
            message="A selected current rollout is missing from its complete owned inventory.",
        )

    @property
    def physical_rollouts(self) -> tuple[RolloutMetadata, ...]:
        """Derive all selected-owner representations without following provider references."""
        selected_owners = frozenset(self.selected_threads)
        return tuple(
            item for item in self.inventory.rollouts if item.ownership.thread_id in selected_owners
        )
