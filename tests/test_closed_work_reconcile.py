# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, S608, C408

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator

import pytest

from codex_session_gc.execution import closed_work_reconcile as observer
from codex_session_gc.execution import closed_work_request as collector
from codex_session_gc.execution.closed_work_apply import DispatchLimits, _payload, apply_closed_work
from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
from codex_session_gc.execution.closed_work_request import (
    CollectionLimits,
    DatabaseKind,
    QualifiedHelperContract,
)
from codex_session_gc.execution.process_identity import ChildIdentity, Presence
from codex_session_gc.planning.closed_work import select_closed_work
from codex_session_gc.recovery import (
    AttemptId,
    LaunchId,
    LaunchIntent,
    LaunchPurpose,
    LaunchRegistration,
    LaunchStatus,
    Payload,
    StartEnvelope,
    TerminalKind,
    TransitionError,
    Unit,
    UnitId,
    create,
    fence_launch,
    inspect,
    record_dispatch_intent,
    record_launch_absence,
    record_launch_intent,
    record_terminal_evidence,
    register_launch,
)
from tests.current_storage_fixture import (
    _CUTOFF,
    _LIMITS,
    _before,
    _fixture,
    _guardian_fixture,
    _id,
    _prepare,
    _refresh_physical,
)
from tests.test_closed_work_apply import _setup as _apply_fixture

type Setup = tuple[Path, AttemptId, UnitId, Payload, QualifiedHelperContract, ChildIdentity]

_SELECTED = tuple(str(_id(number).value) for number in (1, 2))


