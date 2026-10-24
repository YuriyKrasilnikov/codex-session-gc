# Copyright (c) 2026
# ruff: noqa: S608

"""Disposable selected-work storage and explicit synthetic native contracts."""

from __future__ import annotations

import json
import sqlite3
import time
from base64 import b64decode
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from codex_session_gc.domain import (
    ActivityState,
    ArchiveState,
    FileStatIdentity,
    GraphSnapshot,
    LastActivity,
    OwnershipEdge,
    PhysicalRepresentation,
    RolloutId,
    RolloutRecord,
    SpawnEdge,
    ThreadId,
    ThreadOrigin,
    ThreadRecord,
    UtcCutoff,
)
from codex_session_gc.execution.closed_work_request import (
    ClosedWorkFacts,
    CollectionBudget,
    CollectionLimits,
    DatabaseContract,
    DatabaseKind,
    QualifiedHelperContract,
    prepare_request,
)
from codex_session_gc.execution.physical_inventory import observe_inventory
from codex_session_gc.inventory.catalogue import StoredRollout
from codex_session_gc.inventory.observation import RolloutObservation
from codex_session_gc.planning.closed_work import ClosedWorkSelection, select_closed_work
from codex_session_gc.recovery import AttemptId, Payload, UnitId

_EPOCH = 1780272000000

_CUTOFF = UtcCutoff(datetime(2026, 8, 1, tzinfo=UTC))

_LIMITS = CollectionLimits(4_000_000, 4_000_000, 32_000_000, 100_000, 1_000_000, 30)

_DDL = {
    DatabaseKind.STATE: [
        (
            "CREATE TABLE threads(id TEXT,rollout_path TEXT,source TEXT,agent_role TEXT,"
            "model_provider TEXT,updated_at_ms INTEGER,archived INTEGER,history_mode TEXT)"
        ),
        "CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT)",
        "CREATE TABLE thread_dynamic_tools(thread_id TEXT)",
        "CREATE TABLE thread_attachments(thread_id TEXT)",
        "CREATE TABLE guardian_review_feedback(thread_id TEXT,feedback TEXT)",
        "CREATE TABLE thread_read_receipts(thread_id TEXT,receipt TEXT)",
    ],
    DatabaseKind.LOGS: ["CREATE TABLE logs(thread_id TEXT)"],
    DatabaseKind.GOALS: [
        "CREATE TABLE thread_goals(thread_id TEXT)",
        "CREATE TABLE thread_goal_continuation_deferrals(thread_id TEXT)",
    ],
    DatabaseKind.MEMORY: [
        "CREATE TABLE stage1_outputs(thread_id TEXT)",
        "CREATE TABLE jobs(kind TEXT,job_key TEXT)",
    ],
    DatabaseKind.MEMORY_V2: [
        "CREATE TABLE stage1_outputs(thread_id TEXT)",
        "CREATE TABLE jobs(kind TEXT,job_key TEXT)",
    ],
    DatabaseKind.QUEUE: [
        "CREATE TABLE queued_items(thread_id TEXT)",
        "CREATE TABLE queued_thread_revisions(id INTEGER PRIMARY KEY AUTOINCREMENT,thread_id TEXT)",
    ],
    DatabaseKind.HISTORY: [
        f"CREATE TABLE {name}(thread_id TEXT)"
        for name in (
            "thread_items",
            "thread_realtime_items",
            "thread_turns",
            "thread_history_projection_state",
        )
    ],
    DatabaseKind.BOARD: [
        "CREATE TABLE board_templates(board TEXT PRIMARY KEY NOT NULL,template TEXT NOT NULL)",
        *[
            f"CREATE TABLE {name}(board TEXT)"
            for name in (
                "subscriptions",
                "subscription_opt_outs",
                "posts",
                "channels",
                "deleted_boards",
            )
        ],
    ],
}


def _id(number: int) -> ThreadId:
    return ThreadId(UUID(int=number))


