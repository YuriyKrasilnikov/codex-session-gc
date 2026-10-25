# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.cli.console_tasks import preview_selection
from codex_session_gc.domain import (
    ArchiveState,
    GuardianAssociation,
    ThreadId,
    ThreadOrigin,
    UtcCutoff,
)
from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
from codex_session_gc.execution.closed_work_request import PreparationRefused
from codex_session_gc.execution.resource_policy import collection_limits
from codex_session_gc.planning.closed_work import select_closed_work
from tests.current_storage_fixture import (
    _CHILD,
    _GRANDCHILD,
    _MAIN,
    _fixture,
    _id,
    _native_home,
    _source,
)

if TYPE_CHECKING:
    from pathlib import Path

_CUTOFF = UtcCutoff(datetime(2030, 1, 1, tzinfo=UTC))
_ROOTS = frozenset({ThreadId.parse(_MAIN)})


def _path(home: Path, identifier: str) -> Path:
    return next(path for path in home.rglob("*.jsonl") if identifier in path.name)


def _legacy_work(home: Path) -> None:
    _native_home(home)
    for identifier in (_MAIN, _CHILD, _GRANDCHILD):
        path = _path(home, identifier)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["payload"]["history_mode"] = "legacy"
        if identifier != _MAIN:
            rows[0]["payload"]["multi_agent_version"] = "v2"
        for row in rows:
            row.pop("ordinal", None)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET history_mode='legacy'")


def test_direct_facts_preserve_legacy_v2_members_with_complete_physical_inventory(
    tmp_path: Path,
) -> None:
    _legacy_work(tmp_path)
    before = _source(tmp_path)
    facts = read_closed_work_facts(tmp_path, _CUTOFF, _ROOTS, None, collection_limits())
    records = {str(record.id.value): record for record in facts.snapshot.threads}
    assert set(records) == {_MAIN, _CHILD, _GRANDCHILD}
    assert all(record.archive_state is ArchiveState.NOT_ARCHIVED for record in records.values())
    assert all(record.last_activity.timestamp is not None for record in records.values())
    assert facts.incoming_references_complete
    assert _source(tmp_path) == before


def test_explicit_preview_keeps_conditional_v2_selection(tmp_path: Path) -> None:
    _legacy_work(tmp_path)
    result = preview_selection(tmp_path, _CUTOFF, _ROOTS, None)
    assert {_CHILD, _GRANDCHILD} <= set(result["deletions"])
    assert not result["closureCertified"]
    assert result["incomingReferencesComplete"]
    assert not result["executable"]


@pytest.mark.parametrize("tail", [b"not-json\n", b'{"unfinished"'])
def test_body_qualification_is_not_claimed_by_header_preview(tmp_path: Path, tail: bytes) -> None:
    _legacy_work(tmp_path)
    with _path(tmp_path, _GRANDCHILD).open("ab") as stream:
        stream.write(tail)
    result = preview_selection(tmp_path, _CUTOFF, _ROOTS, None)
    assert _GRANDCHILD in result["deletions"]
    assert result["incomingReferencesComplete"]
    assert not result["closureCertified"]


def test_guardian_actual_spawn_edge_is_not_reinterpreted_as_association(tmp_path: Path) -> None:
    _legacy_work(tmp_path)
    source = {"subagent": {"other": "guardian"}}
    path = _path(tmp_path, _GRANDCHILD)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["payload"]["source"] = source
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET source=? WHERE id=?", (json.dumps(source), _GRANDCHILD)
        )
    result = preview_selection(tmp_path, _CUTOFF, _ROOTS, None)
    assert _GRANDCHILD not in result["deletions"]
    assert "guardian_spawn_topology_conflict" in result["errors"]


def test_external_references_require_native_qualification(tmp_path: Path) -> None:
    _legacy_work(tmp_path)
    result = preview_selection(tmp_path, _CUTOFF, _ROOTS, None)
    assert result["incomingReferencesComplete"]
    assert not result["closureCertified"]
    assert not result["executable"]


def test_empty_selected_header_refuses_preview(tmp_path: Path) -> None:
    _legacy_work(tmp_path)
    _path(tmp_path, _MAIN).write_bytes(b"")
    with pytest.raises(PreparationRefused, match="physical history is empty"):
        preview_selection(tmp_path, _CUTOFF, _ROOTS, None)


@pytest.mark.parametrize("selected", [frozenset({_id(3)}), frozenset({_id(2), _id(3)})])
def test_existing_foreign_selection_is_rejected_by_real_reader_and_planner(
    tmp_path: Path, selected: frozenset[ThreadId]
) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    before = _source(home)
    roots = frozenset({_id(1)})
    facts = read_closed_work_facts(home, _CUTOFF, roots, selected, collection_limits())
    selection = select_closed_work(
        facts.snapshot,
        _CUTOFF,
        roots,
        selected_threads=selected,
        physical_inventory=facts.physical_inventory,
    )
    assert selection.errors == ("selected_thread_outside_closed_work",)
    assert selection.conditional_candidates == frozenset()
    assert _id(3) not in {item.thread_id for item in facts.rollouts}
    assert _source(home) == before