def _setup(  # noqa: C901, PLR0912, PLR0913, PLR0915
    tmp_path: Path,
    *,
    terminal: TerminalKind = TerminalKind.PARTIAL,
    child_only: bool = False,
    absent_board: bool = False,
    malformed: str | None = None,
    declaration: str | None = None,
    profile: Payload | None = None,
    guardian: bool = False,
    guard_mutation: str | None = None,
    source_variant: str | None = None,
) -> Setup:
    observation, selection, helper = _fixture(tmp_path)
    if profile is not None:
        helper = replace(helper, evidence=profile)
    if child_only:
        facts = observation.snapshot
        assert facts is not None
        selection = select_closed_work(
            facts, observation.cutoff, frozenset({_id(1)}), selected_threads=frozenset({_id(2)})
        )
    if absent_board:
        (tmp_path / "home" / DatabaseKind.BOARD).unlink()
    if guardian:
        _guardian_fixture(tmp_path)
        observation = _refresh_physical(tmp_path, observation)
    request = _prepare(tmp_path, observation, selection, helper)
    if source_variant is not None:
        raw = json.loads(request.data)
        child = next(item for item in raw["targets"] if item["thread_id"] == str(_id(2).value))
        child["source"] = (
            {"subagent": {"other": "fixture-worker"}}
            if source_variant == "other"
            else {"subagent": {"thread_spawn": {"parent_thread_id": str(_id(3).value), "depth": 1}}}
        )
        request = replace(request, data=json.dumps(raw).encode())
    if guard_mutation is not None:
        raw = json.loads(request.data)
        extra = str(tmp_path / "home" / f"thread-writer-locks/{_id(4).value}.lock")
        if guard_mutation == "missing":
            raw["guard_paths"].remove(
                str(tmp_path / "home" / f"thread-writer-locks/{_id(1).value}.lock")
            )
        elif guard_mutation == "duplicate":
            raw["guard_paths"].append(extra)
        else:
            raw["guard_paths"].remove(extra)
            invalid = {
                "foreign": str(tmp_path / "foreign" / f"{_id(4).value}.lock"),
                "parent": str(
                    tmp_path / "home" / "thread-writer-locks" / ".." / f"{_id(4).value}.lock"
                ),
                "uppercase": str(
                    tmp_path
                    / "home"
                    / "thread-writer-locks/00000000-0000-0000-0000-00000000000A.lock"
                ),
                "suffix": str(tmp_path / "home" / f"thread-writer-locks/{_id(4).value}.other"),
            }
            raw["guard_paths"].append(invalid[guard_mutation])
        raw["guard_paths"].sort()
        request = replace(request, data=json.dumps(raw).encode())
    if malformed is not None:
        raw = json.loads(request.data)
        if malformed in {"version", "retired_version"}:
            raw["version"] = 2 if malformed == "version" else 3
        elif malformed == "wire4":
            raw["version"] = 4
            for target in raw["targets"]:
                relation = target.pop("parent_relation")
                target["expected_spawn_parent"] = relation.get("parent_thread_id")
        elif malformed == "scope":
            raw["deletion_scope"].append(str(_id(3).value))
        elif malformed == "path":
            raw["targets"][0]["rollouts"][0]["rollout_path"] = str(tmp_path / "outside.jsonl")
        elif malformed == "bound":
            raw["databases"][0]["rows"]["unexpected"] = []
        request = replace(request, data=json.dumps(raw).encode())
    raw = json.loads(request.data)
    receipt = Path(raw["journal"])
    attempt = AttemptId(UUID(raw["attempt_id"]))
    unit = UnitId(UUID(raw["unit_id"]))
    evidence = Payload("test/evidence", 1, b"fixture")
    scope = dict(
        root=raw["ownership_scope"]["root_thread_id"],
        ownership_scope=raw["ownership_scope"]["thread_ids"],
        requested_threads=sorted(str(item.value) for item in selection.requested_threads),
        deletion_scope=raw["deletion_scope"],
        guardian_associations=[],
    )
    if malformed == "wire4":
        scope.pop("guardian_associations")
    context = dict(
        codex_home=raw["codex_home"],
        sqlite_home=raw["sqlite_home"],
        journal=raw["journal"],
        helper_path=str(tmp_path / "qualified-helper"),
        helper_sha256=helper.binary_sha256.hex(),
        source_identity=helper.source_identity,
        provider=raw["provider"],
        cutoff=raw["cutoff"],
        collection_limits=asdict(_LIMITS),
        dispatch_limits=asdict(DispatchLimits(5, 1000000, 4096, 1)),
        scopes=[scope],
    )
    process = _payload(context)
    plan = _payload([scope])
    unit_evidence = _payload(scope)
    if declaration == "unknown":
        process = evidence
    elif declaration == "home":
        context["codex_home"] = str(tmp_path / "different-home")
        process = _payload(context)
    elif declaration == "helper":
        context["helper_sha256"] = "00" * 32
        process = _payload(context)
    elif declaration == "root":
        scope["root"] = str(_id(3).value)
        process = _payload(context)
        plan = _payload([scope])
        unit_evidence = _payload(scope)
    elif declaration == "ownership":
        scope["ownership_scope"] = [str(_id(1).value), str(_id(2).value), str(_id(3).value)]
        process = _payload(context)
        plan = _payload([scope])
        unit_evidence = _payload(scope)
    elif declaration == "deletion":
        scope["deletion_scope"] = [str(_id(2).value)]
        process = _payload(context)
        plan = _payload([scope])
        unit_evidence = _payload(scope)
    elif declaration == "plan":
        plan = evidence
    elif declaration == "unit":
        unit_evidence = evidence
    elif declaration == "process":
        process = evidence
    create(
        receipt,
        StartEnvelope(attempt, helper.evidence, _payload(context), plan, process),
        (Unit(unit, 0, unit_evidence),),
    )
    launch = LaunchId(uuid4())
    identity = ChildIdentity(543210, "fixture-owned", 12, "boot", "pidns", "mntns", 0, "proc")
    record_launch_intent(receipt, attempt, LaunchIntent(launch, 1, LaunchPurpose.APPLY, request))
    register_launch(receipt, attempt, LaunchRegistration(launch, 1, identity.incarnation()))
    record_dispatch_intent(receipt, attempt, unit, request, evidence, identity.incarnation())
    record_terminal_evidence(receipt, attempt, unit, request.digest, terminal, evidence)
    return receipt, attempt, unit, request, helper, identity


