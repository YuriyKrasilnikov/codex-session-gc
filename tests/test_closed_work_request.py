# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004, SLF001

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

import pytest

from codex_session_gc.domain import (
    ArchiveState,
    RolloutMetadata,
)
from codex_session_gc.execution import closed_work_request as adapter
from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
from codex_session_gc.execution.closed_work_request import (
    DatabaseKind,
    PreparationRefused,
)
from codex_session_gc.execution.parent_relation import parse_parent_relation
from codex_session_gc.inventory.observation import RolloutObservation
from codex_session_gc.planning.closed_work import select_closed_work
from tests.current_storage_fixture import (
    _CUTOFF,
    _DDL,
    _LIMITS,
    _before,
    _fixture,
    _guardian_fixture,
    _id,
    _prepare,
    _refresh_physical,
)
from tests.test_closed_work_facts import _independent_work

# Deliberately synthetic schemas: these tests qualify collection semantics, not
# native production schemas, helper artifacts or closure.


def test_v6_exact_scope_targets_rows_and_immutable_bytes(tmp_path: Path) -> None:
    observation, selection, helper = _fixture(tmp_path)
    before = _before(tmp_path / "home")
    payload = _prepare(tmp_path, observation, selection, helper)
    value = json.loads(payload.data)
    assert payload.version == 1
    assert value["version"] == 6
    assert value["provider"] == "store-default"
    assert {target["provider"] for target in value["targets"]} == {"openai"}
    assert value["ownership_scope"]["thread_ids"] == [str(_id(i).value) for i in (1, 2)]
    assert value["deletion_scope"] == value["ownership_scope"]["thread_ids"]
    assert {target["thread_id"] for target in value["targets"]} == set(value["deletion_scope"])
    for target in value["targets"]:
        physical = target["rollouts"][0]
        path = Path(physical["rollout_path"])
        assert physical["header_sha256"] == list(
            sha256(path.read_bytes().splitlines(keepends=True)[0]).digest()
        )
        assert physical["physical_identity"]["size"] == path.stat().st_size
        assert "rollout_sha256" not in target
    bounds = {Path(bound["path"]).name: bound for bound in value["databases"]}
    assert len(bounds) == len(DatabaseKind)
    assert bounds[DatabaseKind.BOARD]["board_tombstone"] == str(_id(1).value)
    assert bounds[DatabaseKind.BOARD]["rows"]["posts"] == [1]
    assert bounds[DatabaseKind.BOARD]["rows"]["board_templates"] == [1]
    assert bounds[DatabaseKind.GOALS]["rows"]["thread_goal_continuation_deferrals"] == [1, 2]
    assert bounds[DatabaseKind.MEMORY]["rows"]["jobs"] == [1, 3]
    assert bounds[DatabaseKind.QUEUE]["queue_bookkeeping"] is True
    assert bounds[DatabaseKind.QUEUE]["rows"]["sqlite_sequence"] == [1]
    assert _before(tmp_path / "home") == before


def test_target_observation_lookup_grows_linearly_and_preserves_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RolloutObservation.__getattribute__
    accesses = 0

    def counted(item: RolloutObservation, name: str) -> object:
        nonlocal accesses
        accesses += name == "thread_id"
        return original(item, name)

    measurements = []
    for count in (100, 200):
        home, owners = _independent_work(tmp_path / str(count), count)
        facts = read_closed_work_facts(home, _CUTOFF, owners, owners, _LIMITS)
        selection = select_closed_work(
            facts.snapshot, _CUTOFF, owners, physical_inventory=facts.physical_inventory
        )
        before = _before(home)
        expected = adapter.review_targets(facts, selection, home, _LIMITS)
        with monkeypatch.context() as patch:
            patch.setattr(RolloutObservation, "__getattribute__", counted)
            accesses = 0
            observed = adapter.review_targets(facts, selection, home, _LIMITS)
        measurements.append(accesses)
        assert observed == expected
        assert {target.thread_id for target in observed} == {str(owner.value) for owner in owners}
        assert _before(home) == before
    assert measurements[1] < measurements[0] * 3


