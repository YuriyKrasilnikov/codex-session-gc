# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from codex_session_gc.cli.console_tasks import preview_selection
from codex_session_gc.domain import Attachment, InvalidRecordError, ThreadId
from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
from codex_session_gc.execution.closed_work_request import ClosedWorkFacts, PreparationRefused
from codex_session_gc.planning.closed_work import ClosedWorkReason, select_closed_work
from tests.current_storage_fixture import _CUTOFF, _LIMITS, _fixture, _id

if TYPE_CHECKING:
    from pathlib import Path


def _header(path: Path, **values: object) -> None:
    line = json.loads(path.read_text())
    line["payload"].update(values)
    path.write_text(json.dumps(line) + "\n")


def _facts(home: Path, selected: frozenset[ThreadId] | None = None) -> ClosedWorkFacts:
    return read_closed_work_facts(home, _CUTOFF, frozenset({_id(1)}), selected, _LIMITS)


def test_typed_attachment_rejects_self_and_other_identity_types() -> None:
    with pytest.raises(InvalidRecordError):
        Attachment(_id(1), _id(1))
    with pytest.raises(InvalidRecordError):
        Attachment(_id(1), "invalid")  # type: ignore[arg-type]


def test_attached_child_protects_parent_even_when_both_selected(tmp_path: Path) -> None:
    facts, _, _ = _fixture(tmp_path)
    child, parent = _id(2), _id(1)
    graph = replace(facts.snapshot, attachments=(Attachment(child, parent),))
    result = select_closed_work(graph, _CUTOFF, frozenset({parent}))
    assert result.conditional_candidates == frozenset({child})
    assert any(
        item.thread_id == parent
        and item.reason is ClosedWorkReason.ATTACHED_THREAD
        and item.witness == child
        for item in result.retained
    )
    child_only = select_closed_work(
        graph, _CUTOFF, frozenset({parent}), selected_threads=frozenset({child})
    )
    assert child_only.conditional_candidates == frozenset({child})


def test_header_only_orphan_protects_source(tmp_path: Path) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    child = _id(3)
    path = next((home / "sessions").glob(f"*{child.value}.jsonl"))
    _header(path, attached_thread=True, forked_from_id=str(_id(1).value))
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("DELETE FROM threads WHERE id=?", (str(child.value),))
    facts = _facts(home)
    result = select_closed_work(
        facts.snapshot, _CUTOFF, frozenset({_id(1)}), physical_inventory=facts.physical_inventory
    )
    assert _id(1) not in result.conditional_candidates
    assert any(
        item.witness == child and item.reason is ClosedWorkReason.ATTACHED_THREAD
        for item in result.retained
    )


@pytest.mark.parametrize(("flag", "parent"), [(True, None), ("true", 1), (True, 2), (True, 1)])
def test_invalid_or_disagreeing_selected_header_refuses(
    tmp_path: Path, flag: object, parent: int | None
) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    child = _id(2)
    path = next((home / "sessions").glob(f"*{child.value}.jsonl"))
    _header(
        path,
        attached_thread=flag,
        forked_from_id=None if parent is None else str(_id(parent).value),
    )
    with pytest.raises(PreparationRefused):
        _facts(home, frozenset({child}))


def test_sql_only_incoming_edge_protects_source(tmp_path: Path) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute(
            "UPDATE threads SET attached_thread_parent_id=? WHERE id=?",
            (str(_id(1).value), str(_id(3).value)),
        )
    facts = _facts(home)
    result = select_closed_work(facts.snapshot, _CUTOFF, frozenset({_id(1)}))
    assert any(
        item.reason is ClosedWorkReason.ATTACHED_THREAD and item.witness == _id(3)
        for item in result.retained
    )


def test_ordinary_fork_is_not_attachment(tmp_path: Path) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    path = next((home / "sessions").glob(f"*{_id(3).value}.jsonl"))
    _header(path, forked_from_id=str(_id(1).value))
    facts = _facts(home)
    result = select_closed_work(facts.snapshot, _CUTOFF, frozenset({_id(1)}))
    assert _id(1) in result.conditional_candidates


def test_preview_exposes_attached_child_witness(tmp_path: Path) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute(
            "UPDATE threads SET attached_thread_parent_id=? WHERE id=?",
            (str(_id(1).value), str(_id(3).value)),
        )
    preview = preview_selection(home, _CUTOFF, frozenset({_id(1)}), None)
    assert {
        "id": str(_id(1).value),
        "reason": "attached_thread_exists",
        "witness": str(_id(3).value),
    } in preview["retained"]


def test_selected_attached_child_remains_eligible(tmp_path: Path) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    child, parent = _id(2), _id(1)
    path = next((home / "sessions").glob(f"*{child.value}.jsonl"))
    _header(path, attached_thread=True, forked_from_id=str(parent.value))
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute(
            "UPDATE threads SET attached_thread_parent_id=? WHERE id=?",
            (str(parent.value), str(child.value)),
        )
    facts = _facts(home, frozenset({child}))
    result = select_closed_work(
        facts.snapshot,
        _CUTOFF,
        frozenset({parent}),
        selected_threads=frozenset({child}),
        physical_inventory=facts.physical_inventory,
    )
    assert result.conditional_candidates == frozenset({child})


