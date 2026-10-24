# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from codex_session_gc.domain import ArchiveState, ThreadId, ThreadOrigin, UtcCutoff
from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
from codex_session_gc.execution.closed_work_request import PreparationRefused
from codex_session_gc.execution.resource_policy import collection_limits
from codex_session_gc.planning.closed_work import select_closed_work
from tests.current_storage_fixture import _FORK, _MAIN, _native_home, _source

_CUTOFF = UtcCutoff(datetime(2030, 1, 1, tzinfo=UTC))
_LIFECYCLE = {
    "parent_thread_id",
    "forked_from_id",
    "forked_from_ordinal_exclusive",
    "thread_source",
    "agent_nickname",
    "multi_agent_version",
    "history_base",
    "subagent_history_start_ordinal",
}


def _home(home: Path, *, archived: bool = False, source: str = "vscode") -> tuple[Path, Path]:
    _native_home(home)
    paths = {
        identifier: next(path for path in home.rglob("*.jsonl") if identifier in path.name)
        for identifier in (_MAIN, _FORK)
    }
    rows = [json.loads(line) for line in paths[_MAIN].read_text().splitlines()]
    # A native Legacy copy owns every copied body record, without provider dependence.
    for row in rows:
        row.pop("ordinal", None)
    metadata = rows[0]["payload"]
    for key in _LIFECYCLE:
        metadata.pop(key, None)
    metadata.update(session_id=_MAIN, id=_MAIN, source=source, history_mode="legacy")
    root_rows = json.loads(json.dumps(rows))
    fork_rows = json.loads(json.dumps(rows))
    fork_rows[0]["payload"].update(
        session_id=_FORK,
        id=_FORK,
        forked_from_id=_MAIN,
        thread_source="user",
        multi_agent_version="v1",
    )
    if archived:
        paths[_MAIN].rename(home / "archived_sessions" / paths[_MAIN].name)
        paths[_MAIN] = home / "archived_sessions" / paths[_MAIN].name
    paths[_MAIN].write_text("".join(json.dumps(row) + "\n" for row in root_rows))
    paths[_FORK].write_text("".join(json.dumps(row) + "\n" for row in fork_rows))
    for path in home.rglob("*.jsonl"):
        if path not in paths.values():
            path.unlink()
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("DELETE FROM thread_spawn_edges")
        db.execute("DELETE FROM threads WHERE id NOT IN (?,?)", (_MAIN, _FORK))
        db.execute(
            (
                "UPDATE threads SET source=?,history_mode='legacy',thread_source=NULL,"
                "archived=?,rollout_path=? WHERE id=?"
            ),
            (source, int(archived), str(paths[_MAIN]), _MAIN),
        )
        db.execute(
            (
                "UPDATE threads SET source=?,history_mode='legacy',thread_source='user',"
                "rollout_path=? WHERE id=?"
            ),
            (source, str(paths[_FORK]), _FORK),
        )
    _history_owners(home, (_MAIN, _FORK))
    return paths[_MAIN], paths[_FORK]


def _history_owners(home: Path, keep: tuple[str, ...]) -> None:
    marks = ",".join("?" for _ in keep)
    with sqlite3.connect(home / "thread_history_1.sqlite") as db:
        for table in (
            "thread_turns",
            "thread_items",
            "thread_realtime_items",
            "thread_history_projection_state",
        ):
            db.execute(f"DELETE FROM {table} WHERE thread_id NOT IN ({marks})", keep)  # noqa: S608


def _rewrite(path: Path, values: dict[str, object]) -> None:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["payload"].update(values)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.mark.parametrize("source", ["cli", "exec", "vscode"])
def test_native_user_root_facts_keep_independent_fork_outside_scope(
    tmp_path: Path, source: str
) -> None:
    _, fork = _home(tmp_path, archived=True, source=source)
    before = _source(tmp_path)
    facts = read_closed_work_facts(
        tmp_path, _CUTOFF, frozenset({ThreadId.parse(_MAIN)}), None, collection_limits()
    )
    (record,) = facts.snapshot.threads
    assert record.origin is ThreadOrigin.MAIN
    assert record.archive_state is ArchiveState.ARCHIVED
    assert record.last_activity.timestamp is not None
    assert facts.snapshot.spawn_edges == ()
    selection = select_closed_work(facts.snapshot, _CUTOFF, frozenset({ThreadId.parse(_MAIN)}))
    assert selection.conditional_candidates == frozenset({ThreadId.parse(_MAIN)})
    assert ThreadId.parse(_FORK) not in selection.ownership_scope
    assert fork.is_file()
    assert facts.incoming_references_complete
    assert _source(tmp_path) == before


def test_explicit_independent_history_survives_absent_parent(tmp_path: Path) -> None:
    parent, fork = _home(tmp_path)
    parent.unlink()
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("DELETE FROM threads WHERE id=?", (_MAIN,))
    before = _source(tmp_path)
    facts = read_closed_work_facts(
        tmp_path, _CUTOFF, frozenset({ThreadId.parse(_FORK)}), None, collection_limits()
    )
    assert [record.id for record in facts.snapshot.threads] == [ThreadId.parse(_FORK)]
    assert facts.snapshot.history_provider_references == ()
    assert facts.incoming_references_complete
    assert fork.is_file()
    assert _source(tmp_path) == before


@pytest.mark.parametrize("source", ["unknown", "mcp"])
def test_unqualified_native_root_sources_are_not_promoted(tmp_path: Path, source: str) -> None:
    _home(tmp_path, source=source)
    facts = read_closed_work_facts(
        tmp_path, _CUTOFF, frozenset({ThreadId.parse(_MAIN)}), None, collection_limits()
    )
    assert facts.snapshot.threads[0].origin is ThreadOrigin.UNKNOWN
    assert not select_closed_work(
        facts.snapshot, _CUTOFF, frozenset({ThreadId.parse(_MAIN)})
    ).conditional_candidates


def test_selected_header_owner_disagreement_is_refused(tmp_path: Path) -> None:
    parent, _ = _home(tmp_path)
    _rewrite(parent, {"id": _FORK})
    with pytest.raises(PreparationRefused, match="owner disagrees"):
        read_closed_work_facts(
            tmp_path, _CUTOFF, frozenset({ThreadId.parse(_MAIN)}), None, collection_limits()
        )
