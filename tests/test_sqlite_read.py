# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

import sqlite3
import subprocess
import sys
from contextlib import closing
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from codex_session_gc import sqlite_engine
from codex_session_gc.sqlite_read import DirectReadError, read_transaction


def test_live_wal_snapshot_and_query_only(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE facts(value TEXT)")
        writer.execute("INSERT INTO facts VALUES ('first')")
        writer.commit()
    with read_transaction(path) as reader:
        assert reader.execute("SELECT value FROM facts").fetchall() == [("first",)]
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-c",
                (
                    "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
                    "c.execute(\"UPDATE facts SET value='second'\"); c.commit()"
                ),
                str(path),
            ],
            check=True,
        )
        assert reader.execute("SELECT value FROM facts").fetchall() == [("first",)]
        with pytest.raises(sqlite_engine.OperationalError, match="readonly"):
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


def test_engine_identity_and_invalid_database(tmp_path: Path) -> None:
    import pysqlite3  # noqa: PLC0415

    with closing(sqlite_engine.connect(":memory:")) as connection:
        assert isinstance(connection, pysqlite3.Connection)
        version, source = connection.execute(
            "SELECT sqlite_version(), sqlite_source_id()"
        ).fetchone()
    assert version == "3.54.1"
    assert source == sqlite_engine._EXPECTED_SOURCE  # noqa: SLF001
    damaged = tmp_path / "damaged.sqlite"
    damaged.write_bytes(b"not a sqlite database")
    with (
        pytest.raises(sqlite_engine.DatabaseError),
        read_transaction(damaged) as connection,
    ):
        connection.execute("SELECT * FROM sqlite_master").fetchall()


def test_expired_read_is_cancelled(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE facts(value INTEGER)")
        writer.executemany("INSERT INTO facts VALUES (?)", [(i,) for i in range(100)])
    with (
        pytest.raises(sqlite_engine.OperationalError, match="interrupt"),
        read_transaction(path, deadline=0) as reader,
    ):
        reader.execute("SELECT count(*) FROM facts a, facts b, facts c").fetchone()


def test_storage_and_receipt_use_qualified_engine(tmp_path: Path) -> None:
    from codex_session_gc.recovery import journal  # noqa: PLC0415

    path = tmp_path / "storage.sqlite"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("CREATE TABLE facts(value INTEGER)")
    with read_transaction(path) as reader:
        assert type(reader) is sqlite_engine.Connection
        assert reader.execute("SELECT sqlite_source_id()").fetchone() == (
            sqlite_engine._EXPECTED_SOURCE,  # noqa: SLF001
        )
    with closing(journal._writable(tmp_path / "receipt.sqlite")) as receipt:  # noqa: SLF001
        assert type(receipt) is sqlite_engine.Connection
        assert receipt.execute("SELECT sqlite_source_id()").fetchone() == (
            sqlite_engine._EXPECTED_SOURCE,  # noqa: SLF001
        )


def test_mismatched_engine_refuses_before_creating_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_session_gc.recovery import journal  # noqa: PLC0415

    sqlite_engine.ensure_qualified.cache_clear()
    monkeypatch.setattr(sqlite_engine, "_EXPECTED_SOURCE", "unqualified")
    path = tmp_path / "receipt.sqlite"
    try:
        with pytest.raises(ValueError, match="Unsupported SQLite engine"):
            sqlite_engine.connect(path)
        assert not path.exists()
        # Receipt creation checks the engine before even creating its file.
        start = journal.StartEnvelope(
            journal.AttemptId(uuid4()),
            *([journal.Payload("application/test", 1, b"x")] * 4),
        )
        with pytest.raises(ValueError, match="Unsupported SQLite engine"):
            journal.create(path, start, (journal.Unit(journal.UnitId(uuid4()), 0, start.profile),))
        assert not path.exists()
    finally:
        sqlite_engine.ensure_qualified.cache_clear()
