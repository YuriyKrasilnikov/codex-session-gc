# Copyright (c) 2026
# ruff: noqa: D103, S101, TC003

"""Synthetic selected-alias bounds and retained ownership; not producer qualification."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from codex_session_gc.execution.closed_work_request import DatabaseKind, PreparationRefused
from codex_session_gc.planning.closed_work import select_closed_work
from tests.current_storage_fixture import _CUTOFF, _fixture, _id, _prepare, _refresh_physical


def _row(home: Path, session: int) -> None:
    with closing(sqlite3.connect(home / DatabaseKind.STATE)) as db:
        db.execute(
            "INSERT INTO session_model_ids VALUES (?, 'model', 'x', 0, 'initial')",
            (str(_id(session).value),),
        )
        db.commit()


def _bound(payload: bytes) -> list[int]:
    value = json.loads(payload)
    state = next(item for item in value["databases"] if item["path"].endswith("state_5.sqlite"))
    return state["rows"]["session_model_ids"]


def test_alias_rows_are_exact_selected_namespaces_and_empty_is_declared(tmp_path: Path) -> None:
    facts, _, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    selection = select_closed_work(
        facts.snapshot, _CUTOFF, frozenset({_id(1)}), selected_threads=frozenset({_id(2)})
    )
    _row(home, 1)
    assert _bound(_prepare(tmp_path, facts, selection, helper).data) == []
    _row(home, 2)
    assert _bound(_prepare(tmp_path, facts, selection, helper).data) == [2]


def test_retained_header_without_parent_refuses_only_when_alias_exists(tmp_path: Path) -> None:
    facts, selection, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    orphan = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(4).value}.jsonl"
    orphan.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": str(_id(4).value),
                    "source": "cli",
                    "session_id": str(_id(1).value),
                },
            }
        )
        + "\n"
    )
    facts = _refresh_physical(tmp_path, facts)
    assert _bound(_prepare(tmp_path, facts, selection, helper).data) == []
    _row(home, 1)
    with pytest.raises(PreparationRefused, match="consumes a selected session"):
        _prepare(tmp_path, facts, selection, helper)


def test_ambiguous_agent_session_and_missing_current_header_refuse(tmp_path: Path) -> None:
    facts, selection, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    _row(home, 1)
    retained = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(3).value}.jsonl"
    retained.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": str(_id(3).value), "source": {"subagent": "review"}},
            }
        )
        + "\n"
    )
    facts = _refresh_physical(tmp_path, facts)
    with pytest.raises(PreparationRefused, match="ambiguous session"):
        _prepare(tmp_path, facts, selection, helper)
    retained.unlink()
    facts = _refresh_physical(tmp_path, facts)
    with pytest.raises(PreparationRefused, match="no resolvable current history"):
        _prepare(tmp_path, facts, selection, helper)


def test_reconciliation_checks_selected_alias_namespace_only(tmp_path: Path) -> None:
    import time  # noqa: PLC0415

    from codex_session_gc import sqlite_engine  # noqa: PLC0415
    from codex_session_gc.execution import closed_work_reconcile as reconcile  # noqa: PLC0415
    from codex_session_gc.execution.closed_work_request import CollectionBudget  # noqa: PLC0415
    from tests.current_storage_fixture import _LIMITS  # noqa: PLC0415

    _fixture(tmp_path)
    home = tmp_path / "home"
    _row(home, 1)
    _row(home, 3)
    selected = str(_id(1).value)
    request = reconcile._Request(  # noqa: SLF001
        home, home, selected, (selected,), (), (), (), frozenset({DatabaseKind.STATE})
    )
    with closing(sqlite_engine.connect(home / DatabaseKind.STATE)) as db:
        for table in (
            "threads",
            "thread_spawn_edges",
            "thread_dynamic_tools",
            "thread_attachments",
            "guardian_review_feedback",
            "thread_read_receipts",
        ):
            db.execute(f"DELETE FROM {table}")  # noqa: S608
        db.commit()
        budget = CollectionBudget(_LIMITS, time.monotonic() + _LIMITS.deadline_seconds)
        with pytest.raises(PreparationRefused, match="logical effect remains"):
            reconcile._effects_absent(db, DatabaseKind.STATE, request, budget)  # noqa: SLF001
        db.execute("DELETE FROM session_model_ids WHERE session_id=?", (selected,))
        db.commit()
        reconcile._effects_absent(db, DatabaseKind.STATE, request, budget)  # noqa: SLF001
        assert db.execute("SELECT session_id FROM session_model_ids").fetchall() == [
            (str(_id(3).value),)
        ]


def test_independent_retained_fork_session_is_allowed(tmp_path: Path) -> None:
    facts, selection, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    retained = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(3).value}.jsonl"
    retained.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": str(_id(3).value),
                    "session_id": str(_id(3).value),
                    "source": "cli",
                    "forked_from_id": str(_id(1).value),
                },
            }
        )
        + "\n"
    )
    facts = _refresh_physical(tmp_path, facts)
    _row(home, 1)
    _row(home, 3)
    assert _bound(_prepare(tmp_path, facts, selection, helper).data) == [1]


def test_identified_empty_retained_history_refuses_with_selected_alias(tmp_path: Path) -> None:
    facts, selection, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    orphan = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(4).value}.jsonl"
    orphan.touch()
    facts = _refresh_physical(tmp_path, facts)
    _row(home, 1)
    with pytest.raises(PreparationRefused, match="Retained session ownership"):
        _prepare(tmp_path, facts, selection, helper)


def test_retained_session_matching_empty_selected_namespace_is_allowed(tmp_path: Path) -> None:
    facts, selection, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    retained = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(3).value}.jsonl"
    retained.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": str(_id(3).value),
                    "session_id": str(_id(2).value),
                    "source": "cli",
                },
            }
        )
        + "\n"
    )
    facts = _refresh_physical(tmp_path, facts)
    _row(home, 1)
    assert _bound(_prepare(tmp_path, facts, selection, helper).data) == [1]
    _row(home, 2)
    with pytest.raises(PreparationRefused, match="consumes a selected session"):
        _prepare(tmp_path, facts, selection, helper)