def test_target_index_keeps_first_matching_observation(tmp_path: Path) -> None:
    facts, selection, _ = _fixture(tmp_path)
    home = tmp_path / "home"
    expected = adapter.review_targets(facts, selection, home, _LIMITS)
    first = facts.rollouts[0]
    duplicate = replace(first, metadata_spawn_parent=_id(3), metadata_origin="subagent")
    repeated = replace(facts, rollouts=(*facts.rollouts, duplicate))
    assert adapter.review_targets(repeated, selection, home, _LIMITS) == expected


def test_missing_current_observation_refuses_before_request(tmp_path: Path) -> None:
    facts, selection, _ = _fixture(tmp_path)
    incomplete = replace(
        facts, rollouts=tuple(item for item in facts.rollouts if item.thread_id != _id(2))
    )
    with pytest.raises(PreparationRefused, match="current rollout observation is missing"):
        adapter.review_targets(incomplete, selection, tmp_path / "home", _LIMITS)


def test_guardian_guards_index_observed_objects_without_extending_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RolloutMetadata.__getattribute__
    accesses = 0

    def counted(item: RolloutMetadata, name: str) -> object:
        nonlocal accesses
        accesses += name == "ownership"
        return original(item, name)

    measurements = []
    for count in (100, 200):
        home, owners = _independent_work(tmp_path / str(count), count)
        expected_identities = {}
        guardian_source = {"subagent": {"other": "guardian"}}
        with sqlite3.connect(home / DatabaseKind.STATE) as connection:
            for owner in owners:
                path = home / "sessions" / f"rollout-2026-06-01T00-00-00-{owner.value}.jsonl"
                metadata = json.loads(path.read_text())
                metadata["payload"].update(
                    source=guardian_source, parent_thread_id=str(_id(1).value)
                )
                path.write_text(json.dumps(metadata) + "\n")
                connection.execute(
                    "UPDATE threads SET source=? WHERE id=?",
                    (json.dumps(guardian_source), str(owner.value)),
                )
                stat = path.stat()
                expected_identities[path] = (
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
        facts = read_closed_work_facts(home, _CUTOFF, frozenset({_id(1)}), None, _LIMITS)
        before = _before(home)
        budget = adapter.CollectionBudget(_LIMITS, time.monotonic() + _LIMITS.deadline_seconds)
        with adapter._read_transaction(home / DatabaseKind.STATE, budget) as connection:
            connection.row_factory = sqlite3.Row
            with monkeypatch.context() as patch:
                patch.setattr(RolloutMetadata, "__getattribute__", counted)
                accesses = 0
                guards, identities = adapter._guardian_guards(
                    connection, (str(_id(1).value),), home, budget, facts.physical_inventory
                )
        measurements.append(accesses)
        assert identities == expected_identities
        assert guards == {str(home / f"thread-writer-locks/{owner.value}.lock") for owner in owners}
        assert _before(home) == before
    assert measurements[1] < measurements[0] * 3


def test_child_subset_preserves_board_and_other_scope_members(tmp_path: Path) -> None:
    observation, _, helper = _fixture(tmp_path)
    assert observation.snapshot is not None
    selection = select_closed_work(
        observation.snapshot,
        _CUTOFF,
        frozenset({_id(1)}),
        selected_threads=frozenset({_id(2)}),
    )
    value = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    assert value["deletion_scope"] == [str(_id(2).value)]
    board = next(
        bound for bound in value["databases"] if Path(bound["path"]).name == DatabaseKind.BOARD
    )
    assert board["board_tombstone"] is None
    assert all(rows == [] for rows in board["rows"].values())
    assert len(value["ownership_scope"]["thread_ids"]) == 2


def test_absent_board_is_omitted_without_creation(tmp_path: Path) -> None:
    observation, selection, helper = _fixture(tmp_path)
    board = tmp_path / "home" / DatabaseKind.BOARD
    board.unlink()
    value = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    assert all(Path(bound["path"]).name != DatabaseKind.BOARD for bound in value["databases"])
    assert not board.exists()


@pytest.mark.parametrize(
    "mutation",
    ["activity", "archive", "schema", "migration", "paginated", "symlink", "missing_state"],
)
def test_unqualified_or_changed_input_refuses(tmp_path: Path, mutation: str) -> None:
    observation, selection, helper = _fixture(tmp_path)
    state = tmp_path / "home" / DatabaseKind.STATE
    if mutation == "symlink":
        state.rename(state.with_suffix(".saved"))
        state.symlink_to(state.with_suffix(".saved"))
    elif mutation == "missing_state":
        state.unlink()
    else:
        with sqlite3.connect(state) as connection:
            if mutation == "activity":
                connection.execute(
                    "UPDATE threads SET updated_at_ms=updated_at_ms+1 WHERE id=?",
                    (str(_id(2).value),),
                )
            elif mutation == "archive":
                connection.execute("UPDATE threads SET archived=1 WHERE id=?", (str(_id(2).value),))
            elif mutation == "schema":
                connection.execute("ALTER TABLE threads ADD COLUMN unexpected TEXT")
            elif mutation == "migration":
                connection.execute("UPDATE _sqlx_migrations SET checksum=?", (b"wrong",))
            else:
                connection.execute("UPDATE threads SET history_mode='paginated'")
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper)


