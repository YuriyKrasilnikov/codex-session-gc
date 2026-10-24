# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, SLF001

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from codex_session_gc.execution import closed_work_request as collector
from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
from codex_session_gc.planning.closed_work import select_closed_work
from tests.current_storage_fixture import _LIMITS, _fixture, _id, _prepare, _refresh_physical


def test_sparse_history_body_is_never_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]
) -> None:
    observation, selection, helper = _fixture(tmp_path)
    paths = {
        item.path
        for item in observation.rollouts
        if item.thread_id in selection.conditional_candidates
    }
    for path in paths:
        with path.open("ab") as stream:
            stream.truncate(2 * 1024**3)
    limits = replace(_LIMITS, maximum_rollout_bytes=3 * 1024**3)
    observation = _refresh_physical(tmp_path, observation, limits)
    actual_read = os.pread
    read_bytes = 0

    def measured_read(fd: int, count: int, offset: int) -> bytes:
        nonlocal read_bytes
        data = actual_read(fd, count, offset)
        read_bytes += len(data)
        return data

    monkeypatch.setattr(collector.os, "pread", measured_read)
    limits = replace(_LIMITS, maximum_rollout_bytes=3 * 1024**3)
    _prepare(tmp_path, observation, selection, helper, limits)
    record_property("body_bytes", sum(path.stat().st_size for path in paths))
    record_property("header_read_bytes", read_bytes)
    assert 0 < read_bytes <= len(paths) * 4096


def _rchar() -> int:
    fields = dict(line.split(": ", 1) for line in Path("/proc/self/io").read_text().splitlines())
    return int(fields["rchar"])


def test_unrelated_database_payload_neither_copied_nor_scanned(
    tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    observation, selection, helper = _fixture(tmp_path)
    path = tmp_path / "home" / collector.DatabaseKind.LOGS
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated_payload(data BLOB)")
        connection.execute("INSERT INTO unrelated_payload VALUES(zeroblob(?))", (32 * 1024**2,))
        schema = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' AND name!='_sqlx_migrations' ORDER BY type,name"
            )
        )
    helper = replace(
        helper,
        databases=tuple(
            replace(item, schema=schema) if item.kind is collector.DatabaseKind.LOGS else item
            for item in helper.databases
        ),
    )
    before_paths = set(tmp_path.rglob("*"))
    start = _rchar()
    _prepare(
        tmp_path,
        observation,
        selection,
        helper,
        replace(_LIMITS, maximum_database_bytes=64 * 1024**2),
    )
    consumed = _rchar() - start
    record_property("database_bytes", path.stat().st_size)
    record_property("read_bytes", consumed)
    record_property("temporary_files", len(set(tmp_path.rglob("*")) - before_paths))
    assert consumed < 1024**2
    assert set(tmp_path.rglob("*")) == before_paths


def test_complete_inventory_preserves_missing_sql_member_and_refuses_malformed_orphan(
    tmp_path: Path,
) -> None:
    observation, _, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    with sqlite3.connect(home / collector.DatabaseKind.STATE) as connection:
        connection.execute(
            "INSERT INTO thread_spawn_edges VALUES(?,?,?)",
            (str(_id(1).value), str(_id(9).value), "closed"),
        )
    facts = read_closed_work_facts(
        home, observation.cutoff, frozenset({_id(1)}), frozenset({_id(2)}), _LIMITS
    )
    selection = select_closed_work(
        facts.snapshot,
        facts.cutoff,
        frozenset({_id(1)}),
        selected_threads=frozenset({_id(2)}),
        physical_inventory=facts.physical_inventory,
    )
    assert selection.conditional_candidates == frozenset({_id(2)})
    assert _id(9) in selection.ownership_scope
    assert facts.incoming_references_complete
    assert len(facts.headers) == 1
    _prepare(tmp_path, facts, selection, helper)
    unrelated = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(8).value}.jsonl"
    unrelated.write_bytes(b"not a supported header\n" + b"x" * 1024)
    with pytest.raises(ValueError, match=r"."):
        read_closed_work_facts(
            home, observation.cutoff, frozenset({_id(1)}), frozenset({_id(2)}), _LIMITS
        )