def _fixture(  # noqa: C901, PLR0912
    tmp_path: Path,
) -> tuple[ClosedWorkFacts, ClosedWorkSelection, QualifiedHelperContract]:
    home = tmp_path / "home"
    home.mkdir()
    child_number = 2
    facts = []
    for number in (1, 2, 3):
        path = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(number).value}.jsonl"
        path.parent.mkdir(exist_ok=True)
        source = (
            "cli"
            if number != child_number
            else {"subagent": {"thread_spawn": {"parent_thread_id": str(_id(1).value), "depth": 1}}}
        )
        metadata = {
            "id": str(_id(number).value),
            "source": source,
            "history_mode": "legacy",
            "model_provider": "openai",
        }
        path.write_text(json.dumps({"type": "session_meta", "payload": metadata}) + "\n")
        facts.append(
            RolloutObservation(
                path,
                RolloutId(UUID(int=number)),
                _id(number),
                ArchiveState.NOT_ARCHIVED,
                path.stat().st_size,
                path.stat().st_size,
                "subagent" if number == child_number else "cli",
                _id(1) if number == child_number else None,
                None,
            )
        )
    contracts = []
    for kind, ddl in _DDL.items():
        with sqlite3.connect(home / kind.value) as connection:
            for statement in ddl:
                connection.execute(statement)
            if kind is not DatabaseKind.BOARD:
                connection.execute(
                    "CREATE TABLE _sqlx_migrations(version INTEGER,checksum BLOB,success BOOLEAN)"
                )
                connection.execute("INSERT INTO _sqlx_migrations VALUES(1,?,1)", (b"synthetic",))
            if kind is DatabaseKind.STATE:
                for number, fact in zip((1, 2, 3), facts, strict=True):
                    source = json.loads(fact.path.read_text())["payload"]["source"]
                    connection.execute(
                        "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)",
                        (
                            str(_id(number).value),
                            str(fact.path),
                            source if isinstance(source, str) else json.dumps(source),
                            None,
                            "openai",
                            _EPOCH,
                            0,
                            "legacy",
                        ),
                    )
                connection.execute(
                    "INSERT INTO thread_spawn_edges VALUES(?,?,?)",
                    (str(_id(1).value), str(_id(2).value), "closed"),
                )
            else:
                for table in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                ).fetchall():
                    name = table[0]
                    if name.startswith(("_", "sqlite_")):
                        continue
                    for number in (1, 2, 3):
                        identity = str(_id(number).value)
                        if name == "jobs":
                            connection.execute(
                                "INSERT INTO jobs VALUES(?,?)", ("memory_stage1", identity)
                            )
                            connection.execute(
                                "INSERT INTO jobs VALUES(?,?)", ("global_consolidation", identity)
                            )
                        elif name == "board_templates":
                            connection.execute(
                                "INSERT INTO board_templates VALUES(?,?)",
                                (identity, "opaque:not-json:" + identity),
                            )
                        elif name == "queued_thread_revisions":
                            connection.execute(
                                "INSERT INTO queued_thread_revisions(thread_id) VALUES(?)",
                                (identity,),
                            )
                        else:
                            connection.execute(f"INSERT INTO {name} VALUES(?)", (identity,))
            excluded = "" if kind is DatabaseKind.BOARD else " AND name!='_sqlx_migrations'"
            schema = tuple(
                connection.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%'" + excluded + " ORDER BY type,name"
                )
            )
            contracts.append(
                DatabaseContract(
                    kind, schema, () if kind is DatabaseKind.BOARD else ((1, b"synthetic", True),)
                )
            )
    snapshot = GraphSnapshot.from_records(
        threads=[
            ThreadRecord(
                _id(number),
                ThreadOrigin.SUBAGENT if number == child_number else ThreadOrigin.MAIN,
                ActivityState.UNKNOWN,
                ArchiveState.NOT_ARCHIVED,
                LastActivity(datetime.fromtimestamp(_EPOCH // 1000, UTC)),
            )
            for number in (1, 2, 3)
        ],
        rollouts=[
            RolloutRecord(fact.rollout_id, ArchiveState.NOT_ARCHIVED)
            for fact in facts
            if fact.rollout_id is not None
        ],
        ownership_edges=[
            OwnershipEdge(fact.thread_id, fact.rollout_id)
            for fact in facts
            if fact.thread_id is not None and fact.rollout_id is not None
        ],
        spawn_edges=[SpawnEdge(_id(1), _id(2))],
    )
    inventory, headers = observe_inventory(
        home,
        {
            str(fact.thread_id.value): {"rollout_path": str(fact.path)}
            for fact in facts
            if fact.thread_id is not None
        },
        frozenset({_id(1), _id(2)}),
        CollectionBudget(_LIMITS, time.monotonic() + _LIMITS.deadline_seconds),
    )
    observation = ClosedWorkFacts(
        snapshot, tuple(facts), _CUTOFF, inventory, tuple(headers.values())
    )
    selection = select_closed_work(snapshot, _CUTOFF, frozenset({_id(1)}))
    helper = QualifiedHelperContract(
        b"x" * 32,
        "synthetic-source",
        Payload("test/qualification", 1, b"synthetic"),
        tuple(contracts),
    )
    return observation, selection, helper


def _refresh_physical(
    tmp_path: Path, observation: ClosedWorkFacts, limits: CollectionLimits = _LIMITS
) -> ClosedWorkFacts:
    """Observe a test fixture after intentional setup changes, before review."""
    home = tmp_path / "home"
    with sqlite3.connect(home / DatabaseKind.STATE) as connection:
        rows = {
            owner: {"rollout_path": path}
            for owner, path in connection.execute("SELECT id,rollout_path FROM threads")
        }
    inventory, headers = observe_inventory(
        home,
        rows,
        frozenset({_id(1), _id(2)}),
        CollectionBudget(limits, time.monotonic() + limits.deadline_seconds),
    )
    return replace(observation, physical_inventory=inventory, headers=tuple(headers.values()))


def _prepare(  # noqa: PLR0913
    tmp_path: Path,
    observation: ClosedWorkFacts,
    selection: ClosedWorkSelection,
    helper: QualifiedHelperContract,
    limits: CollectionLimits = _LIMITS,
    *,
    default_provider: str = "store-default",
) -> Payload:
    return prepare_request(
        observation,
        selection,
        cutoff=_CUTOFF,
        codex_home=tmp_path / "home",
        sqlite_home=tmp_path / "home",
        journal=tmp_path / "receipt.sqlite",
        attempt_id=AttemptId(uuid4()),
        unit_id=UnitId(uuid4()),
        helper=helper,
        default_provider=default_provider,
        limits=limits,
    )


def _before(home: Path) -> dict[Path, tuple[bytes, int, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for path in home.rglob("*")
        if path.is_file()
    }


def _guardian_fixture(tmp_path: Path, *, parent: int = 2, kind: str = "guardian") -> Path:
    home = tmp_path / "home"
    path = home / "sessions" / f"rollout-2026-06-01T00-00-00-{_id(4).value}.jsonl"
    source = {"subagent": {"other": kind}}
    metadata = {
        "id": str(_id(4).value),
        "source": source,
        "parent_thread_id": str(_id(parent).value),
        "history_mode": "legacy",
        "model_provider": "openai",
    }
    path.write_text(json.dumps({"type": "session_meta", "payload": metadata}) + "\n")
    with sqlite3.connect(home / DatabaseKind.STATE) as connection:
        connection.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)",
            (
                str(_id(4).value),
                str(path),
                json.dumps(source),
                None,
                "openai",
                _EPOCH,
                0,
                "legacy",
            ),
        )
    return path


_MAIN = "00000000-0000-7000-8000-000000000002"

_CHILD = "00000000-0000-7000-8000-000000000003"

_GRANDCHILD = "00000000-0000-7000-8000-000000000004"

_FORK = "00000000-0000-7000-8000-000000000005"

_ARCHIVE = "00000000-0000-7000-8000-000000000006"


def _source(home: Path) -> dict[Path, tuple[bytes, int, int, int]]:
    return {
        path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns, path.stat().st_size)
        for path in home.rglob("*")
        if path.is_file()
    }