@pytest.mark.parametrize(
    "field",
    [
        "maximum_database_bytes",
        "maximum_rollout_bytes",
        "maximum_total_bytes",
        "maximum_header_bytes",
        "maximum_request_bytes",
    ],
)
def test_explicit_bounds_do_not_expand(tmp_path: Path, field: str) -> None:
    observation, selection, helper = _fixture(tmp_path)
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper, replace(_LIMITS, **{field: 1}))


def test_wal_committed_state_is_captured_in_read_transaction(tmp_path: Path) -> None:
    observation, selection, helper = _fixture(tmp_path)
    state = tmp_path / "home" / DatabaseKind.STATE
    with sqlite3.connect(state) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE threads SET agent_role=? WHERE id=?", ("updated-role", str(_id(2).value))
        )
        writer.commit()
        child_path = next(item.path for item in observation.rollouts if item.thread_id == _id(2))
        envelope = json.loads(child_path.read_bytes())
        envelope["payload"]["agent_role"] = "updated-role"
        child_path.write_text(json.dumps(envelope) + "\n")
        observation = _refresh_physical(tmp_path, observation)
        before = _before(tmp_path / "home")
        value = json.loads(_prepare(tmp_path, observation, selection, helper).data)
        child = next(item for item in value["targets"] if item["thread_id"] == str(_id(2).value))
        assert child["agent_role"] == "updated-role"
        after = _before(tmp_path / "home")
        assert {p: value for p, value in after.items() if not p.name.endswith("-shm")} == {
            p: value for p, value in before.items() if not p.name.endswith("-shm")
        }


def test_unrelated_sql_activity_does_not_require_whole_home_stability(tmp_path: Path) -> None:
    observation, selection, helper = _fixture(tmp_path)
    with sqlite3.connect(tmp_path / "home" / DatabaseKind.STATE) as connection:
        connection.execute(
            "UPDATE threads SET updated_at_ms=updated_at_ms+1000 WHERE id=?", (str(_id(3).value),)
        )
    value = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    assert value["deletion_scope"] == [str(_id(i).value) for i in (1, 2)]
    state = next(
        bound for bound in value["databases"] if Path(bound["path"]).name == DatabaseKind.STATE
    )
    assert state["rows"]["threads"] == [1, 2]


def test_known_archived_main_is_explicitly_selectable(tmp_path: Path) -> None:
    observation, _, helper = _fixture(tmp_path)
    snapshot = observation.snapshot
    assert snapshot is not None
    facts = []
    with sqlite3.connect(tmp_path / "home" / DatabaseKind.STATE) as connection:
        for fact in observation.rollouts:
            assert fact.thread_id is not None
            archived = tmp_path / "home" / "archived_sessions" / fact.path.name
            archived.parent.mkdir(exist_ok=True)
            fact.path.rename(archived)
            connection.execute(
                "UPDATE threads SET archived=1,rollout_path=? WHERE id=?",
                (str(archived), str(fact.thread_id.value)),
            )
            facts.append(replace(fact, path=archived, archive_state=ArchiveState.ARCHIVED))
    snapshot = replace(
        snapshot,
        threads=tuple(
            replace(item, archive_state=ArchiveState.ARCHIVED) for item in snapshot.threads
        ),
        rollouts=tuple(
            replace(item, archive_state=ArchiveState.ARCHIVED) for item in snapshot.rollouts
        ),
    )
    observation = replace(
        observation,
        snapshot=snapshot,
        rollouts=tuple(facts),
    )
    observation = _refresh_physical(tmp_path, observation)
    selection = select_closed_work(snapshot, _CUTOFF, frozenset({_id(1)}))
    value = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    assert all(target["archived"] is True for target in value["targets"])
    assert str(_id(1).value) in value["deletion_scope"]