def test_preview_guidance_lists_all_selected_source_blockers(tmp_path: Path) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.executemany(
            "UPDATE threads SET attached_thread_parent_id=? WHERE id=?",
            [(str(_id(1).value), str(_id(i).value)) for i in (2, 3)],
        )
    # Do not select the children: their headers are intentionally not promoted to authority.
    report = preview_selection(home, _CUTOFF, frozenset({_id(1)}), frozenset({_id(1)}))
    guidance = report["attachment_cleanup"]
    assert guidance["home_identity"] == report["home_identity"]
    assert len(guidance["sources"]) == 1
    source = guidance["sources"][0]
    assert source["id"] == str(_id(1).value)
    assert {child["id"] for child in source["children"]} == {str(_id(2).value), str(_id(3).value)}
    assert all(not child["selected"] for child in source["children"])


def test_implicit_selection_does_not_create_guidance(tmp_path: Path) -> None:
    _fixture(tmp_path)
    home = tmp_path / "home"
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute(
            "UPDATE threads SET attached_thread_parent_id=? WHERE id=?",
            (str(_id(1).value), str(_id(3).value)),
        )
    report = preview_selection(home, _CUTOFF, frozenset({_id(1)}), None)
    assert report["attachment_cleanup"]["sources"] == []


def test_cross_root_guidance_and_conflicting_spawn_source(tmp_path: Path) -> None:
    from tests.mcp_native_fixture import (  # noqa: PLC0415
        CHILD,
        OTHER,
        ROOT,
        attach_history,
        make_home,
    )

    home = tmp_path / "home"
    paths = make_home(home)
    attach_history(paths, OTHER, CHILD, standalone=True)
    source = ThreadId.parse(CHILD)
    report = preview_selection(
        home, _CUTOFF, frozenset({ThreadId.parse(ROOT)}), frozenset({source})
    )
    entry = report["attachment_cleanup"]["sources"][0]
    assert entry["root_id"] == ROOT
    assert entry["children"][0]["root_id"] == OTHER
    # SQL and selected header disagree about spawn provenance; guidance must not
    # infer an executable root even though the ordinary selection remains conditional.
    lines = paths[CHILD].read_text().splitlines()
    header = json.loads(lines[0])
    header["payload"]["source"] = "cli"
    lines[0] = json.dumps(header)
    paths[CHILD].write_text("\n".join(lines) + "\n")
    report = preview_selection(
        home, _CUTOFF, frozenset({ThreadId.parse(ROOT)}), frozenset({source})
    )
    assert report["attachment_cleanup"]["sources"][0]["root_id"] is None
    header["payload"]["source"] = {
        "subagent": {"thread_spawn": {"parent_thread_id": ROOT, "depth": 1}}
    }
    header["payload"]["parent_thread_id"] = OTHER
    lines[0] = json.dumps(header)
    paths[CHILD].write_text("\n".join(lines) + "\n")
    report = preview_selection(
        home, _CUTOFF, frozenset({ThreadId.parse(ROOT)}), frozenset({source})
    )
    assert report["attachment_cleanup"]["sources"][0]["root_id"] is None


def test_unrelated_large_spawn_scope_does_not_truncate_attachment_guidance(tmp_path: Path) -> None:
    from tests.mcp_native_fixture import (  # noqa: PLC0415
        CHILD,
        OTHER,
        ROOT,
        attach_history,
        make_home,
    )

    home = tmp_path / "home"
    paths = make_home(home)
    attach_history(paths, OTHER, CHILD, standalone=True)
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.executemany(
            "INSERT INTO thread_spawn_edges VALUES(?,?,'closed')",
            [(ROOT, str(UUID(int=i))) for i in range(1, 1010)],
        )
    report = preview_selection(
        home,
        _CUTOFF,
        frozenset({ThreadId.parse(ROOT)}),
        frozenset({ThreadId.parse(CHILD)}),
    )
    guidance = report["attachment_cleanup"]
    assert guidance["truncated"] is False
    assert guidance["sources"][0]["root_id"] == ROOT
    assert guidance["sources"][0]["children"][0]["root_id"] == OTHER


def test_selected_attachment_tracks_current_relationship_only(tmp_path: Path) -> None:
    from tests.mcp_native_fixture import (  # noqa: PLC0415
        CHILD,
        OTHER,
        ROOT,
        attach_history,
        make_home,
    )

    home = tmp_path / "home"
    paths = make_home(home)
    attach_history(paths, CHILD, ROOT)
    root = ThreadId.parse(ROOT)
    child = ThreadId.parse(CHILD)

    def report(selected: frozenset[ThreadId] | None) -> dict[str, object]:
        return preview_selection(home, _CUTOFF, frozenset({root}), selected)

    assert report(frozenset({child}))["selected_attachments"] == {CHILD: ROOT}
    assert report(frozenset({root}))["selected_attachments"] == {}
    assert report(None)["selected_attachments"] == {}
    attach_history(paths, CHILD, OTHER)
    assert report(frozenset({child}))["selected_attachments"] == {CHILD: OTHER}
    lines = paths[CHILD].read_text().splitlines()
    header = json.loads(lines[0])
    header["payload"].pop("attached_thread")
    header["payload"].pop("forked_from_id")
    lines[0] = json.dumps(header)
    paths[CHILD].write_text("\n".join(lines) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as db:
        db.execute("UPDATE threads SET attached_thread_parent_id=NULL WHERE id=?", (CHILD,))
    assert report(frozenset({child}))["selected_attachments"] == {}