def _delete_selected(
    home: Path, *, board: bool = True, selected: tuple[str, ...] = _SELECTED
) -> None:
    selected = (selected[0], selected[-1])
    # Simulate the persisted effects, independently of the observer queries.
    tables = {
        DatabaseKind.STATE: (
            "threads",
            "thread_dynamic_tools",
            "thread_attachments",
            "guardian_review_feedback",
            "thread_read_receipts",
        ),
        DatabaseKind.LOGS: ("logs",),
        DatabaseKind.GOALS: ("thread_goals", "thread_goal_continuation_deferrals"),
        DatabaseKind.MEMORY: ("stage1_outputs",),
        DatabaseKind.MEMORY_V2: ("stage1_outputs",),
        DatabaseKind.HISTORY: (
            "thread_items",
            "thread_realtime_items",
            "thread_turns",
            "thread_history_projection_state",
        ),
        DatabaseKind.QUEUE: ("queued_items", "queued_thread_revisions"),
    }
    for kind, names in tables.items():
        with sqlite3.connect(home / kind.value) as connection:
            for table in names:
                column = "id" if table == "threads" else "thread_id"
                connection.execute(f"DELETE FROM {table} WHERE {column} IN (?,?)", selected)
            if kind is DatabaseKind.STATE:
                connection.execute(
                    (
                        "DELETE FROM thread_spawn_edges WHERE parent_thread_id IN (?,?) "
                        "OR child_thread_id IN (?,?)"
                    ),
                    selected * 2,
                )
            if kind in {DatabaseKind.MEMORY, DatabaseKind.MEMORY_V2}:
                connection.execute(
                    "DELETE FROM jobs WHERE kind='memory_stage1' AND job_key IN (?,?)", selected
                )
    if board:
        with sqlite3.connect(home / DatabaseKind.BOARD) as connection:
            for table in (
                "board_templates",
                "subscriptions",
                "subscription_opt_outs",
                "posts",
                "channels",
            ):
                connection.execute(f"DELETE FROM {table} WHERE board=?", (selected[0],))
            connection.execute(
                (
                    "INSERT INTO deleted_boards(board) SELECT ? WHERE NOT EXISTS("
                    "SELECT 1 FROM deleted_boards WHERE board=?)"
                ),
                (selected[0], selected[0]),
            )
    for path in (home / "sessions").glob("*.jsonl"):
        if json.loads(path.read_bytes())["payload"]["id"] in selected:
            path.unlink()


def _observe(
    tmp_path: Path,
    setup: Setup,
    monkeypatch: pytest.MonkeyPatch,
    *,
    presence: Presence = Presence.ABSENT,
    limits: CollectionLimits = _LIMITS,
) -> tuple[observer.ReconciliationResult, list[ChildIdentity]]:
    receipt, attempt, unit, _, helper, identity = setup
    seen: list[ChildIdentity] = []

    def present(value: ChildIdentity) -> Presence:
        assert value == identity
        seen.append(value)
        return presence

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("The observer attempted to launch a process")

    monkeypatch.setattr(observer, "child_presence", present)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    connect = sqlite3.connect

    def readonly_sources(
        database: str | Path,
        *,
        uri: bool = False,
        timeout: float = 5,
        isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = None,
    ) -> sqlite3.Connection:
        if str(tmp_path / "home") in str(database):
            assert uri
            assert str(database).endswith("?mode=ro")
        return connect(database, uri=uri, timeout=timeout, isolation_level=isolation_level)

    monkeypatch.setattr(sqlite3, "connect", readonly_sources)
    before = _before(tmp_path / "home")
    result = observer.reconcile(receipt, attempt, unit, helper=helper, limits=limits)
    assert _before(tmp_path / "home") == before
    return result, seen


def test_final_state_confirmed_preserves_partial_intent_and_unrelated_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    home = tmp_path / "home"
    _delete_selected(home)
    result, seen = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.CONFIRMED
    assert result.recorded
    assert result.original_terminal is TerminalKind.PARTIAL
    assert seen
    value = json.loads(result.evidence.data)
    assert value["state"] == "CONFIRMED_FINAL_STATE"
    assert value["atomic"] is False
    assert value["attribution"] is False
    assert value["perpetual_absence"] is False
    (status,) = inspect(setup[0])
    assert status.units[0].request == setup[3]
    assert status.units[0].terminal_kind is TerminalKind.PARTIAL
    assert status.units[0].resolution_kind is TerminalKind.CONFIRMED
    assert status.launches[0].status is LaunchStatus.OBSERVED_ABSENT
    assert status.lifecycle_kind is TerminalKind.CONFIRMED
    with pytest.raises(TransitionError):
        record_dispatch_intent(
            setup[0], setup[1], setup[2], setup[3], setup[4].evidence, setup[5].incarnation()
        )


@pytest.mark.parametrize("presence", [Presence.ALIVE, Presence.AMBIGUOUS])
def test_owned_child_not_absent_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, presence: Presence
) -> None:
    setup = _setup(tmp_path)
    _delete_selected(tmp_path / "home")
    result, _ = _observe(tmp_path, setup, monkeypatch, presence=presence)
    assert result.state is observer.FinalState.UNKNOWN
    assert result.recorded
    (status,) = inspect(setup[0])
    assert status.launches[0].status is LaunchStatus.REGISTERED
    assert status.units[0].terminal_kind is TerminalKind.PARTIAL
    assert status.units[0].resolution_kind is TerminalKind.UNKNOWN