def test_late_database_appearance_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observation, selection, helper = _fixture(tmp_path)
    board = tmp_path / "home" / DatabaseKind.BOARD
    board.unlink()
    read = adapter._read_transaction

    @contextmanager
    def concurrent_read(
        source: Path, budget: adapter.CollectionBudget
    ) -> Iterator[sqlite3.Connection]:
        with read(source, budget) as connection:
            yield connection
        if not board.exists():
            with sqlite3.connect(board) as connection:
                for statement in _DDL[DatabaseKind.BOARD]:
                    connection.execute(statement)

    monkeypatch.setattr(adapter, "_read_transaction", concurrent_read)
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper)


def test_late_selected_rollout_change_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, selection, helper = _fixture(tmp_path)
    selected = next(item.path for item in observation.rollouts if item.thread_id == _id(2))
    read = adapter._read_transaction

    @contextmanager
    def concurrent_read(
        source: Path, budget: adapter.CollectionBudget
    ) -> Iterator[sqlite3.Connection]:
        with read(source, budget) as connection:
            yield connection
        if source.name == DatabaseKind.HISTORY:
            with selected.open("ab") as stream:
                stream.write(b"\n")

    monkeypatch.setattr(adapter, "_read_transaction", concurrent_read)
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper)
    assert selected.read_bytes().endswith(b"\n\n")
    assert len(tuple((tmp_path / "home").glob("*.sqlite"))) == len(DatabaseKind)


@pytest.mark.parametrize(
    ("field", "value"),
    [("model_provider", None), ("model_provider", "different"), ("agent_role", "different")],
)
def test_header_provider_role_must_match_sql(tmp_path: Path, field: str, value: str | None) -> None:
    observation, selection, helper = _fixture(tmp_path)
    path = next(item.path for item in observation.rollouts if item.thread_id == _id(2))
    envelope = json.loads(path.read_bytes())
    if value is None:
        envelope["payload"].pop(field, None)
    else:
        envelope["payload"][field] = value
    path.write_text(json.dumps(envelope) + "\n")
    before = _before(tmp_path / "home")
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper)
    assert _before(tmp_path / "home") == before


def test_explicit_store_provider_is_required(tmp_path: Path) -> None:
    observation, selection, helper = _fixture(tmp_path)
    before = _before(tmp_path / "home")
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper, default_provider="")
    assert _before(tmp_path / "home") == before


@pytest.mark.parametrize(
    "mutation",
    ["directory", "extension", "identity", "timestamp", "missing_timestamp", "compressed_sibling"],
)
def test_native_path_profile_refuses(tmp_path: Path, mutation: str) -> None:
    observation, selection, helper = _fixture(tmp_path)
    fact = next(item for item in observation.rollouts if item.thread_id == _id(2))
    path = fact.path
    if mutation == "compressed_sibling":
        path.with_suffix(".jsonl.zst").write_bytes(b"not admitted")
    else:
        if mutation == "directory":
            target = path.parent.parent / "other" / path.name
        elif mutation == "extension":
            target = path.with_suffix(".data")
        elif mutation == "identity":
            target = path.with_name(path.name.replace(str(_id(2).value), str(_id(3).value)))
        elif mutation == "timestamp":
            target = path.with_name(path.name.replace("2026-06-01", "2026-99-01"))
        else:
            target = path.with_name(f"rollout-{_id(2).value}.jsonl")
        target.parent.mkdir(exist_ok=True)
        path.rename(target)
        with sqlite3.connect(tmp_path / "home" / DatabaseKind.STATE) as connection:
            connection.execute(
                "UPDATE threads SET rollout_path=? WHERE id=?", (str(target), str(_id(2).value))
            )
        observation = replace(
            observation,
            rollouts=tuple(
                replace(item, path=target) if item == fact else item
                for item in observation.rollouts
            ),
        )
    before = _before(tmp_path / "home")
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper)
    assert _before(tmp_path / "home") == before


