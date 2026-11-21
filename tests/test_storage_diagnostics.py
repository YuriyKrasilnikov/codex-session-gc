# Copyright (c) 2026
# ruff: noqa: S101, D103, PT011, PT018, PLR2004, S108, PLC0415, ARG001, TRY003, EM101, ANN202

"""Diagnostic observations retain facts without treating gaps as absence."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.domain import RolloutId, ThreadId
from codex_session_gc.inventory.diagnostic_sql import DiagnosticLimits, SqlEdge, SqlFacts, SqlThread
from codex_session_gc.inventory.storage import observe_storage
from codex_session_gc.inventory.storage_diagnostics import _project, observe_diagnostics
from codex_session_gc.inventory.storage_walk import StorageScanCancelled, StorageScanError
from tests.current_storage_fixture import _A, _B, _C, _catalogue_home

if TYPE_CHECKING:
    from pathlib import Path


def test_sql_relations_cycle_tail_and_dangling(tmp_path: Path) -> None:
    database = _catalogue_home(tmp_path)
    absent = "44444444-4444-4444-8444-444444444444"
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM thread_spawn_edges")
        connection.executemany(
            "INSERT INTO thread_spawn_edges VALUES (?,?,?)",
            [
                (_A, _A, "active"),
                (_A, _B, "invalid"),
                (absent, _C, "active"),
            ],
        )
        connection.execute(
            "UPDATE threads SET rollout_path=? WHERE id=?", (str(tmp_path / "other"), _A)
        )
    snapshot = observe_storage(tmp_path)
    result = observe_diagnostics(tmp_path, snapshot)
    items = result.subjects()["items"]
    by_id = {s["thread_id"]: s for s in items if s["kind"] != "physical"}
    assert "spawn_cycle" in by_id[_A]["reasons"]
    assert "spawn_cycle" not in by_id[_B]["reasons"]
    assert "blocked_by" in by_id[_B]["reasons"]
    assert "invalid_status" in by_id[_B]["reasons"]
    assert result.summary()["coverage"]["spawn_status"] == "partial"
    assert by_id[absent]["kind"] == "endpoint"
    assert "missing_parent" in by_id[absent]["reasons"]
    assert "blocked_by" in by_id[_C]["reasons"]
    physical = next(s for s in items if s["kind"] == "physical" and s["thread_id"] == _A)
    assert physical["sql_owner"] == "exists"
    assert physical["current_pointer"] == "different"
    incoming = result.explain(by_id[_A]["subject_id"], direction="incoming")
    outgoing = result.explain(by_id[_A]["subject_id"], direction="outgoing")
    all_edges = result.explain(by_id[_A]["subject_id"], direction="all")
    assert len(all_edges["items"]) < len(incoming["items"]) + len(outgoing["items"])
    assert result.lookup(rollout_id=RolloutId.parse(_A))["status"] == "observed"
    assert result.lookup(thread_id=ThreadId.parse(absent))["status"] == "observed"
    assert result.subjects(reason="missing_parent")["total_matching"] == 1
    with pytest.raises(ValueError):
        result.explain("diag/not-this-observation/thread/1")
    with pytest.raises(ValueError):
        result.subjects(limit=True)


def test_partial_owner_observation_never_proves_missing(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    snapshot = observe_storage(tmp_path)
    result = observe_diagnostics(tmp_path, snapshot, limits=DiagnosticLimits(max_rows=1))
    assert result.summary()["coverage"]["owners"] == "partial"
    unseen = [
        s for s in result.subjects()["items"] if s["kind"] == "physical" and s["thread_id"] != _A
    ]
    assert unseen and all(s["sql_owner"] == "unknown" for s in unseen)
    assert result.lookup(thread_id=ThreadId.parse(_C))["coverage"]["owners"] == "partial"


def test_pages_remain_detached_and_sql_only_is_searchable(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    (tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{_A}.jsonl").unlink()
    snapshot = observe_storage(tmp_path)
    result = observe_diagnostics(tmp_path, snapshot)
    (tmp_path / "state_5.sqlite").unlink()
    page = result.subjects(reason="sql_only", limit=1)
    assert page["total_matching"] == 1
    assert page["items"][0]["thread_id"] == _A
    assert result.explain(page["items"][0]["subject_id"])["subject"]["thread_id"] == _A
    assert result.subjects(reason="sql_only", offset=1)["items"] == []


def test_limits_ids_and_sql_strings_are_not_exposed(tmp_path: Path) -> None:
    database = _catalogue_home(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE threads SET rollout_path=? WHERE id=?", ("private-marker" * 1000, _A)
        )
        connection.execute(
            "UPDATE thread_spawn_edges SET parent_thread_id=?", ("bad-private-marker",)
        )
    snapshot = observe_storage(tmp_path)
    result = observe_diagnostics(tmp_path, snapshot)
    assert result.summary()["coverage"]["pointers"] == "partial"
    assert result.summary()["coverage"]["edges"] == "partial"
    assert result.summary()["counts"]["invalid_endpoints"] == 1
    assert "private-marker" not in repr(result.summary()) + repr(result.subjects())
    physical = next(
        s for s in result.subjects()["items"] if s["kind"] == "physical" and s["thread_id"] == _A
    )
    assert physical["current_pointer"] == "unknown"


def test_subject_and_evidence_limits_are_separate(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    result = observe_diagnostics(
        tmp_path, observe_storage(tmp_path), limits=DiagnosticLimits(max_subjects=2)
    )
    assert result.summary()["coverage"]["subjects"] == "partial"
    assert result.summary()["coverage"]["evidence"] == "partial"
    assert result.subjects()["total_matching"] == 2
    limited = observe_diagnostics(
        tmp_path, observe_storage(tmp_path), limits=DiagnosticLimits(max_evidence=1)
    )
    assert limited.summary()["coverage"]["evidence"] == "partial"
    assert limited.summary()["counts"]["evidence"] == 1
    assert limited.summary()["truncation"]["evidence_limit_reached"]


def test_root_mismatch_and_cancel_are_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    snapshot = observe_storage(home)
    with pytest.raises(StorageScanError):
        observe_diagnostics(other, snapshot)
    with pytest.raises(StorageScanCancelled):
        observe_diagnostics(home, snapshot, cancelled=lambda: True)


def test_alias_and_duplicate_rollout_candidates(tmp_path: Path) -> None:
    database = _catalogue_home(tmp_path)
    original = tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{_A}.jsonl"
    compressed = original.with_name(original.name + ".zst")
    original.unlink()
    compressed.write_bytes(b"not read")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(original), _A))
    snapshot = observe_storage(tmp_path)
    result = observe_diagnostics(tmp_path, snapshot)
    one = result.lookup(rollout_id=RolloutId.parse(_A))
    assert one["status"] == "observed"
    assert one["subject"]["current_pointer"] == "current"
    original.write_bytes(b"also not read")
    second = observe_diagnostics(tmp_path, observe_storage(tmp_path))
    ambiguous = second.lookup(rollout_id=RolloutId.parse(_A), limit=1)
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["total_matching"] == 2
    assert ambiguous["next_offset"] == 1
    thread = second.lookup(thread_id=ThreadId.parse(_A))
    assert thread["status"] == "observed"
    assert len([e for e in thread["items"] if e["kind"] == "filename_owner"]) == 2


def test_full_names_distinguish_truncated_display_paths(tmp_path: Path) -> None:
    database = _catalogue_home(tmp_path)
    old = tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{_A}.jsonl"
    old.unlink()
    prefix = tmp_path / "sessions" / ("x" * 180) / ("y" * 180) / ("z" * 180)
    paths = [prefix / suffix / old.name for suffix in ("a", "b")]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not read")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(paths[0]), _A))
    snapshot = observe_storage(tmp_path)
    rows = [r for r in snapshot.page()["items"] if r["filename_thread_id"] == _A]
    assert len(rows) == 2
    assert rows[0]["display_path"] == rows[1]["display_path"]
    assert all(r["path_truncated"] for r in rows)
    observation = observe_diagnostics(tmp_path, snapshot)
    subjects = [
        s
        for s in observation.subjects()["items"]
        if s["kind"] == "physical" and s["thread_id"] == _A
    ]
    assert {s["current_pointer"] for s in subjects} == {"current", "different"}


def test_graph_projection_preserves_multiple_parents_and_duplicate_witnesses(
    tmp_path: Path,
) -> None:
    _catalogue_home(tmp_path)
    snapshot = observe_storage(tmp_path)
    threads = tuple(SqlThread(i, None, None, invalid_attachment=False) for i in (_A, _B, _C))
    edges = (SqlEdge(_A, _C, "active"), SqlEdge(_A, _C, "active"), SqlEdge(_B, _C, "closed"))
    observation = _project(
        tmp_path,
        snapshot,
        SqlFacts(threads, edges, "complete", "partial", "complete"),
        DiagnosticLimits(),
        lambda: False,
    )
    child = next(
        s for s in observation.subjects()["items"] if s["kind"] == "thread" and s["thread_id"] == _C
    )
    assert {"multiple_parents", "duplicate_edge"} <= set(child["reasons"])
    incoming = [
        e
        for e in observation.explain(child["subject_id"], direction="incoming")["items"]
        if e["kind"] == "spawn"
    ]
    assert len(incoming) == 2
    assert {e["status"] for e in incoming} == {"active", "closed"}


def test_ambiguous_rollout_survives_subject_truncation(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    plain = tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{_A}.jsonl"
    plain.with_name(plain.name + ".zst").write_bytes(b"not read")
    result = observe_diagnostics(
        tmp_path, observe_storage(tmp_path), limits=DiagnosticLimits(max_subjects=1)
    )
    page = result.lookup(rollout_id=RolloutId.parse(_A))
    assert page["status"] == "ambiguous"
    assert page["total_matching"] == 1
    assert page["coverage"]["subjects"] == "partial"
    assert result.summary()["truncation"]["subjects_omitted"] > 0
    assert result.summary()["truncation"]["physical_ordinals_omitted"] == [2, 5]


@pytest.mark.parametrize(
    "bad_path", ["relative/path", "/elsewhere/path", "/tmp/../x", "/tmp/x\x00y"]
)
def test_invalid_pointer_coverage_and_original_interval(tmp_path: Path, bad_path: str) -> None:
    database = _catalogue_home(tmp_path)
    snapshot = observe_storage(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (bad_path, _A))
    result = observe_diagnostics(tmp_path, snapshot)
    summary = result.summary()
    assert summary["coverage"]["pointers"] == "partial"
    assert summary["counts"]["invalid_pointers"] == 1
    explained = result.lookup(rollout_id=RolloutId.parse(_A))
    assert explained["subject"]["current_pointer"] == "unknown"
    assert "invalid_pointer" in explained["subject"]["reasons"]
    assert (
        explained["original_association_observation"]["coverage"]
        == snapshot.summary()["sql_coverage"]
    )
    assert (
        explained["original_association_observation"]["started_at"]
        == snapshot.summary()["sql_started_at"]
    )


def test_owner_and_edge_rows_share_budget(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    result = observe_diagnostics(
        tmp_path, observe_storage(tmp_path), limits=DiagnosticLimits(max_rows=3)
    )
    assert result.summary()["counts"]["sql_owners"] == 3
    assert result.summary()["counts"]["sql_edges"] == 0
    assert result.summary()["coverage"]["owners"] == "complete"
    assert result.summary()["coverage"]["edges"] == "unknown"
    assert result.summary()["sql_reason"] == "sql_row_limit"


def test_variable_output_budget_and_original_info_on_missing_lookup(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    result = observe_diagnostics(
        tmp_path, observe_storage(tmp_path), limits=DiagnosticLimits(max_bytes=150)
    )
    summary = result.summary()
    assert summary["truncation"]["serialized_variable_bytes"] <= 150
    assert summary["coverage"]["subjects"] == "partial"
    result_page = result.lookup(rollout_id=RolloutId.parse("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    assert result_page["status"] == "subject_not_observed"
    assert (
        result_page["original_association_observation"]
        == summary["original_association_observation"]
    )


def test_malformed_endpoint_preserves_other_role(tmp_path: Path) -> None:
    database = _catalogue_home(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE thread_spawn_edges SET parent_thread_id='not-a-uuid'")
    result = observe_diagnostics(tmp_path, observe_storage(tmp_path))
    page = result.lookup(thread_id=ThreadId.parse(_B))
    links = [e for e in page["items"] if e["kind"] == "spawn"]
    assert len(links) == 1
    assert links[0]["source"] is None
    assert links[0]["target"] == page["subject"]["subject_id"]
    assert links[0]["reason"] == "invalid_parent"
    assert "not-a-uuid" not in repr(page)


def test_diagnostics_reuses_source_without_rescan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_session_gc.inventory import storage_walk

    _catalogue_home(tmp_path)
    snapshot = observe_storage(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("diagnostics unexpectedly rescanned the tree")

    monkeypatch.setattr(storage_walk, "scan_storage", forbidden)
    result = observe_diagnostics(tmp_path, snapshot)
    assert result.summary()["coverage"]["owners"] == "complete"


def test_interrupted_edge_query_keeps_completed_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from codex_session_gc import sqlite_engine
    from codex_session_gc.inventory import diagnostic_sql

    database = _catalogue_home(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO thread_spawn_edges VALUES (?,?,'closed')", (_A, _C))
    snapshot = observe_storage(tmp_path)
    real_transaction = diagnostic_sql.read_transaction

    class InterruptEdges:
        def __init__(self, connection: object) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> object:
            return getattr(self.connection, name)

        def execute(self, query: str, args: object = ()) -> object:
            result = self.connection.execute(query, args)
            if "FROM thread_spawn_edges LIMIT" not in query:
                return result

            def interrupted():
                yield next(iter(result))
                raise sqlite_engine.Error("interrupted")

            return interrupted()

    @contextmanager
    def transaction(*args: object, **kwargs: object):
        with real_transaction(*args, **kwargs) as connection:
            yield InterruptEdges(connection)

    monkeypatch.setattr(diagnostic_sql, "read_transaction", transaction)
    summary = observe_diagnostics(tmp_path, snapshot).summary()
    assert summary["coverage"]["owners"] == "complete"
    assert summary["coverage"]["pointers"] == "complete"
    assert summary["coverage"]["edges"] == "partial"
    assert summary["coverage"]["spawn_status"] == "partial"
    assert summary["counts"]["sql_edges"] == 1


def test_malformed_child_preserves_parent_role(tmp_path: Path) -> None:
    database = _catalogue_home(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE thread_spawn_edges SET child_thread_id='bad-private-child'")
    result = observe_diagnostics(tmp_path, observe_storage(tmp_path))
    page = result.lookup(thread_id=ThreadId.parse(_A))
    links = [e for e in page["items"] if e["kind"] == "spawn"]
    assert len(links) == 1
    assert links[0]["source"] == page["subject"]["subject_id"]
    assert links[0]["target"] is None
    assert links[0]["reason"] == "invalid_child"
    assert "bad-private-child" not in repr(page)


@pytest.mark.parametrize(
    ("invalid", "missing", "identity"),
    [("parent", "child", _B), ("child", "parent", _A)],
)
def test_malformed_end_preserves_missing_opposite_end(
    tmp_path: Path, invalid: str, missing: str, identity: str
) -> None:
    database = _catalogue_home(tmp_path)
    with sqlite3.connect(database) as connection:
        if invalid == "parent":
            connection.execute("UPDATE thread_spawn_edges SET parent_thread_id='bad-private-id'")
        else:
            connection.execute("UPDATE thread_spawn_edges SET child_thread_id='bad-private-id'")
        connection.execute("DELETE FROM threads WHERE id=?", (identity,))
    result = observe_diagnostics(tmp_path, observe_storage(tmp_path))
    reason = f"missing_{missing}"
    (subject,) = result.subjects(reason=reason)["items"]
    assert subject["thread_id"] == identity
    assert subject["sql_owner"] == "missing"
    page = result.explain(subject["subject_id"])
    (dangling,) = [record for record in page["items"] if record["kind"] == "dangling_endpoint"]
    assert dangling["reason"] == reason
    assert dangling["source" if invalid == "parent" else "target"] is None