def _native_home(home: Path) -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures" / "plan_storage.json").read_text())
    for entry in fixture["files"]:
        path = home / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b64decode(entry["base64"]).replace(b"{home}", str(home).encode()))
    for database in fixture["sqlite"]:
        path = home / database["path"]
        with sqlite3.connect(path) as connection:
            objects = database["objects"]
            for object_ in objects:
                if object_["type"] == "table" and object_["name"] != "sqlite_sequence":
                    connection.execute(object_["sql"])
            for table, rows in database["rows"].items():
                if table == "sqlite_sequence":
                    continue
                for row in rows:
                    connection.execute(
                        f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})",
                        tuple(_cell(value, home) for value in row),
                    )
            for object_ in objects:
                if object_["type"] != "table":
                    connection.execute(object_["sql"])


def _cell(value: object, home: Path) -> object:
    if isinstance(value, str):
        return value.replace("{home}", str(home))
    if isinstance(value, dict):
        return b64decode(value["blobBase64"])
    return value


_A = "11111111-1111-4111-8111-111111111111"
_B = "22222222-2222-4222-8222-222222222222"
_C = "33333333-3333-4333-8333-333333333333"


def _catalogue_home(home: Path) -> Path:
    (home / "sessions").mkdir()
    for identifier in (_A, _B, _C):
        (home / "sessions" / f"rollout-2026-06-01T00-00-00-{identifier}.jsonl").write_bytes(
            b"not history JSON"
        )
    database = home / "state_5.sqlite"
    profile = json.loads(
        (Path(__file__).parent / "fixtures/inventory/native-state-profile.json").read_text()
    )
    with sqlite3.connect(database) as connection:
        for item in profile["objects"]:
            if item["name"] in {"threads", "thread_spawn_edges"}:
                connection.execute(item["sql"])
        columns = profile["threads"]["columns"]
        for identifier, archived in ((_A, 0), (_B, 1), (_C, 0)):
            values = list(profile["threads"]["rows"][0])
            for key, value in {
                "id": identifier,
                "archived": archived,
                "rollout_path": str(
                    home / "sessions" / f"rollout-2026-06-01T00-00-00-{identifier}.jsonl"
                ),
            }.items():
                values[columns.index(key)] = value
            marks = ",".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO threads ({','.join(columns)}) VALUES ({marks})",
                values,
            )
        connection.execute("INSERT INTO thread_spawn_edges VALUES (?,?,'active')", (_A, _B))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE threads SET title='Visible title',cwd='/project',updated_at_ms=1234567890000"
        )
    return database