def test_native_scalar_sql_and_structured_source_wire_values(tmp_path: Path) -> None:
    observation, selection, helper = _fixture(tmp_path)
    with sqlite3.connect(tmp_path / "home" / DatabaseKind.STATE) as connection:
        sources = dict(connection.execute("SELECT id,source FROM threads"))
    assert sources[str(_id(1).value)] == "cli"
    assert isinstance(json.loads(sources[str(_id(2).value)]), dict)
    targets = json.loads(_prepare(tmp_path, observation, selection, helper).data)["targets"]
    root = next(item for item in targets if item["thread_id"] == str(_id(1).value))
    child = next(item for item in targets if item["thread_id"] == str(_id(2).value))
    assert root["source"] == "cli"
    assert child["source"]["subagent"]["thread_spawn"]["parent_thread_id"] == str(_id(1).value)


@pytest.mark.parametrize(
    "source", ["unrecognized-native-source", '"unrecognized-native-source"', "null", "[]"]
)
def test_unqualified_scalar_sql_source_refuses(tmp_path: Path, source: str) -> None:
    observation, selection, helper = _fixture(tmp_path)
    with sqlite3.connect(tmp_path / "home" / DatabaseKind.STATE) as connection:
        connection.execute("UPDATE threads SET source=? WHERE id=?", (source, str(_id(1).value)))
    before = _before(tmp_path / "home")
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper)
    assert _before(tmp_path / "home") == before


def test_associated_retained_guardian_adds_only_its_exact_writer_guard(tmp_path: Path) -> None:
    observation, selection, helper = _fixture(tmp_path)
    before = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    path = _guardian_fixture(tmp_path)
    observation = _refresh_physical(tmp_path, observation)
    guardian_bytes = path.read_bytes()
    after = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    assert set(after["guard_paths"]) - set(before["guard_paths"]) == {
        str(tmp_path / "home" / f"thread-writer-locks/{_id(4).value}.lock")
    }
    assert after["targets"] == before["targets"]
    assert after["deletion_scope"] == before["deletion_scope"]
    assert after["ownership_scope"] == before["ownership_scope"]
    assert after["databases"] == before["databases"]
    assert path.read_bytes() == guardian_bytes
    assert not (tmp_path / "home" / "thread-writer-locks").exists()


@pytest.mark.parametrize(("parent", "kind"), [(3, "guardian"), (2, "reviewer")])
def test_unrelated_or_other_subagent_adds_no_guard(tmp_path: Path, parent: int, kind: str) -> None:
    observation, selection, helper = _fixture(tmp_path)
    before = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    _guardian_fixture(tmp_path, parent=parent, kind=kind)
    observation = _refresh_physical(tmp_path, observation)
    after = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    assert after["guard_paths"] == before["guard_paths"]


@pytest.mark.parametrize(
    "mutation",
    [
        "provider",
        "role",
        "owner",
        "source",
        "history",
        "base",
        "sql_history",
        "incoming",
        "archive",
    ],
)
def test_inconsistent_associated_guardian_refuses(tmp_path: Path, mutation: str) -> None:
    observation, selection, helper = _fixture(tmp_path)
    path = _guardian_fixture(tmp_path)
    observation = _refresh_physical(tmp_path, observation)
    envelope = json.loads(path.read_bytes())
    metadata = envelope["payload"]
    field_changes = {
        "provider": ("model_provider", "other"),
        "role": ("agent_role", "other"),
        "owner": ("id", str(_id(5).value)),
        "source": ("source", {"subagent": {"other": "unknown"}}),
        "history": ("history_mode", "paginated"),
        "base": ("history_base", str(_id(2).value)),
    }
    if mutation in field_changes:
        key, value = field_changes[mutation]
        metadata[key] = value
        path.write_text(json.dumps(envelope) + "\n")
    else:
        with sqlite3.connect(tmp_path / "home" / DatabaseKind.STATE) as connection:
            if mutation == "incoming":
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES(?,?,?)",
                    (str(_id(2).value), str(_id(4).value), "closed"),
                )
            elif mutation == "archive":
                connection.execute("UPDATE threads SET archived=1 WHERE id=?", (str(_id(4).value),))
            else:
                connection.execute(
                    "UPDATE threads SET history_mode='paginated' WHERE id=?", (str(_id(4).value),)
                )
    with pytest.raises(PreparationRefused):
        _prepare(tmp_path, observation, selection, helper)