@pytest.mark.parametrize(
    ("kind", "statement"),
    [
        (DatabaseKind.LOGS, "INSERT INTO logs(thread_id) VALUES(?)"),
        (DatabaseKind.GOALS, "INSERT INTO thread_goal_continuation_deferrals(thread_id) VALUES(?)"),
        (DatabaseKind.MEMORY, "INSERT INTO stage1_outputs(thread_id) VALUES(?)"),
        (DatabaseKind.MEMORY_V2, "INSERT INTO jobs(kind,job_key) VALUES('memory_stage1',?)"),
        (DatabaseKind.HISTORY, "INSERT INTO thread_realtime_items(thread_id) VALUES(?)"),
        (DatabaseKind.QUEUE, "INSERT INTO queued_items(thread_id) VALUES(?)"),
        (DatabaseKind.BOARD, "INSERT INTO posts(board) VALUES(?)"),
    ],
)
def test_mixed_or_recreated_selected_effects_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: DatabaseKind, statement: str
) -> None:
    setup = _setup(tmp_path)
    home = tmp_path / "home"
    _delete_selected(home)
    with sqlite3.connect(home / kind.value) as connection:
        connection.execute(statement, (_SELECTED[0],))
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.UNKNOWN
    assert result.recorded


@pytest.mark.parametrize("sibling", [False, True])
def test_saved_physical_absence_does_not_rediscover_other_representations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, sibling: bool
) -> None:
    setup = _setup(tmp_path)
    path = Path(json.loads(setup[3].data)["targets"][0]["rollouts"][0]["rollout_path"])
    _delete_selected(tmp_path / "home")
    observed = path.with_suffix(".jsonl.zst") if sibling else path
    observed.write_bytes(b"recreated selected history")
    result, _ = _observe(tmp_path, setup, monkeypatch)
    expected = observer.FinalState.CONFIRMED if sibling else observer.FinalState.UNKNOWN
    assert result.state is expected
    assert observed.read_bytes() == b"recreated selected history"


def test_missing_required_database_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = _setup(tmp_path)
    _delete_selected(tmp_path / "home")
    (tmp_path / "home" / DatabaseKind.LOGS).unlink()
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.UNKNOWN


def test_required_root_tombstone_missing_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    home = tmp_path / "home"
    _delete_selected(home)
    with sqlite3.connect(home / DatabaseKind.BOARD) as connection:
        connection.execute("DELETE FROM deleted_boards WHERE board=?", (_SELECTED[0],))
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.UNKNOWN


def test_bounds_unknown_without_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = _setup(tmp_path)
    _delete_selected(tmp_path / "home")
    result, _ = _observe(
        tmp_path, setup, monkeypatch, limits=replace(_LIMITS, maximum_total_bytes=1)
    )
    assert result.state is observer.FinalState.UNKNOWN


def test_wrong_profile_unknown_without_home_or_process_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    receipt, attempt, unit, _, helper, _ = setup
    helper = replace(helper, evidence=Payload("test/different", 1, b"different"))
    monkeypatch.setattr(
        observer, "child_presence", lambda _: pytest.fail("Unexpected process observation")
    )
    before = _before(tmp_path / "home")
    result = observer.reconcile(receipt, attempt, unit, helper=helper, limits=_LIMITS)
    assert result.state is observer.FinalState.UNKNOWN
    assert _before(tmp_path / "home") == before


def test_child_subset_confirms_without_deleting_parent_or_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, child_only=True)
    home = tmp_path / "home"
    board_before = (home / DatabaseKind.BOARD).read_bytes()
    parent = next(
        path
        for path in (home / "sessions").glob("*.jsonl")
        if json.loads(path.read_bytes())["payload"]["id"] == _SELECTED[0]
    )
    _delete_selected(home, selected=(_SELECTED[1],), board=False)
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.CONFIRMED
    assert parent.exists()
    assert (home / DatabaseKind.BOARD).read_bytes() == board_before


def test_originally_absent_board_needs_no_marker_or_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, absent_board=True)
    home = tmp_path / "home"
    _delete_selected(home, board=False)
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.CONFIRMED
    assert not (home / DatabaseKind.BOARD).exists()


@pytest.mark.parametrize("malformed", ["version", "wire4", "scope", "path", "bound"])
def test_malformed_saved_intent_unknown_before_child_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: str
) -> None:
    setup = _setup(tmp_path, malformed=malformed)
    result, seen = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.UNKNOWN
    assert not seen


