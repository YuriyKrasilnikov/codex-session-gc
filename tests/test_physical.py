# Copyright (c) 2026

"""Semantic invariants of detached physical observations and exact manifests."""

from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from codex_session_gc.domain import (
    CurrentRolloutPointer,
    FileStatIdentity,
    HistoryMode,
    HistoryPrefix,
    InvalidRecordError,
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


def _thread(number: int) -> ThreadId:
    return ThreadId(UUID(int=number))


def _rollout(number: int) -> RolloutId:
    return RolloutId(UUID(int=number))


def _metadata(
    owner: int, rollout: int, representation: PhysicalRepresentation = PhysicalRepresentation.PLAIN
) -> RolloutMetadata:
    logical, immutable = (_thread(owner), _rollout(rollout))
    ids = str(logical.value)
    if logical.value != immutable.value:
        ids += "_" + str(immutable.value)
    suffix = ".jsonl" if representation is PhysicalRepresentation.PLAIN else ".jsonl.zst"
    return RolloutMetadata(
        ownership=OwnershipEdge(logical, immutable),
        history_mode=HistoryMode.PAGINATED,
        physical=PhysicalIdentity(
            Path("/synthetic/sessions") / f"rollout-2026-10-22T00-00-00-{ids}{suffix}",
            representation,
            FileStatIdentity(1, rollout, 37, -1, 2),
            b"x" * 32,
        ),
        history_prefix=None,
        logical_fork=None,
    )


def _pointer(item: RolloutMetadata) -> CurrentRolloutPointer:
    if item.physical is None:
        message = "Test pointer requires known physical data."
        raise ValueError(message)
    return CurrentRolloutPointer(item.ownership, item.physical.path)


def _check(*, condition: bool) -> None:
    if not condition:
        pytest.fail("Physical value contract did not hold.")


class TestPhysicalValues:
    """Pure value contracts do not access files or activate an execution profile."""

    def test_prefix_and_logical_fork_keep_independent_coordinates(self) -> None:
        """Revert can change physical providers without changing logical fork provenance."""
        logical = LogicalForkBoundary(_thread(7), 99)
        original = replace(_metadata(1, 1), logical_fork=logical)
        reverted = replace(
            _metadata(1, 2), history_prefix=HistoryPrefix(_rollout(1), 4, 123), logical_fork=logical
        )
        _check(condition=original.logical_fork == reverted.logical_fork)
        _check(condition=reverted.history_prefix == HistoryPrefix(_rollout(1), 4, 123))
        _check(
            condition=LogicalForkBoundary(_thread(7), None).forked_from_ordinal_exclusive is None
        )

    def test_prefix_u64_boundaries_do_not_infer_turn_coordinates(self) -> None:
        """Ordinal and byte offsets accept exactly the source u64 range independently."""
        maximum = (1 << 64) - 1
        _check(condition=HistoryPrefix(_rollout(1), 0, maximum).end_byte_offset == maximum)
        _check(condition=HistoryPrefix(_rollout(1), maximum, 0).end_ordinal_exclusive == maximum)
        for invalid in (-1, 1 << 64, True, None, 1.0, "1"):
            value = cast("int", invalid)
            with pytest.raises(InvalidRecordError):
                HistoryPrefix(_rollout(1), value, 0)
            with pytest.raises(InvalidRecordError):
                HistoryPrefix(_rollout(1), 0, value)
            if invalid is not None:
                with pytest.raises(InvalidRecordError):
                    LogicalForkBoundary(_thread(1), value)

    def test_thread_and_rollout_types_cannot_substitute(self) -> None:
        """Equal UUID bytes do not erase the semantic identity boundary."""
        with pytest.raises(InvalidRecordError):
            HistoryPrefix(cast("RolloutId", _thread(1)), 1, 1)
        with pytest.raises(InvalidRecordError):
            LogicalForkBoundary(cast("ThreadId", _rollout(1)), 1)

    def test_stat_preserves_native_unsigned_and_signed_ranges(self) -> None:
        """Negative timestamps remain valid while unknown sizes never become zero."""
        maximum = (1 << 64) - 1
        minimum_time, maximum_time = (-(1 << 63), (1 << 63) - 1)
        known = FileStatIdentity(maximum, maximum, 0, minimum_time, maximum_time)
        _check(condition=known.stored_bytes == 0)
        for field, value in (
            ("dev", -1),
            ("ino", 1 << 64),
            ("stored_bytes", None),
            ("stored_bytes", True),
            ("mtime_ns", minimum_time - 1),
            ("ctime_ns", maximum_time + 1),
        ):
            with pytest.raises(InvalidRecordError):
                replace(known, **{field: cast("int", value)})

    def test_physical_header_identity_is_immutable_and_representation_explicit(self) -> None:
        """The same owner/rollout can be observed in either encoding without changing IDs."""
        plain, compressed = (_metadata(1, 2), _metadata(1, 2, PhysicalRepresentation.ZSTD))
        _check(condition=plain.ownership == compressed.ownership)
        _check(condition=plain.physical != compressed.physical)
        physical = plain.physical
        _check(condition=physical is not None)
        if physical is None:
            return
        for field in fields(physical):
            with pytest.raises(FrozenInstanceError):
                setattr(physical, field.name, getattr(physical, field.name))
        for mutation in (
            {"decoded_header_sha256": b"short"},
            {"decoded_header_sha256": bytearray(b"x" * 32)},
            {"path": Path("relative.jsonl")},
            {"path": Path("/synthetic/../escape.jsonl")},
            {"representation": "plain"},
            {"representation": PhysicalRepresentation.ZSTD},
        ):
            with pytest.raises(InvalidRecordError):
                replace(physical, **mutation)

    def test_filename_must_bind_logical_owner_and_immutable_rollout(self) -> None:
        """A reverted filename preserves both identities and rejects contradictions."""
        metadata = _metadata(1, 2)
        with pytest.raises(InvalidRecordError):
            replace(metadata, ownership=OwnershipEdge(_thread(2), _rollout(2)))
        with pytest.raises(InvalidRecordError):
            replace(metadata, ownership=OwnershipEdge(_thread(1), _rollout(3)))
        physical = metadata.physical
        if physical is None:
            pytest.fail("Test physical identity is required.")
        bad = replace(physical, path=physical.path.with_name("rollout-2026-13-22T00-00-00-1.jsonl"))
        with pytest.raises(InvalidRecordError):
            replace(metadata, physical=bad)

    def test_manifest_includes_all_owned_revert_rollouts_but_not_providers(self) -> None:
        """History prefix observations do not expand physical deletion authority."""
        provider = _metadata(9, 9)
        old = replace(_metadata(1, 1), history_prefix=HistoryPrefix(_rollout(9), 7, 200))
        current = replace(_metadata(1, 2), history_prefix=HistoryPrefix(_rollout(1), 3, 90))
        inventory = PhysicalInventory(
            (provider, old, current), (_pointer(current),), InventoryCompleteness.COMPLETE
        )
        selected = SelectedPhysicalManifest((_thread(1),), inventory)
        _check(condition=selected.physical_rollouts == (old, current))
        _check(condition=provider not in selected.physical_rollouts)
        _check(condition=inventory.current_pointers == (_pointer(current),))
        _check(condition=selected.physical_rollouts != (current,))

    def test_competing_representations_and_duplicate_rollouts_are_refused(self) -> None:
        """No plain-preference convention silently discards a competing representation."""
        plain, compressed = (_metadata(1, 1), _metadata(1, 1, PhysicalRepresentation.ZSTD))
        for values in ((plain, plain), (plain, compressed), (plain, _metadata(2, 1))):
            with pytest.raises(InvalidRecordError):
                PhysicalInventory(values, (), InventoryCompleteness.COMPLETE)

    def test_descriptor_aliases_and_conflicting_current_pointers_are_refused(self) -> None:
        """Distinct immutable IDs cannot alias the same observed descriptor identity."""
        first, second = (_metadata(1, 1), _metadata(2, 2))
        physical = second.physical
        if physical is None:
            pytest.fail("Test physical identity is required.")
        alias = replace(second, physical=replace(physical, stat=FileStatIdentity(1, 1, 37, -1, 2)))
        with pytest.raises(InvalidRecordError):
            PhysicalInventory((first, alias), (), InventoryCompleteness.COMPLETE)
        with pytest.raises(InvalidRecordError):
            PhysicalInventory(
                (first,), (_pointer(first), _pointer(first)), InventoryCompleteness.COMPLETE
            )
        with pytest.raises(InvalidRecordError):
            PhysicalInventory(
                (first,),
                (
                    CurrentRolloutPointer(second.ownership, physical.path),
                    CurrentRolloutPointer(OwnershipEdge(_thread(3), _rollout(1)), physical.path),
                ),
                InventoryCompleteness.COMPLETE,
            )

    def test_unknown_and_incomplete_observations_cannot_be_effect_manifests(self) -> None:
        """Retain missing extent, coverage, history mode and current-pointer uncertainty."""
        known = _metadata(1, 1)
        for values, pointers, coverage in (
            ((known,), (_pointer(known),), InventoryCompleteness.UNKNOWN),
            ((replace(known, physical=None),), (_pointer(known),), InventoryCompleteness.COMPLETE),
            (
                (replace(known, history_mode=HistoryMode.UNKNOWN),),
                (_pointer(known),),
                InventoryCompleteness.COMPLETE,
            ),
            ((known,), (), InventoryCompleteness.COMPLETE),
            ((known,), (_pointer(_metadata(1, 2)),), InventoryCompleteness.COMPLETE),
            ((), (), InventoryCompleteness.COMPLETE),
        ):
            inventory = PhysicalInventory(values, pointers, coverage)
            with pytest.raises(InvalidRecordError):
                SelectedPhysicalManifest((_thread(1),), inventory)

    def test_mutable_collections_and_duplicate_selected_owners_are_rejected(self) -> None:
        """Constructors require immutable collections rather than retaining caller lists."""
        item = _metadata(1, 1)
        with pytest.raises(InvalidRecordError):
            PhysicalInventory(
                cast("tuple[RolloutMetadata, ...]", [item]), (), InventoryCompleteness.UNKNOWN
            )
        inventory = PhysicalInventory((item,), (_pointer(item),), InventoryCompleteness.COMPLETE)
        for owners in ((), (_thread(1), _thread(1))):
            with pytest.raises(InvalidRecordError):
                SelectedPhysicalManifest(owners, inventory)

    def test_raw_current_pointer_can_name_other_representation_of_observed_rollout(self) -> None:
        """Stock compression leaves the SQL plain path while publishing a zstd file."""
        for representation in (PhysicalRepresentation.PLAIN, PhysicalRepresentation.ZSTD):
            item = _metadata(1, 2, representation)
            physical = item.physical
            if physical is None:
                pytest.fail("Test physical identity is required.")
            plain = physical.path.with_name(physical.path.name.removesuffix(".zst"))
            raw_path = (
                plain
                if representation is PhysicalRepresentation.ZSTD
                else plain.with_name(plain.name + ".zst")
            )
            pointer = CurrentRolloutPointer(item.ownership, raw_path)
            inventory = PhysicalInventory((item,), (pointer,), InventoryCompleteness.COMPLETE)
            manifest = SelectedPhysicalManifest((_thread(1),), inventory)
            _check(condition=pointer.path == raw_path)
            _check(condition=inventory.current_pointers == (pointer,))
            _check(condition=manifest.physical_rollouts == (item,))
            _check(condition=manifest.physical_rollouts[0].physical == physical)

    def test_raw_current_pointer_cannot_change_parent_basename_or_identity(self) -> None:
        """Normalization never admits unrelated paths, timestamps, owners or rollout IDs."""
        item = _metadata(1, 2, PhysicalRepresentation.ZSTD)
        physical = item.physical
        if physical is None:
            pytest.fail("Test physical identity is required.")
        plain = physical.path.with_name(physical.path.name.removesuffix(".zst"))
        for raw_path in (
            plain.with_name("unrelated.jsonl"),
            Path("/other") / plain.name,
            plain.with_name(plain.name.replace("2026-10-22", "2026-10-21")),
            plain.with_name(plain.name.replace(str(_thread(1).value), str(_thread(3).value))),
            plain.with_name(plain.name.replace(str(_rollout(2).value), str(_rollout(4).value))),
            plain.with_name(plain.name + ".zst.zst"),
        ):
            pointer = CurrentRolloutPointer(item.ownership, raw_path)
            with pytest.raises(InvalidRecordError):
                PhysicalInventory((item,), (pointer,), InventoryCompleteness.COMPLETE)
            _check(condition=pointer.path == raw_path)