def test_retained_guardian_large_body_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, selection, helper = _fixture(tmp_path)
    path = _guardian_fixture(tmp_path)
    with path.open("ab") as stream:
        stream.truncate(2 * 1024**3)
    observation = _refresh_physical(tmp_path, observation)
    real_read = adapter.os.pread
    total = 0

    def measured(fd: int, count: int, offset: int) -> bytes:
        nonlocal total
        data = real_read(fd, count, offset)
        total += len(data)
        return data

    monkeypatch.setattr(adapter.os, "pread", measured)
    _prepare(tmp_path, observation, selection, helper)
    assert total <= 3 * 4096


def test_guardian_target_has_separate_tagged_association_without_parent_closure(
    tmp_path: Path,
) -> None:
    observation, _, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    target = next(item.path for item in observation.rollouts if item.thread_id == _id(3))
    header = json.loads(target.read_text())
    source = {"subagent": {"other": "guardian"}}
    header["payload"].update(source=source, parent_thread_id=str(_id(1).value))
    target.write_text(json.dumps(header) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET source=? WHERE id=?", (json.dumps(source), str(_id(3).value))
        )
        for table in ("guardian_review_feedback", "thread_read_receipts"):
            connection.execute(
                f"INSERT INTO {table} VALUES(?,?)", (str(_id(3).value), "selected opaque")
            )
            connection.execute(
                f"INSERT INTO {table} VALUES(?,?)", (str(_id(1).value), "retained opaque")
            )
    facts = read_closed_work_facts(home, _CUTOFF, frozenset({_id(3)}), None, _LIMITS)
    selection = select_closed_work(facts.snapshot, _CUTOFF, frozenset({_id(3)}))
    before = _before(home)
    value = json.loads(_prepare(tmp_path, facts, selection, helper).data)
    assert value["version"] == 6
    assert value["ownership_scope"]["thread_ids"] == [str(_id(3).value)]
    assert value["targets"][0]["parent_relation"] == {
        "kind": "guardian_association",
        "associated_parent_thread_id": str(_id(1).value),
    }
    assert str(home / f"thread-writer-locks/{_id(1).value}.lock") not in value["guard_paths"]
    state = next(
        database for database in value["databases"] if database["path"].endswith("state_5.sqlite")
    )
    assert state["rows"]["guardian_review_feedback"] == [1]
    assert state["rows"]["thread_read_receipts"] == [1]
    assert _before(home) == before


def test_supplemental_rows_are_bound_for_selected_ordinary_owners(tmp_path: Path) -> None:
    facts, selection, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        for table in ("guardian_review_feedback", "thread_read_receipts"):
            connection.execute(
                f"INSERT INTO {table} VALUES(?,?)", (str(_id(2).value), "selected opaque")
            )
            connection.execute(
                f"INSERT INTO {table} VALUES(?,?)", (str(_id(3).value), "retained opaque")
            )
    value = json.loads(_prepare(tmp_path, facts, selection, helper).data)
    state = next(
        database for database in value["databases"] if database["path"].endswith("state_5.sqlite")
    )
    assert state["rows"]["guardian_review_feedback"] == [1]
    assert state["rows"]["thread_read_receipts"] == [1]
    assert {item["parent_relation"]["kind"] for item in value["targets"]} == {"root", "spawn"}


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "root", "parent_thread_id": "00000000-0000-0000-0000-000000000001"},
        {"kind": "guardian_association"},
        {"kind": "spawn", "associated_parent_thread_id": "00000000-0000-0000-0000-000000000001"},
        {"kind": "guardian_association", "associated_parent_thread_id": "malformed"},
        {"kind": "future_relation"},
    ],
)
def test_parent_relation_rejects_unknown_mixed_or_missing_variants(value: object) -> None:
    with pytest.raises(ValueError, match=r"relation|identifier"):
        parse_parent_relation(value)