def test_known_selected_path_still_catalogued_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    path = json.loads(setup[3].data)["targets"][0]["rollouts"][0]["rollout_path"]
    home = tmp_path / "home"
    _delete_selected(home)
    with sqlite3.connect(home / DatabaseKind.STATE) as connection:
        connection.execute(
            "UPDATE threads SET rollout_path=? WHERE id=?", (path, str(_id(3).value))
        )
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.UNKNOWN


def test_unrelated_rows_and_queue_sequence_not_absence_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    home = tmp_path / "home"
    _delete_selected(home)
    with sqlite3.connect(home / DatabaseKind.LOGS) as connection:
        connection.execute("INSERT INTO logs(thread_id) VALUES(?)", (str(_id(3).value),))
    with sqlite3.connect(home / DatabaseKind.QUEUE) as connection:
        assert connection.execute("SELECT seq FROM sqlite_sequence").fetchone() is not None
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.CONFIRMED


@pytest.mark.parametrize("fenced", [False, True])
def test_pending_or_fenced_unregistered_launcher_not_claimed_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fenced: bool
) -> None:
    setup = _setup(tmp_path)
    receipt, attempt, _, request, helper, _ = setup
    (status,) = inspect(receipt)
    first = status.launches[0].intent
    record_launch_absence(receipt, attempt, first.launch_id, first.generation, helper.evidence)
    pending = LaunchId(uuid4())
    record_launch_intent(
        receipt, attempt, LaunchIntent(pending, 2, LaunchPurpose.RECONCILE, request)
    )
    if fenced:
        fence_launch(receipt, attempt, pending, 2, helper.evidence)
    _delete_selected(tmp_path / "home")
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.UNKNOWN
    assert result.recorded
    (status,) = inspect(receipt)
    assert status.lifecycle_kind is None
    assert status.launches[-1].status is (LaunchStatus.FENCED if fenced else LaunchStatus.PENDING)


def test_foreign_sqlite_receipt_never_recovered_or_mutated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    receipt = tmp_path / "foreign.sqlite"
    with sqlite3.connect(receipt) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    before = receipt.read_bytes(), receipt.stat()
    monkeypatch.setattr(
        observer, "child_presence", lambda _: pytest.fail("Unexpected process observation")
    )
    result = observer.reconcile(receipt, setup[1], setup[2], helper=setup[4], limits=_LIMITS)
    after = receipt.read_bytes(), receipt.stat()
    assert result.state is observer.FinalState.UNKNOWN
    assert not result.recorded
    assert before[0] == after[0]
    assert before[1].st_ino == after[1].st_ino
    assert before[1].st_mtime_ns == after[1].st_mtime_ns


def test_late_owned_absence_ambiguity_prevents_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    _delete_selected(tmp_path / "home")
    copied = False
    read = collector._read_transaction  # noqa: SLF001
    identity = setup[-1]

    @contextmanager
    def concurrent_read(
        source: Path, budget: collector.CollectionBudget
    ) -> Iterator[sqlite3.Connection]:
        nonlocal copied
        with read(source, budget) as connection:
            yield connection
        copied = True

    def presence(value: ChildIdentity) -> Presence:
        assert value == identity
        return Presence.AMBIGUOUS if copied else Presence.ABSENT

    monkeypatch.setattr(observer, "child_presence", presence)
    monkeypatch.setattr(collector, "_read_transaction", concurrent_read)
    before = _before(tmp_path / "home")
    result = observer.reconcile(setup[0], setup[1], setup[2], helper=setup[4], limits=_LIMITS)
    assert result.state is observer.FinalState.UNKNOWN
    assert result.recorded
    assert _before(tmp_path / "home") == before
    (status,) = inspect(setup[0])
    assert status.launches[0].status is LaunchStatus.REGISTERED
    assert status.lifecycle_kind is None


@pytest.mark.parametrize(
    "declaration",
    ["unknown", "home", "helper", "root", "ownership", "deletion", "plan", "unit", "process"],
)
def test_saved_apply_declaration_mismatch_unknown_before_work_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, declaration: str
) -> None:
    setup = _setup(tmp_path, declaration=declaration)
    monkeypatch.setattr(
        observer, "child_presence", lambda _: pytest.fail("Unexpected process observation")
    )
    monkeypatch.setattr(
        collector, "_sample", lambda _: pytest.fail("Unexpected work-home observation")
    )
    result = observer.reconcile(setup[0], setup[1], setup[2], helper=setup[4], limits=_LIMITS)
    assert result.state is observer.FinalState.UNKNOWN
    assert result.recorded
    (status,) = inspect(setup[0])
    assert status.units[0].request == setup[3]
    assert status.units[0].terminal_kind is TerminalKind.PARTIAL
    assert status.lifecycle_kind is None


