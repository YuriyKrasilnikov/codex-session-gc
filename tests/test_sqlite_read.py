# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from codex_session_gc.sqlite_read import DirectReadError, read_transaction


def test_live_wal_snapshot_and_query_only(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE facts(value TEXT)")
        writer.execute("INSERT INTO facts VALUES ('first')")
        writer.commit()
        with read_transaction(path) as reader:
            assert reader.execute("SELECT value FROM facts").fetchall() == [("first",)]
            writer.execute("UPDATE facts SET value='second'")
            writer.commit()
            assert reader.execute("SELECT value FROM facts").fetchall() == [("first",)]
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("DELETE FROM facts")
        with read_transaction(path) as reader:
            assert reader.execute("SELECT value FROM facts").fetchall() == [("second",)]


def test_replaced_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE facts(value TEXT)")

    def replace_during_read() -> None:
        with read_transaction(path) as reader:
            assert reader.execute("SELECT * FROM facts").fetchall() == []
            path.rename(tmp_path / "previous.sqlite")
            with sqlite3.connect(path) as replacement:
                replacement.execute("CREATE TABLE facts(value TEXT)")

    with pytest.raises(DirectReadError, match="physical identity changed"):
        replace_during_read()


def test_database_bound_and_symlink_refusal(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE facts(value TEXT)")
    with (
        pytest.raises(DirectReadError, match="size bound"),
        read_transaction(path, maximum_bytes=1),
    ):
        pytest.fail("Oversize database admitted")
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(path)
    with pytest.raises(OSError, match="Too many levels"), read_transaction(alias):
        pytest.fail("Symlink database admitted")
