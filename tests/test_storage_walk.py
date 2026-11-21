# Copyright (c) 2026
# ruff: noqa: ANN001, ANN003, ANN201, ANN202, D103, PLR2004, PTH116, S101

"""Exercise bounded, no-follow storage traversal."""

import os
from pathlib import Path

import pytest

from codex_session_gc.inventory import storage_walk as walk


def scan(path, **kwargs):
    return walk.scan_storage(
        path,
        cancelled=kwargs.pop("cancelled", lambda: False),
        max_operations=kwargs.pop("max_operations", 10000),
        max_path_bytes=kwargs.pop("max_path_bytes", 1000000),
        **kwargs,
    )


def reasons(result):
    return {gap.reason: gap.count for gap in result.gaps}


def test_observes_root_and_does_not_follow_symlink(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "dir").mkdir()
    (home / "dir" / "file").write_text("hello")
    (home / "link").symlink_to(home / "dir", target_is_directory=True)
    result = scan(home)
    assert {e.parts for e in result.entries} == {(), ("dir",), ("dir", "file"), ("link",)}
    assert result.root_complete
    assert not result.gaps
    assert result.home_identity == result.entries[0].identity


def test_limits_and_root_discovery_are_separate(tmp_path) -> None:
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "file").touch()
    result = scan(tmp_path, max_operations=1)
    assert result.root_complete
    assert "operation_limit" in reasons(result)
    result = scan(tmp_path, max_path_bytes=1)
    assert result.root_complete
    (tmp_path / "state_5.sqlite").touch()
    result = scan(tmp_path, max_path_bytes=1)
    assert not result.root_complete
    assert "path_limit" in reasons(result)


def test_detects_changed_directory_on_reopen(tmp_path, monkeypatch) -> None:
    (tmp_path / "dir").mkdir()
    original = walk.identity_for_fd
    count = 0

    def changed(fd, info=None):
        nonlocal count
        identity = original(fd, info)
        if identity.ino == os.stat(tmp_path / "dir").st_ino:
            count += 1
            if count == 2:
                return walk.MountIdentity(identity.dev, identity.ino + 1, identity.mount_id)
        return identity

    monkeypatch.setattr(walk, "identity_for_fd", changed)
    result = scan(tmp_path)
    assert reasons(result)["directory_changed"] == 1
    assert result.root_complete


def test_mount_boundary_excluded(tmp_path, monkeypatch) -> None:
    (tmp_path / "foreign").mkdir()
    original = walk.identity_for_fd

    def other_mount(fd, info=None):
        identity = original(fd, info)
        if identity.ino == os.stat(tmp_path / "foreign").st_ino:
            return walk.MountIdentity(identity.dev, identity.ino, identity.mount_id + 1)
        return identity

    monkeypatch.setattr(walk, "identity_for_fd", other_mount)
    result = scan(tmp_path)
    assert [e.parts for e in result.entries] == [()]
    assert reasons(result)["mount_boundary"] == 1
    assert result.root_complete
    (tmp_path / "foreign").rename(tmp_path / "state_5.sqlite")
    # The candidate mount cannot be represented in entries.
    monkeypatch.setattr(walk, "identity_for_fd", original)
    inode = (tmp_path / "state_5.sqlite").stat().st_ino

    def candidate_mount(fd, info=None):
        identity = original(fd, info)
        if identity.ino == inode:
            return walk.MountIdentity(identity.dev, identity.ino, identity.mount_id + 1)
        return identity

    monkeypatch.setattr(walk, "identity_for_fd", candidate_mount)
    assert not scan(tmp_path).root_complete


def test_cancel_and_root_error_are_sanitized(tmp_path) -> None:
    with pytest.raises(walk.StorageScanCancelled):
        scan(tmp_path, cancelled=lambda: True)
    with pytest.raises(walk.StorageScanError) as error:
        scan(tmp_path / "private-missing")
    assert "private-missing" not in str(error.value)


def test_deep_tree_does_not_hold_ancestor_descriptors(tmp_path, monkeypatch) -> None:
    directory = tmp_path
    for _ in range(80):
        directory = directory / "d"
        directory.mkdir()

    def descriptors_for_tree() -> int:
        total = 0
        for entry in Path("/proc/self/fd").iterdir():
            try:
                target = entry.readlink()
            except FileNotFoundError:
                continue
            if target.is_relative_to(tmp_path):
                total += 1
        return total

    real_identity = walk.identity_for_fd
    peak = 0

    def observe_identity(fd, info=None):
        nonlocal peak
        peak = max(peak, descriptors_for_tree())
        return real_identity(fd, info)

    monkeypatch.setattr(walk, "identity_for_fd", observe_identity)
    assert descriptors_for_tree() == 0
    result = scan(tmp_path)
    assert len(result.entries) == 81
    assert 0 < peak <= 4
    assert descriptors_for_tree() == 0


def test_operation_limit_stops_queued_directories(tmp_path, monkeypatch) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    original_dup = os.dup
    duplicates = 0

    def count_dup(fd):
        nonlocal duplicates
        duplicates += 1
        return original_dup(fd)

    monkeypatch.setattr(walk.os, "dup", count_dup)
    result = scan(tmp_path, max_operations=3)
    assert "operation_limit" in reasons(result)
    assert duplicates == 2  # root, then the first queued directory