def test_real_apply_journal_can_be_reconciled_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation, first, helper = _fixture(tmp_path)
    facts = observation
    second = select_closed_work(facts.snapshot, facts.cutoff, frozenset({_id(3)}))
    selections = (first, second)
    consumed = tmp_path / "consumed"
    executable = tmp_path / "fixture-helper"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys,json,hashlib\n"
        "raw=sys.stdin.buffer.read()\n"
        f"open({str(consumed)!r},'ab').write(b'consumed\\n')\n"
        "print(json.dumps(dict(status='partial',request_sha256=list(hashlib.sha256(raw).digest()))))\n"
        "sys.exit(2)\n"
    )
    executable.chmod(0o700)
    helper = replace(helper, binary_sha256=sha256(executable.read_bytes()).digest())
    receipt = tmp_path / "actual-apply.sqlite"
    applied = apply_closed_work(
        facts,
        selections,
        codex_home=tmp_path / "home",
        sqlite_home=tmp_path / "home",
        journal=receipt,
        helper_executable=executable,
        helper=helper,
        default_provider="openai",
        collection_limits=_LIMITS,
        dispatch_limits=DispatchLimits(5, 1000000, 4096, 1),
    )
    assert applied.outcome is TerminalKind.PARTIAL
    before_consumed = consumed.read_bytes()
    _delete_selected(tmp_path / "home")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Reconciliation attempted to start another helper")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = _before(tmp_path / "home")
    result = observer.reconcile(
        receipt,
        applied.attempt_id,
        applied.roots[0].unit_id,
        helper=helper,
        limits=_LIMITS,
    )
    assert result.state is observer.FinalState.CONFIRMED
    assert result.recorded
    assert result.original_terminal is TerminalKind.PARTIAL
    assert consumed.read_bytes() == before_consumed
    assert _before(tmp_path / "home") == before
    (status,) = inspect(receipt)
    assert status.units[0].terminal_kind is TerminalKind.PARTIAL
    assert status.units[1].request is None


def test_noncurrent_saved_request_refuses_final_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, malformed="retired_version")
    _delete_selected(tmp_path / "home")
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.UNKNOWN


def test_current_guardian_request_reconciles_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path, guardian=True)
    raw = json.loads(setup[3].data)
    assert str(tmp_path / "home" / f"thread-writer-locks/{_id(4).value}.lock") in raw["guard_paths"]
    _delete_selected(tmp_path / "home")
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.CONFIRMED
    with sqlite3.connect(
        (tmp_path / "home" / DatabaseKind.STATE).as_uri() + "?mode=ro", uri=True
    ) as connection:
        assert connection.execute(
            "SELECT id FROM threads WHERE id=?", (str(_id(4).value),)
        ).fetchone()


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "foreign", "parent", "uppercase", "suffix"]
)
def test_invalid_guard_paths_refuse_final_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    setup = _setup(tmp_path, guardian=True, guard_mutation=mutation)
    _delete_selected(tmp_path / "home")
    result, _ = _observe(tmp_path, setup, monkeypatch)
    assert result.state is observer.FinalState.UNKNOWN


@pytest.mark.parametrize("table", ["guardian_review_feedback", "thread_read_receipts"])
def test_selected_supplemental_rows_block_confirmation_until_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    setup = _setup(tmp_path)
    home = tmp_path / "home"
    _delete_selected(home)
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            f"INSERT INTO {table} VALUES(?,?)", (str(_id(2).value), b"selected opaque")
        )
        connection.execute(
            f"INSERT INTO {table} VALUES(?,?)", (str(_id(3).value), b"retained opaque")
        )
        assert connection.execute(
            f"SELECT count(*) FROM {table} WHERE thread_id=?", (str(_id(2).value),)
        ).fetchone() == (1,)
    with monkeypatch.context() as platform:
        blocked, seen = _observe(tmp_path, setup, platform)
    assert seen  # The explicit ABSENT platform observation preceded the SQL observation.
    assert blocked.state is observer.FinalState.UNKNOWN
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(f"DELETE FROM {table} WHERE thread_id=?", (str(_id(2).value),))
    with monkeypatch.context() as platform:
        confirmed, seen = _observe(tmp_path, setup, platform)
    assert seen
    assert confirmed.state is observer.FinalState.CONFIRMED
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == [
            (str(_id(3).value), b"retained opaque")
        ]