def test_existing_foreign_selection_has_structured_preview_refusal(tmp_path: Path) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    before = _source(home)
    result = preview_selection(home, _CUTOFF, frozenset({_id(1)}), frozenset({_id(3)}))
    assert result["errors"] == ["selected_thread_outside_closed_work"]
    assert result["deletions"] == []
    assert not result["executable"]
    assert _source(home) == before


def _guardian_case(home: Path) -> Path:
    observation, _, _ = _fixture(home)
    path = next(item.path for item in observation.rollouts if item.thread_id == _id(3))
    metadata = json.loads(path.read_text())
    metadata["payload"].update(
        source={"subagent": {"other": "guardian"}}, parent_thread_id=str(_id(1).value)
    )
    path.write_text(json.dumps(metadata) + "\n")
    with sqlite3.connect(home / "home/state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET source=? WHERE id=?",
            (json.dumps(metadata["payload"]["source"]), str(_id(3).value)),
        )
    return path


def test_selected_guardian_has_independent_historical_association(tmp_path: Path) -> None:
    path = _guardian_case(tmp_path)
    home = tmp_path / "home"
    with path.open("r+b") as stream:
        stream.truncate(128 * 1024**2)
    facts = read_closed_work_facts(home, _CUTOFF, frozenset({_id(3)}), None, collection_limits())
    assert facts.snapshot.guardian_associations == (GuardianAssociation(_id(3), _id(1)),)
    assert facts.snapshot.threads[0].origin is ThreadOrigin.GUARDIAN
    assert facts.snapshot.spawn_edges == ()
    assert len(facts.headers) == len(facts.rollouts) == 1
    assert facts.rollouts[0].metadata_spawn_parent is None
    assert facts.rollouts[0].metadata_guardian_parent == _id(1)
    assert facts.rollouts[0].stored_bytes == 128 * 1024**2
    assert len(facts.headers[0].decoded_header_sha256) == 32  # noqa: PLR2004
    assert facts.headers[0].identity[2] == 128 * 1024**2
    selected = select_closed_work(facts.snapshot, _CUTOFF, frozenset({_id(3)}))
    assert selected.conditional_candidates == frozenset({_id(3)})
    assert selected.guardian_associations == facts.snapshot.guardian_associations


def test_guardian_parent_deletion_does_not_destroy_historical_association(tmp_path: Path) -> None:
    _guardian_case(tmp_path)
    home = tmp_path / "home"
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("DELETE FROM threads WHERE id=?", (str(_id(1).value),))
    facts = read_closed_work_facts(home, _CUTOFF, frozenset({_id(3)}), None, collection_limits())
    assert {record.id for record in facts.snapshot.threads} == {_id(3)}
    assert facts.snapshot.guardian_associations == (GuardianAssociation(_id(3), _id(1)),)
    assert select_closed_work(
        facts.snapshot, _CUTOFF, frozenset({_id(3)})
    ).conditional_candidates == frozenset({_id(3)})


def test_parent_selection_does_not_read_or_select_associated_guardian(tmp_path: Path) -> None:
    path = _guardian_case(tmp_path)
    path.unlink()
    facts = read_closed_work_facts(
        tmp_path / "home", _CUTOFF, frozenset({_id(1)}), None, collection_limits()
    )
    assert _id(3) not in {record.id for record in facts.snapshot.threads}
    assert facts.snapshot.guardian_associations == ()
    assert {item.thread_id for item in facts.rollouts} == {_id(1), _id(2)}


@pytest.mark.parametrize("parent", [None, "malformed", "00000000-0000-0000-0000-000000000003"])
def test_selected_guardian_invalid_association_refuses(tmp_path: Path, parent: str | None) -> None:
    path = _guardian_case(tmp_path)
    metadata = json.loads(path.read_text())
    metadata["payload"]["parent_thread_id"] = parent
    path.write_text(json.dumps(metadata) + "\n")
    with pytest.raises(PreparationRefused, match="historical parent association"):
        read_closed_work_facts(
            tmp_path / "home", _CUTOFF, frozenset({_id(3)}), None, collection_limits()
        )


def test_selected_guardian_sql_header_source_mismatch_refuses(tmp_path: Path) -> None:
    path = _guardian_case(tmp_path)
    metadata = json.loads(path.read_text())
    metadata["payload"]["source"] = "cli"
    path.write_text(json.dumps(metadata) + "\n")
    with pytest.raises(PreparationRefused, match="SQL/header source disagrees"):
        read_closed_work_facts(
            tmp_path / "home", _CUTOFF, frozenset({_id(3)}), None, collection_limits()
        )


def test_unselected_guardian_does_not_invent_association_from_sql(tmp_path: Path) -> None:
    path = _guardian_case(tmp_path)
    path.unlink()
    facts = read_closed_work_facts(
        tmp_path / "home", _CUTOFF, frozenset({_id(3)}), frozenset(), collection_limits()
    )
    assert facts.snapshot.threads[0].origin is ThreadOrigin.GUARDIAN
    assert facts.snapshot.guardian_associations == ()
    assert facts.headers == ()