def test_live_wal_read_transaction_is_stable_and_query_only(tmp_path: Path) -> None:
    _fixture(tmp_path)
    state = tmp_path / "home" / collector.DatabaseKind.STATE
    with sqlite3.connect(state) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        budget = collector.CollectionBudget(_LIMITS, time.monotonic() + 30)
        with collector._read_transaction(state, budget) as reader:
            old = reader.execute(
                "SELECT updated_at_ms FROM threads WHERE id=?", (str(_id(2).value),)
            ).fetchone()
            writer.execute(
                "UPDATE threads SET updated_at_ms=updated_at_ms+1 WHERE id=?", (str(_id(2).value),)
            )
            writer.commit()
            assert (
                reader.execute(
                    "SELECT updated_at_ms FROM threads WHERE id=?", (str(_id(2).value),)
                ).fetchone()
                == old
            )
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("DELETE FROM threads")


def test_complete_inventory_preserves_unrelated_regular_empty_file(tmp_path: Path) -> None:
    """The qualified empty regular-file exception does not invent header ownership."""
    observation, _, _ = _fixture(tmp_path)
    path = tmp_path / "home" / "sessions" / "unrelated-empty"
    path.write_bytes(b"")
    before = path.stat()
    facts = read_closed_work_facts(
        tmp_path / "home", observation.cutoff, frozenset({_id(1)}), frozenset({_id(2)}), _LIMITS
    )
    assert facts.incoming_references_complete
    assert all(
        item.physical is None or item.physical.path != path
        for item in facts.physical_inventory.rollouts
    )
    assert path.read_bytes() == b""
    assert path.stat() == before


def test_unrelated_large_extent_never_becomes_selected_size_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained main body can exceed the selected bound while prefix work stays finite."""
    observation, _, _ = _fixture(tmp_path)
    path = next(item.path for item in observation.rollouts if item.thread_id == _id(3))
    with path.open("ab") as output:
        output.truncate(2 * 1024**3)
    real = os.pread
    total = 0

    def measured(descriptor: int, count: int, offset: int) -> bytes:
        nonlocal total
        data = real(descriptor, count, offset)
        total += len(data)
        return data

    monkeypatch.setattr(collector.os, "pread", measured)
    facts = read_closed_work_facts(
        tmp_path / "home", observation.cutoff, frozenset({_id(1)}), frozenset({_id(2)}), _LIMITS
    )
    assert total <= 3 * 4096
    assert facts.incoming_references_complete
    assert path.stat().st_size == 2 * 1024**3
    with pytest.raises(collector.PreparationRefused, match="size bound"):
        read_closed_work_facts(
            tmp_path / "home", observation.cutoff, frozenset({_id(3)}), frozenset({_id(3)}), _LIMITS
        )


@pytest.mark.parametrize("number", [1, 3])
def test_empty_current_physical_cannot_satisfy_inventory(tmp_path: Path, number: int) -> None:
    """Current empty histories refuse whether selected or retained."""
    observation, _, _ = _fixture(tmp_path)
    path = next(item.path for item in observation.rollouts if item.thread_id == _id(number))
    path.write_bytes(b"")
    with pytest.raises(collector.PreparationRefused, match="current physical history is empty"):
        read_closed_work_facts(
            tmp_path / "home", observation.cutoff, frozenset({_id(1)}), frozenset({_id(2)}), _LIMITS
        )


def test_empty_namespace_append_race_is_not_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file becoming nonempty during FD verification does not acquire the zero exception."""
    path = tmp_path / "empty"
    path.write_bytes(b"")
    real = os.fstat
    observed = 0
    first_empty_snapshot = 2

    def changing(descriptor: int) -> os.stat_result:
        nonlocal observed
        value = real(descriptor)
        if value.st_ino == path.stat().st_ino:
            observed += 1
            if observed == first_empty_snapshot:
                path.write_bytes(b"new data")
        return value

    monkeypatch.setattr(collector.os, "fstat", changing)
    with pytest.raises(collector.PreparationRefused, match="changed during observation"):
        collector.is_empty_regular(path)


def test_empty_orphan_cannot_satisfy_referenced_physical_provider(tmp_path: Path) -> None:
    """Skipping a definitely empty object never fabricates its immutable provider metadata."""
    observation, _, _ = _fixture(tmp_path)
    home = tmp_path / "home"
    provider = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(9).value}.jsonl"
    provider.write_bytes(b"")
    child = next(item.path for item in observation.rollouts if item.thread_id == _id(2))
    envelope = json.loads(child.read_text())
    envelope["payload"].update(
        history_mode="paginated",
        history_base={
            "thread_id": str(_id(9).value),
            "end_ordinal_exclusive": 0,
            "end_byte_offset": 0,
        },
    )
    child.write_text(json.dumps(envelope) + "\n")
    with pytest.raises(ValueError, match="provider is missing"):
        read_closed_work_facts(
            home, observation.cutoff, frozenset({_id(1)}), frozenset({_id(2)}), _LIMITS
        )
    assert provider.read_bytes() == b""