def test_selected_guardian_saved_association_reconciles_without_parent_ownership(
    tmp_path: Path,
) -> None:
    observation, _, options, consumed = _apply_fixture(tmp_path, "partial")
    home = options["codex_home"]
    path = next(item.path for item in observation.rollouts if item.thread_id == _id(3))
    metadata = json.loads(path.read_text())
    source = {"subagent": {"other": "guardian"}}
    metadata["payload"].update(source=source, parent_thread_id=str(_id(1).value))
    path.write_text(json.dumps(metadata) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET source=? WHERE id=?", (json.dumps(source), str(_id(3).value))
        )
    facts = read_closed_work_facts(home, _CUTOFF, frozenset({_id(3)}), None, _LIMITS)
    selection = select_closed_work(facts.snapshot, _CUTOFF, frozenset({_id(3)}))
    result = apply_closed_work(facts, (selection,), **options)
    assert result.outcome is TerminalKind.PARTIAL
    dispatched = consumed.read_bytes()
    parent = next(item.path for item in observation.rollouts if item.thread_id == _id(1))
    retained = parent.read_bytes()
    _delete_selected(home, selected=(str(_id(3).value),))
    reconciled = observer.reconcile(
        options["journal"],
        result.attempt_id,
        result.roots[0].unit_id,
        helper=options["helper"],
        limits=_LIMITS,
    )
    assert reconciled.state is observer.FinalState.CONFIRMED
    assert reconciled.original_terminal is TerminalKind.PARTIAL
    assert parent.read_bytes() == retained
    assert consumed.read_bytes() == dispatched


@pytest.mark.parametrize("source_variant", ["other", "contradictory"])
def test_saved_closed_sql_spawn_relation_validates_optional_source_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_variant: str
) -> None:
    setup = _setup(tmp_path, source_variant=source_variant)
    _delete_selected(tmp_path / "home")
    result, seen = _observe(tmp_path, setup, monkeypatch)
    if source_variant == "other":
        assert seen
        assert result.state is observer.FinalState.CONFIRMED
    else:
        assert not seen
        assert result.state is observer.FinalState.UNKNOWN


def test_selected_board_template_prevents_confirmation_until_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _setup(tmp_path)
    home = tmp_path / "home"
    _delete_selected(home)
    with sqlite3.connect(home / DatabaseKind.BOARD) as connection:
        connection.execute(
            "INSERT INTO board_templates VALUES(?,?)",
            (_SELECTED[0], "opaque:not-json:late-template"),
        )
        unrelated = connection.execute(
            "SELECT * FROM board_templates WHERE board=?", (str(_id(3).value),)
        ).fetchall()
    with monkeypatch.context() as platform:
        blocked, _ = _observe(tmp_path, setup, platform)
    assert blocked.state is observer.FinalState.UNKNOWN
    with sqlite3.connect(home / DatabaseKind.BOARD) as connection:
        connection.execute("DELETE FROM board_templates WHERE board=?", (_SELECTED[0],))
        assert (
            connection.execute(
                "SELECT * FROM board_templates WHERE board=?", (str(_id(3).value),)
            ).fetchall()
            == unrelated
        )
    with monkeypatch.context() as platform:
        confirmed, _ = _observe(tmp_path, setup, platform)
    assert confirmed.state is observer.FinalState.CONFIRMED


@pytest.mark.parametrize("terminal", [TerminalKind.CONFIRMED, TerminalKind.REFUSED])
def test_lifecycle_only_reconciliation_settles_definitive_units_without_effect_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: TerminalKind
) -> None:
    setup = _setup(tmp_path, terminal=terminal)
    receipt, attempt, _, request, _, identity = setup
    before_home = _before(tmp_path / "home")
    monkeypatch.setattr(observer, "_observe", lambda *_: pytest.fail("No selected-effect read"))
    monkeypatch.setattr(observer, "child_presence", lambda _: Presence.ABSENT)

    result = observer.reconcile_lifecycle(receipt, attempt, limits=_LIMITS)

    assert result.state is observer.LifecycleState.SETTLED
    assert result.recorded
    (stored,) = inspect(receipt)
    assert stored.shutdown is None
    assert stored.startup_unresolved
    assert stored.units[0].terminal_kind is terminal
    assert stored.units[0].request == request
    assert stored.units[0].resolution is None
    assert stored.launches[0].registration is not None
    assert stored.launches[0].registration.incarnation == identity.incarnation()
    assert stored.launches[0].status is LaunchStatus.OBSERVED_ABSENT
    assert stored.lifecycle_generation == stored.launch_generation == 1
    assert stored.lifecycle_kind is TerminalKind.CONFIRMED
    assert _before(tmp_path / "home") == before_home

    before_receipt = receipt.read_bytes()
    monkeypatch.setattr(
        observer, "child_presence", lambda _: pytest.fail("Settled generation must not be reread")
    )
    repeated = observer.reconcile_lifecycle(receipt, attempt, limits=_LIMITS)
    assert repeated.state is observer.LifecycleState.SETTLED
    assert repeated.recorded
    assert repeated.evidence == result.evidence
    assert receipt.read_bytes() == before_receipt