@pytest.mark.parametrize("contradictory", [False, True])
def test_closed_sql_spawn_edge_allows_other_source_but_rejects_embedded_conflict(
    tmp_path: Path, *, contradictory: bool
) -> None:
    observation, selection, helper = _fixture(tmp_path)
    child = next(item.path for item in observation.rollouts if item.thread_id == _id(2))
    source = (
        {"subagent": {"thread_spawn": {"parent_thread_id": str(_id(3).value), "depth": 1}}}
        if contradictory
        else {"subagent": {"other": "fixture-worker"}}
    )
    header = json.loads(child.read_text())
    header["payload"]["source"] = source
    child.write_text(json.dumps(header) + "\n")
    with sqlite3.connect(tmp_path / "home" / DatabaseKind.STATE) as connection:
        connection.execute(
            "UPDATE threads SET source=? WHERE id=?", (json.dumps(source), str(_id(2).value))
        )
    observation = _refresh_physical(tmp_path, observation)
    before = _before(tmp_path / "home")
    if contradictory:
        with pytest.raises(ValueError, match="source disagrees"):
            _prepare(tmp_path, observation, selection, helper)
    else:
        targets = json.loads(_prepare(tmp_path, observation, selection, helper).data)["targets"]
        target = next(item for item in targets if item["thread_id"] == str(_id(2).value))
        assert target["source"] == source
        assert target["parent_relation"] == {"kind": "spawn", "parent_thread_id": str(_id(1).value)}
    assert _before(tmp_path / "home") == before


@pytest.mark.parametrize("selected_provider", [False, True])
def test_retained_reverted_guardian_keeps_association_independent_of_prefix(
    tmp_path: Path, *, selected_provider: bool
) -> None:
    """Own retained provider allows preparation; an actual selected provider protects its owner."""
    observation, selection, helper = _fixture(tmp_path)
    initial = _guardian_fixture(tmp_path)
    reverted = initial.with_name(
        initial.name.replace(f"{_id(4).value}.jsonl", f"{_id(4).value}_{_id(5).value}.jsonl")
    )
    envelope = json.loads(initial.read_text())
    envelope["payload"].update(
        history_mode="paginated",
        history_base={
            "thread_id": str(_id(2 if selected_provider else 4).value),
            "end_ordinal_exclusive": 1,
            "end_byte_offset": 17,
        },
    )
    reverted.write_text(json.dumps(envelope) + "\n")
    with sqlite3.connect(tmp_path / "home" / DatabaseKind.STATE) as connection:
        connection.execute(
            "UPDATE threads SET rollout_path=?,history_mode='paginated' WHERE id=?",
            (str(reverted), str(_id(4).value)),
        )
    observation = _refresh_physical(tmp_path, observation)
    if selected_provider:
        with pytest.raises(PreparationRefused, match="stale, refused or empty"):
            _prepare(tmp_path, observation, selection, helper)
    else:
        before = _before(tmp_path / "home")
        request = json.loads(_prepare(tmp_path, observation, selection, helper).data)
        assert (
            str(tmp_path / "home" / f"thread-writer-locks/{_id(4).value}.lock")
            in request["guard_paths"]
        )
        assert set(request["deletion_scope"]) == {str(_id(i).value) for i in (1, 2)}
        assert all(target["thread_id"] != str(_id(4).value) for target in request["targets"])
        assert _before(tmp_path / "home") == before


def test_board_template_payload_is_opaque_and_other_roots_are_unbound(tmp_path: Path) -> None:
    observation, selection, helper = _fixture(tmp_path)
    board = tmp_path / "home" / DatabaseKind.BOARD
    with sqlite3.connect(board) as connection:
        connection.execute("UPDATE board_templates SET template=?", ("\x00opaque:not-json:☃",))
    before = board.read_bytes()
    value = json.loads(_prepare(tmp_path, observation, selection, helper).data)
    bound = next(item for item in value["databases"] if Path(item["path"]).name == board.name)
    assert bound["rows"]["board_templates"] == [1]
    assert "opaque" not in json.dumps(bound)
    assert board.read_bytes() == before


@pytest.mark.parametrize("mutation", ["missing-template-table", "extra-template-trigger"])
def test_unqualified_board_schema_refuses_before_collecting_rows(
    tmp_path: Path, mutation: str
) -> None:
    observation, selection, helper = _fixture(tmp_path)
    board = tmp_path / "home" / DatabaseKind.BOARD
    with sqlite3.connect(board) as connection:
        if mutation == "missing-template-table":
            connection.execute("DROP TABLE board_templates")
        else:
            connection.execute(
                "CREATE TRIGGER changed_board AFTER DELETE ON board_templates BEGIN SELECT 1; END"
            )
    before = _before(tmp_path / "home")
    with pytest.raises(PreparationRefused, match="schema"):
        _prepare(tmp_path, observation, selection, helper)
    assert _before(tmp_path / "home") == before