def _review_manifest(
    identifiers: tuple[ThreadId, ...], stored_bytes: int = 900
) -> list[dict[str, object]]:
    """Build detached exact transport values for adapter-only confirmation tests."""
    targets = []
    for ordinal, identifier in enumerate(sorted(identifiers), 1):
        path = f"/synthetic/sessions/rollout-2026-06-01T00-00-00-{identifier.value}.jsonl"
        targets.append(
            {
                "thread_id": str(identifier.value),
                "current_rollout_path": path,
                "current_rollout_id": str(identifier.value),
                "source": "cli",
                "agent_role": None,
                "provider": "openai",
                "updated_at_ms": 0,
                "archived": False,
                "parent_relation": {"kind": "root"},
                "rollouts": [
                    {
                        "rollout_id": str(identifier.value),
                        "representation": "plain",
                        "rollout_path": path,
                        "physical_identity": {
                            "dev": 1,
                            "ino": ordinal,
                            "size": stored_bytes,
                            "mtime_ns": 0,
                            "ctime_ns": 0,
                        },
                        "header_sha256": [0] * 32,
                        "history_mode": "legacy",
                        "history_prefix": None,
                        "logical_fork": None,
                    }
                ],
            }
        )
    return targets


def _stored_rollout(owner: ThreadId, stored_bytes: int) -> StoredRollout:
    """Detached canonical physical stat used by catalogue adapter fixtures."""
    return StoredRollout(
        OwnershipEdge(owner, RolloutId(owner.value)),
        Path(f"/synthetic/sessions/rollout-2026-06-01T00-00-00-{owner.value}.jsonl"),
        PhysicalRepresentation.PLAIN,
        FileStatIdentity(1, owner.value.int & ((1 << 64) - 1), stored_bytes, 0, 0),
    )