@pytest.mark.parametrize("presence", [Presence.ALIVE, Presence.AMBIGUOUS])
def test_lifecycle_only_reconciliation_preserves_live_or_unreadable_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, presence: Presence
) -> None:
    receipt, attempt, _, _, _, _ = _setup(tmp_path, terminal=TerminalKind.CONFIRMED)
    monkeypatch.setattr(observer, "child_presence", lambda _: presence)
    before = receipt.read_bytes()

    result = observer.reconcile_lifecycle(receipt, attempt, limits=_LIMITS)

    assert result.state is observer.LifecycleState.UNKNOWN
    assert not result.recorded
    assert receipt.read_bytes() == before
    (stored,) = inspect(receipt)
    assert stored.lifecycle is None
    assert stored.launches[0].status is LaunchStatus.REGISTERED


@pytest.mark.parametrize("fenced", [False, True])
def test_lifecycle_only_reconciliation_fences_but_never_reaps_an_unregistered_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fenced: bool
) -> None:
    receipt, attempt, _, request, helper, _ = _setup(tmp_path, terminal=TerminalKind.CONFIRMED)
    (stored,) = inspect(receipt)
    first = stored.launches[0].intent
    record_launch_absence(receipt, attempt, first.launch_id, first.generation, helper.evidence)
    pending = LaunchIntent(LaunchId(uuid4()), 2, LaunchPurpose.RECONCILE, request)
    record_launch_intent(receipt, attempt, pending)
    if fenced:
        fence_launch(receipt, attempt, pending.launch_id, 2, helper.evidence)
    monkeypatch.setattr(observer, "child_presence", lambda _: Presence.ABSENT)

    result = observer.reconcile_lifecycle(receipt, attempt, limits=_LIMITS)

    assert result.state is observer.LifecycleState.UNKNOWN
    assert not result.recorded
    (stored,) = inspect(receipt)
    assert stored.launches[-1].status is LaunchStatus.FENCED
    assert stored.launches[-1].registration is None
    assert stored.lifecycle is None
    with pytest.raises(TransitionError):
        register_launch(
            receipt,
            attempt,
            LaunchRegistration(pending.launch_id, 2, helper.evidence),
        )


def test_lifecycle_only_reconciliation_cannot_settle_a_changed_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt, attempt, _, request, helper, _ = _setup(tmp_path, terminal=TerminalKind.CONFIRMED)
    (stored,) = inspect(receipt)
    first = stored.launches[0].intent
    replacement = LaunchIntent(
        LaunchId(uuid4()), first.generation + 1, LaunchPurpose.RECONCILE, request
    )
    changed = False

    def presence(_: ChildIdentity) -> Presence:
        nonlocal changed
        if not changed:
            changed = True
            record_launch_absence(receipt, attempt, first.launch_id, 1, helper.evidence)
            record_launch_intent(receipt, attempt, replacement)
        return Presence.ABSENT

    monkeypatch.setattr(observer, "child_presence", presence)

    result = observer.reconcile_lifecycle(receipt, attempt, limits=_LIMITS)

    assert result.state is observer.LifecycleState.UNKNOWN
    assert not result.recorded
    (stored,) = inspect(receipt)
    assert stored.launch_generation == replacement.generation
    assert stored.lifecycle is None
    assert stored.launches[-1].status is LaunchStatus.PENDING


def test_lifecycle_only_reconciliation_does_not_settle_a_launch_free_start(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.sqlite"
    attempt = AttemptId(uuid4())
    evidence = Payload("test/evidence", 1, b"declared")
    create(
        receipt,
        StartEnvelope(attempt, evidence, evidence, evidence, evidence),
        (Unit(UnitId(uuid4()), 0, evidence),),
    )
    before = receipt.read_bytes()

    result = observer.reconcile_lifecycle(receipt, attempt, limits=_LIMITS)

    assert result.state is observer.LifecycleState.UNKNOWN
    assert not result.recorded
    assert receipt.read_bytes() == before
    assert inspect(receipt)[0].shutdown is None
