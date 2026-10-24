# Copyright (c) 2026

"""Observe the finite managed physical namespace without retaining raw history headers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from codex_session_gc.domain import (
    CurrentRolloutPointer,
    FileStatIdentity,
    HistoryMode,
    HistoryPrefix,
    InvalidRecordError,
    InventoryCompleteness,
    LogicalForkBoundary,
    PhysicalIdentity,
    PhysicalInventory,
    PhysicalRepresentation,
    RolloutId,
    RolloutMetadata,
    ThreadId,
)
from codex_session_gc.domain.physical import ownership_from_path
from codex_session_gc.execution.closed_work_request import (
    CollectionBudget,
    HeaderObservation,
    PreparationRefused,
    is_empty_regular,
    observe_header,
)
from codex_session_gc.planning.physical_lineage import normalize_history_providers

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


def _files(home: Path) -> Iterator[Path]:
    for name in ("sessions", "archived_sessions"):
        root = home / name
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        yield from _directory(descriptor, root)


def _directory(descriptor: int, path: Path) -> Iterator[Path]:
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if entry.is_symlink():
                    message = "A managed physical namespace contains an unresolved alias."
                    raise PreparationRefused(message)
                child = path / entry.name
                if entry.is_dir(follow_symlinks=False):
                    nested = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    yield from _directory(nested, child)
                elif entry.is_file(follow_symlinks=False):
                    yield child
                else:
                    message = "A managed physical namespace contains an unsupported object."
                    raise PreparationRefused(message)
    finally:
        os.close(descriptor)


def _metadata(header: HeaderObservation, representation: PhysicalRepresentation) -> RolloutMetadata:
    ownership = ownership_from_path(header.path, representation)
    metadata = header.metadata
    if metadata.get("id") != str(ownership.thread_id.value):
        message = "Physical header owner disagrees with its canonical filename."
        raise PreparationRefused(message)
    prefix = metadata.get("history_base")
    if prefix is not None:
        if not isinstance(prefix, dict) or set(prefix) != {
            "thread_id",
            "end_ordinal_exclusive",
            "end_byte_offset",
        }:
            message = "A physical history prefix is malformed."
            raise PreparationRefused(message)
        prefix = HistoryPrefix(
            RolloutId.parse(prefix["thread_id"]),
            prefix["end_ordinal_exclusive"],
            prefix["end_byte_offset"],
        )
    fork = metadata.get("forked_from_id")
    coordinate = metadata.get("forked_from_ordinal_exclusive")
    if coordinate is not None and type(coordinate) is not int:
        message = "The logical fork coordinate must be an explicit u64 or absent."
        raise PreparationRefused(message)
    return RolloutMetadata(
        ownership=ownership,
        history_mode=HistoryMode(str(metadata.get("history_mode", "legacy"))),
        physical=PhysicalIdentity(
            header.path,
            representation,
            FileStatIdentity(*header.identity),
            header.decoded_header_sha256,
        ),
        history_prefix=prefix,
        logical_fork=None
        if fork is None
        else LogicalForkBoundary(ThreadId.parse(fork), coordinate),
    )


def observe_inventory(  # noqa: C901
    home: Path,
    current_rows: Mapping[str, Mapping[str, object]],
    capture_owners: frozenset[ThreadId],
    budget: CollectionBudget,
) -> tuple[PhysicalInventory, dict[Path, HeaderObservation]]:
    """Read every active/archive header, including orphan consumer owners.

    Definitely empty regular files preserve the qualified zero-file exception;
    they supply no fabricated metadata and cannot satisfy a provider reference.
    Only headers needed by current SQL pointers are retained as small projections.
    """
    items = []
    headers = {}
    seen = set()
    for path in _files(home):
        budget.check()
        representation = (
            PhysicalRepresentation.ZSTD
            if path.name.endswith(".jsonl.zst")
            else PhysicalRepresentation.PLAIN
        )
        empty = is_empty_regular(path)
        try:
            ownership = ownership_from_path(path, representation)
        except InvalidRecordError:
            if empty:
                continue
            raise
        if ownership.rollout_id in seen:
            message = "One physical rollout has duplicate or competing representations."
            raise PreparationRefused(message)
        seen.add(ownership.rollout_id)
        if empty:
            row = current_rows.get(str(ownership.thread_id.value))
            raw = None if row is None else Path(str(row["rollout_path"]))
            plain = path.with_suffix("") if representation is PhysicalRepresentation.ZSTD else path
            if ownership.thread_id in capture_owners or raw in (
                plain,
                plain.with_name(plain.name + ".zst"),
            ):
                message = "A selected or current physical history is empty."
                raise PreparationRefused(message)
            continue
        selected = ownership.thread_id in capture_owners
        header = observe_header(
            path, budget, require_stable_extent=selected, enforce_extent_limit=selected
        )
        item = _metadata(header, representation)
        items.append(item)
        row = current_rows.get(str(ownership.thread_id.value))
        if row is not None and ownership.thread_id in capture_owners:
            raw = Path(str(row["rollout_path"]))
            plain = path.with_suffix("") if representation is PhysicalRepresentation.ZSTD else path
            if raw in (plain, plain.with_name(plain.name + ".zst")):
                headers[path] = header
    pointers = []
    for owner, row in current_rows.items():
        raw = Path(str(row["rollout_path"]))
        representation = (
            PhysicalRepresentation.ZSTD
            if raw.name.endswith(".jsonl.zst")
            else PhysicalRepresentation.PLAIN
        )
        ownership = ownership_from_path(raw, representation)
        if str(ownership.thread_id.value) != owner:
            message = "Current SQL pointer logical identity disagrees."
            raise PreparationRefused(message)
        pointers.append(CurrentRolloutPointer(ownership, raw))
    inventory = PhysicalInventory(tuple(items), tuple(pointers), InventoryCompleteness.COMPLETE)
    normalize_history_providers(inventory)
    return inventory, headers
