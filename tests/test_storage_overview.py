# Copyright (c) 2026
# ruff: noqa: S101, PLR2004

"""Storage results account for inode identities and stay detached from I/O."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.cli.main import main
from codex_session_gc.inventory.storage import StorageLimits, observe_storage
from tests.current_storage_fixture import _A, _catalogue_home

if TYPE_CHECKING:
    from pathlib import Path


def test_observed_totals_and_detached_pages(tmp_path: Path) -> None:
    """Account for inode aliases, symlink metadata and detached pages."""
    sessions = tmp_path / "sessions"
    cache = tmp_path / "cache"
    sessions.mkdir()
    cache.mkdir()
    history = sessions / "bad.jsonl"
    history.write_bytes(b"abc")
    os.link(history, cache / "alias")
    (cache / "link").symlink_to("/outside")
    snapshot = observe_storage(tmp_path)
    summary = snapshot.summary()
    stats = [
        tmp_path.lstat(),
        sessions.lstat(),
        cache.lstat(),
        history.lstat(),
        (cache / "link").lstat(),
    ]
    assert summary["totals"]["logical_file_bytes"]["known_bytes"] == 3
    assert summary["totals"]["allocated_bytes"]["known_bytes"] == sum(
        s.st_blocks * 512 for s in stats
    )
    assert (
        next(c for c in summary["categories"] if c["category"] == "shared")["unique_objects"] == 1
    )
    history.unlink()
    page = snapshot.page(association="unparsed")
    assert page["total_matching"] == 1
    assert page["items"][0]["display_path"] == "sessions/bad.jsonl"
    summary["totals"].clear()
    assert snapshot.summary()["totals"]["unique_objects"] == 5
    with pytest.raises(ValueError, match="Invalid storage page arguments"):
        snapshot.page(limit=101)
    assert snapshot.page(offset=10**100)["items"] == []


def test_paths_escaped_and_fs_budget(tmp_path: Path) -> None:
    """Display invalid bytes safely and report truncated observations."""
    (tmp_path / "sessions").mkdir()
    fd = os.open(
        os.fsencode(tmp_path / "sessions") + b"/bad\xff\n.jsonl", os.O_CREAT | os.O_WRONLY, 0o600
    )
    os.close(fd)
    snapshot = observe_storage(tmp_path)
    path = snapshot.page()["items"][0]["display_path"]
    assert "\\xff" in path
    assert "\\u000a" in path
    assert (
        observe_storage(tmp_path, limits=StorageLimits(max_operations=1)).summary()["fs_coverage"]
        == "partial"
    )


def test_cli_json_and_invalid_page(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Expose a detached page and reject page arguments without object scope."""
    assert main(["storage", "--home", str(tmp_path), "--json", "--objects", "all"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["objects"]["storage_snapshot_id"] == result["storage_snapshot_id"]
    assert main(["storage", "--home", str(tmp_path), "--offset", "1"]) == 2


def test_sql_positive_missing_and_budget(tmp_path: Path) -> None:
    """Report absence only after a complete observed SQL membership read."""
    _catalogue_home(tmp_path)
    missing = "00000000-0000-0000-0000-000000000099"
    (tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{missing}.jsonl").write_bytes(b"x")
    snapshot = observe_storage(tmp_path)
    assert snapshot.summary()["sql_coverage"] == "complete"
    associations = {
        row["filename_thread_id"]: row["association"] for row in snapshot.page()["items"]
    }
    assert associations[_A] == "matched"
    assert associations[missing] == "missing"
    limited = observe_storage(tmp_path, limits=StorageLimits(max_sql_rows=1))
    assert limited.summary()["sql_coverage"] == "unknown"
    assert all(row["association"] == "unknown" for row in limited.page()["items"])


def test_extra_state_candidate_with_newline_keeps_sql_unknown(tmp_path: Path) -> None:
    """Complete discovery includes non-displayable characters in state filenames."""
    _catalogue_home(tmp_path)
    (tmp_path / "state_\n.sqlite").touch()
    snapshot = observe_storage(tmp_path)
    assert snapshot.summary()["sql_coverage"] == "unknown"
    assert snapshot.summary()["counts_by_reason"]["sql_discovery"] == 1
    assert all(row["association"] == "unknown" for row in snapshot.page()["items"])


def test_oversized_sql_id_cannot_make_a_history_missing(tmp_path: Path) -> None:
    """Reject oversized membership values before materializing them in Python."""
    database = _catalogue_home(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE threads SET id=? WHERE id=?", ("y" * (1024 * 1024), _A))
    snapshot = observe_storage(tmp_path)
    assert snapshot.summary()["sql_coverage"] == "partial"
    assert snapshot.summary()["counts_by_reason"]["sql_invalid_id"] == 1
    rows = {item["filename_thread_id"]: item["association"] for item in snapshot.page()["items"]}
    assert rows[_A] == "unknown"
    assert "matched" in rows.values()


def test_expanded_display_paths_are_bounded_separately_from_totals(tmp_path: Path) -> None:
    """Keep allocated and logical totals when expanded history names exhaust the budget."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    descriptor = os.open(
        os.fsencode(sessions) + b"/" + b"\xff" * 12, os.O_CREAT | os.O_WRONLY, 0o600
    )
    try:
        os.write(descriptor, b"x")
    finally:
        os.close(descriptor)
    snapshot = observe_storage(tmp_path, limits=StorageLimits(max_path_bytes=70))
    summary = snapshot.summary()
    assert summary["fs_coverage"] == "complete"
    assert summary["history_coverage"] == "partial"
    assert summary["counts_by_reason"]["history_row_limit"] == 1
    assert summary["totals"]["logical_file_bytes"]["known_bytes"] == 1
    assert snapshot.page()["items"] == []
