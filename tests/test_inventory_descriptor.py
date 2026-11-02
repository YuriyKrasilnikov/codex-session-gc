# Copyright (c) 2026

"""Physical inventory observations retain identity and close borrowed resources."""

from __future__ import annotations

import json
import os
import time
from compression import zstd
from pathlib import Path
from uuid import UUID

import pytest

from codex_session_gc.domain import InvalidRecordError, ThreadId
from codex_session_gc.execution import bounded_header
from codex_session_gc.execution.closed_work_request import CollectionBudget, PreparationRefused
from codex_session_gc.execution.physical_inventory import observe_inventory
from codex_session_gc.execution.resource_policy import collection_limits


def _file(home: Path) -> Path:
    identifier = str(UUID(int=2))
    path = home / "sessions" / "nested" / f"rollout-2026-06-01T00-00-00-{identifier}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": identifier, "history_mode": "legacy"}}
        )
        + "\n"
    )
    return path


def _budget() -> CollectionBudget:
    limits = collection_limits()
    return CollectionBudget(limits, time.monotonic() + limits.deadline_seconds)


def _fds() -> set[str]:
    return {item.name for item in Path("/proc/self/fd").iterdir()}


def test_inventory_refuses_parent_replacement_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement ancestry cannot pass the final current-path identity check."""
    path = _file(tmp_path)
    inode = path.stat().st_ino
    original = os.pread
    changed = False

    def rename_during_read(fd: int, size: int, offset: int) -> bytes:
        nonlocal changed
        data = original(fd, size, offset)
        if not changed and os.fstat(fd).st_ino == inode:
            changed = True
            moved = path.parent.with_name("moved")
            path.parent.rename(moved)
            path.parent.mkdir()
            path.write_bytes((moved / path.name).read_bytes())
        return data

    before = _fds()
    monkeypatch.setattr(bounded_header.os, "pread", rename_during_read)
    with pytest.raises(PreparationRefused, match="changed during header capture"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if not changed or _fds() != before:
        pytest.fail("replacement did not occur or descriptors remained open")


def test_inventory_filename_failure_closes_nested_descriptors(tmp_path: Path) -> None:
    """Invalid nonempty filenames cannot leak suspended traversal descriptors."""
    path = _file(tmp_path)
    path.rename(path.with_name("not-a-rollout.jsonl"))
    before = _fds()
    with pytest.raises(InvalidRecordError):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if _fds() != before:
        pytest.fail("an inventory descriptor remained open")


def test_inventory_selected_and_empty_current_refuse(tmp_path: Path) -> None:
    """An empty current object cannot satisfy an admitted physical identity."""
    path = _file(tmp_path)
    path.write_bytes(b"")
    identifier = ThreadId(UUID(int=2))
    with pytest.raises(PreparationRefused, match="physical history is empty"):
        observe_inventory(
            tmp_path,
            {str(identifier.value): {"rollout_path": str(path)}},
            frozenset({identifier}),
            _budget(),
        )


@pytest.mark.parametrize("selected", [False, True])
def test_inventory_append_respects_selected_extent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, selected: bool
) -> None:
    """Unrelated tail append is allowed; a selected extent change is refused."""
    path = _file(tmp_path)
    inode = path.stat().st_ino
    original = os.pread
    changed = False

    def append_during_read(fd: int, size: int, offset: int) -> bytes:
        nonlocal changed
        data = original(fd, size, offset)
        if not changed and os.fstat(fd).st_ino == inode:
            changed = True
            with path.open("ab") as stream:
                stream.write(b"new body\n")
        return data

    monkeypatch.setattr(bounded_header.os, "pread", append_during_read)
    owners = frozenset({ThreadId(UUID(int=2))}) if selected else frozenset()
    if selected:
        with pytest.raises(PreparationRefused, match="changed during metadata observation"):
            observe_inventory(tmp_path, {}, owners, _budget())
    else:
        inventory, _ = observe_inventory(tmp_path, {}, owners, _budget())
        if len(inventory.rollouts) != 1:
            pytest.fail("unrelated append lost the observed physical object")
    if not changed:
        pytest.fail("append did not occur during header capture")


def test_inventory_rejects_competing_representation(tmp_path: Path) -> None:
    """Competing plain and compressed files cannot claim one rollout identity."""
    path = _file(tmp_path)
    path.with_name(path.name + ".zst").write_bytes(zstd.compress(path.read_bytes()))
    with pytest.raises(PreparationRefused, match="duplicate or competing"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())


def test_inventory_budget_refuses_without_leaking_descriptors(tmp_path: Path) -> None:
    """The captured-input byte ceiling applies to borrowed file descriptors."""
    _file(tmp_path)
    limits = collection_limits(maximum_bytes=4)
    before = _fds()
    with pytest.raises(PreparationRefused):
        observe_inventory(
            tmp_path, {}, frozenset(), CollectionBudget(limits, time.monotonic() + 10)
        )
    if before != _fds():
        pytest.fail("a descriptor remained open after budget refusal")


def test_inventory_refuses_header_change_during_unrelated_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained body append cannot conceal a simultaneous header rewrite."""
    path = _file(tmp_path)
    inode = path.stat().st_ino
    original = os.pread
    changed = False

    def rewrite_during_read(fd: int, size: int, offset: int) -> bytes:
        nonlocal changed
        data = original(fd, size, offset)
        if not changed and os.fstat(fd).st_ino == inode:
            changed = True
            path.write_bytes(data.replace(b"legacy", b"other!") + b"new body\n")
        return data

    monkeypatch.setattr(bounded_header.os, "pread", rewrite_during_read)
    with pytest.raises(PreparationRefused, match="metadata header changed"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if not changed:
        pytest.fail("header rewrite did not occur")


def test_inventory_accepts_single_compressed_header(tmp_path: Path) -> None:
    """A compressed physical object has the same owner and bounded observation."""
    path = _file(tmp_path)
    compressed = path.with_name(path.name + ".zst")
    compressed.write_bytes(zstd.compress(path.read_bytes()))
    path.unlink()
    inventory, _ = observe_inventory(tmp_path, {}, frozenset(), _budget())
    if len(inventory.rollouts) != 1 or inventory.rollouts[0].ownership.thread_id != ThreadId(
        UUID(int=2)
    ):
        pytest.fail("compressed rollout owner was not observed")


def test_inventory_refuses_symlink_and_closes_nested_descriptors(tmp_path: Path) -> None:
    """A symlink in the managed namespace is not admitted as a regular file."""
    path = _file(tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    before = _fds()
    with pytest.raises(PreparationRefused, match="unresolved alias"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if _fds() != before:
        pytest.fail("a descriptor remained open after symlink refusal")


def test_inventory_refuses_invalid_header_and_closes_descriptors(tmp_path: Path) -> None:
    """A malformed metadata envelope cannot leave file or traversal ownership open."""
    path = _file(tmp_path)
    path.write_text('{"type":"wrong"}\n')
    before = _fds()
    with pytest.raises(PreparationRefused, match="metadata line is unavailable"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if _fds() != before:
        pytest.fail("a descriptor remained open after malformed metadata")


def test_inventory_expired_deadline_closes_descriptors(tmp_path: Path) -> None:
    """An expired cooperative deadline cannot leak traversal ownership."""
    _file(tmp_path)
    before = _fds()
    with pytest.raises(PreparationRefused, match="deadline expired"):
        observe_inventory(tmp_path, {}, frozenset(), CollectionBudget(collection_limits(), 0))
    if _fds() != before:
        pytest.fail("a descriptor remained open after deadline refusal")


def test_inventory_refuses_ancestor_symlink_before_header_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No rollout bytes are read through an aliased namespace ancestor."""
    real = tmp_path / "real"
    _file(real)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    original = os.pread
    reads = 0

    def count_reads(fd: int, size: int, offset: int) -> bytes:
        nonlocal reads
        reads += 1
        return original(fd, size, offset)

    before = _fds()
    monkeypatch.setattr(bounded_header.os, "pread", count_reads)
    with pytest.raises(NotADirectoryError):
        observe_inventory(alias, {}, frozenset(), _budget())
    if reads or _fds() != before:
        pytest.fail("an aliased ancestor was read or a descriptor remained open")
