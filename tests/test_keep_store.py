# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

import sqlite3
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from codex_session_gc.domain import ThreadId
from codex_session_gc.execution.keep_store import KeepStoreError, read_kept_threads

_SCHEMA = "CREATE TABLE kept_threads (thread_id TEXT PRIMARY KEY NOT NULL) STRICT"
_ID = str(UUID(int=1))

if TYPE_CHECKING:
    from pathlib import Path


def _store(home: Path, schema: str = _SCHEMA) -> Path:
    directory = home / ".codex-session-gc"
    directory.mkdir()
    path = directory / "keep.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(schema)
    return path


def test_missing_is_empty_and_does_not_create_files(tmp_path: Path) -> None:
    assert read_kept_threads(tmp_path) == frozenset()
    assert not (tmp_path / ".codex-session-gc").exists()


def test_valid_marks_are_read_without_mutation(tmp_path: Path) -> None:
    path = _store(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO kept_threads VALUES (?)", (_ID,))
    before = path.read_bytes()
    assert read_kept_threads(tmp_path) == frozenset({ThreadId.parse(_ID)})
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "schema",
    [
        "CREATE TABLE kept_threads (thread_id TEXT PRIMARY KEY NOT NULL)",
        "CREATE TABLE other (thread_id TEXT PRIMARY KEY NOT NULL) STRICT",
    ],
)
def test_unexpected_schema_is_refused(tmp_path: Path, schema: str) -> None:
    _store(tmp_path, schema)
    with pytest.raises(KeepStoreError):
        read_kept_threads(tmp_path)


def test_sqlite_like_name_does_not_hide_a_trigger(tmp_path: Path) -> None:
    path = _store(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER sqliteX AFTER INSERT ON kept_threads "
            "BEGIN DELETE FROM kept_threads WHERE thread_id=NEW.thread_id; END"
        )
    with pytest.raises(KeepStoreError, match="unsupported schema"):
        read_kept_threads(tmp_path)


def test_a_hard_link_cannot_share_marks_between_homes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    path = _store(first)
    other = _store(second)
    other.unlink()
    other.hardlink_to(path)
    for storage in (first, second):
        with pytest.raises(KeepStoreError, match="singly linked"):
            read_kept_threads(storage)


def test_noncanonical_id_and_symlink_are_refused(tmp_path: Path) -> None:
    path = _store(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO kept_threads VALUES (?)", ("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",)
        )
    with pytest.raises(KeepStoreError):
        read_kept_threads(tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(KeepStoreError):
        read_kept_threads(tmp_path)
