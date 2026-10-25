# Copyright (c) 2026
# ruff: noqa: D103, S101, PLR2004
"""Actual MCP/native integration; appender proves cooperating file-writer continuity only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from codex_session_gc.cli.console_tasks import (
    Intent,
    PreviewMode,
    apply_intent,
    confirmed_intent,
    parse_cutoff,
    preview,
)
from codex_session_gc.domain import ThreadId
from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.recovery import LaunchStatus, inspect
from tests.mcp_native_fixture import (
    CHILD,
    OTHER,
    ROOT,
    logical_rows,
    make_guardian_home,
    make_home,
    retained_provider,
)


@pytest.fixture
def native_helper() -> Path:
    value = os.environ.get("GC_MCP_NATIVE_HELPER")
    if value is None:
        pytest.skip("Set GC_MCP_NATIVE_HELPER to run the real qualified native MCP witness")
    path = Path(value).resolve(strict=True)
    assert hashlib.sha256(path.read_bytes()).digest() == load_closed_work_contract().binary_sha256
    return path


@asynccontextmanager
async def server(home: Path, helper: Path, receipts: Path) -> AsyncIterator[ClientSession]:
    parameters = StdioServerParameters(
        command=sys.executable,
        env=dict(os.environ),
        args=[
            "-m",
            "codex_session_gc.cli.mcp_server",
            "--home",
            str(home),
            "--helper",
            str(helper),
            "--receipt-dir",
            str(receipts),
        ],
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await session.call_tool(name, arguments)
    assert not result.is_error, result
    content = result.content[0]
    assert isinstance(content, TextContent)
    value = json.loads(content.text)
    assert isinstance(value, dict)
    return value


async def prepare(session: ClientSession) -> dict[str, Any]:
    catalogue = await call(session, "query_catalogue", {})
    result = await call(
        session,
        "prepare_cleanup",
        {"snapshot_id": catalogue["snapshot_id"], "thread_ids": [CHILD], "cutoff": "2030-01-01"},
    )
    assert result["report"]["roots"] == [ROOT]
    assert result["report"]["deletions"] == [CHILD]
    declaration = result["confirmation"]
    assert isinstance(declaration, dict)
    return declaration


def assert_selected_effects(
    before: dict[str, dict[str, list[tuple[Any, ...]]]],
    after: dict[str, dict[str, list[tuple[Any, ...]]]],
    *,
    selected_deleted: bool = True,
    selected_root: bool = False,
) -> None:
    # Independent oracle for this explicit fixture's declared state and queue effects.
    expected = {name: dict(tables) for name, tables in before.items()}
    state = expected["state_5.sqlite"]
    if selected_deleted:
        state["threads"] = [row for row in state["threads"] if row[0] != CHILD]
        state["thread_spawn_edges"] = [
            row for row in state["thread_spawn_edges"] if CHILD not in row[:2]
        ]
    if selected_deleted:
        state["guardian_review_feedback"] = [
            row for row in state["guardian_review_feedback"] if row[1] != CHILD
        ]
        state["thread_read_receipts"] = [
            row for row in state["thread_read_receipts"] if row[0] != CHILD
        ]
    if selected_root:
        board = expected["agent_message_board_1.sqlite"]
        board["deleted_boards"] = sorted([*board["deleted_boards"], (CHILD,)], key=repr)
    # The declared native queue_bookkeeping normalizes its empty high-water mark.
    # Preserve every other sequence/table row, including the initially absent entry case.
    queue = expected["queue_1.sqlite"]
    assert queue["queued_thread_revisions"] == []
    queue["sqlite_sequence"] = sorted(
        [row for row in queue["sqlite_sequence"] if row[0] != "queued_thread_revisions"]
        + [("queued_thread_revisions", 0)],
        key=repr,
    )
    assert after == expected


def assert_native_confirmed(result: dict[str, Any], home: Path, target: Path) -> None:
    native = result["roots"][0]["nativeReceipt"]
    assert native["status"] == "confirmed"
    assert native["removed_rollouts"] == [str(target)]
    assert set(native["committed_databases"]) == {str(path) for path in home.glob("*.sqlite")}


_APPENDER = """
import fcntl,json,os,sys
from pathlib import Path
home,path=map(Path,sys.argv[1:3]); identity=sys.argv[3]
locks=home/'thread-writer-locks'; locks.mkdir(exist_ok=True)
with (locks/'.coordination.lock').open('a+') as coordination:
 fcntl.flock(coordination,fcntl.LOCK_EX)
 writer=(locks/(identity+'.lock')).open('a+')
 fcntl.flock(writer,fcntl.LOCK_EX|fcntl.LOCK_NB)
print('ready',flush=True)
with path.open('a') as stream:
 for command in sys.stdin:
  if command.strip()=='exit': break
  stream.write(json.dumps({'timestamp':'2026-06-01T00:00:00Z','type':'response_item',
   'payload':{'type':'message','role':'user','content':[
    {'type':'input_text','text':'independent writer continues'}]}})+'\\n')
  stream.flush(); os.fsync(stream.fileno()); print('appended',flush=True)
writer.close()
"""


def test_native_confirmed_effects_writer_continuity_and_restart_duplicate(
    tmp_path: Path, native_helper: Path
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    paths = make_home(home)
    before = logical_rows(home)
    retained = paths[ROOT].read_bytes()

    async def check() -> None:
        peer = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _APPENDER,
            str(home),
            str(paths[OTHER]),
            OTHER,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert peer.stdout is not None
        assert peer.stdin is not None
        try:
            assert await peer.stdout.readline() == b"ready\n"
            async with server(home, native_helper, receipts) as session:
                declaration = await prepare(session)
                applying = asyncio.create_task(call(session, "apply_cleanup", declaration))
                peer.stdin.write(b"append\n")
                await peer.stdin.drain()
                assert await peer.stdout.readline() == b"appended\n"
                result = await applying
                assert result["outcome"] == "confirmed", result
                assert_native_confirmed(result, home, paths[CHILD])
                peer.stdin.write(b"append\n")
                await peer.stdin.drain()
                assert await peer.stdout.readline() == b"appended\n"
            assert not paths[CHILD].exists()
            assert paths[ROOT].read_bytes() == retained
            assert paths[OTHER].read_text().count("independent writer continues") == 2
            after = logical_rows(home)
            assert_selected_effects(before, after)
            # A new server has no process-local declaration cache. Saved receipt is authoritative.
            async with server(home, native_helper, receipts) as session:
                status = await call(
                    session, "cleanup_status", {"operation_id": declaration["operation_id"]}
                )
                assert status["outcome"] == "confirmed", status
                duplicate = await call(session, "apply_cleanup", declaration)
                assert duplicate["outcome"] == "confirmed", duplicate
                assert duplicate["dispatch_replayed"] is False
                await call(
                    session, "reconcile_cleanup", {"operation_id": declaration["operation_id"]}
                )
            assert logical_rows(home) == after
            assert not paths[CHILD].exists()
        finally:
            if peer.returncode is None:
                peer.stdin.write(b"exit\n")
                await peer.stdin.drain()
                try:
                    await asyncio.wait_for(peer.wait(), 5)
                except TimeoutError:
                    peer.kill()
                    await peer.wait()
            assert peer.returncode == 0

    asyncio.run(asyncio.wait_for(check(), 90))


def test_native_retained_provider_refuses_without_logical_effects(
    tmp_path: Path, native_helper: Path
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    paths = make_home(home)

    async def check() -> None:
        async with server(home, native_helper, receipts) as session:
            declaration = await prepare(session)
            retained_provider(paths[OTHER], paths[CHILD])
            before = logical_rows(home)
            files = {key: path.read_bytes() for key, path in paths.items()}
            result = await call(session, "apply_cleanup", declaration)
            assert result["outcome"] == "refused", result
            assert result["roots"] == []
            assert "manifest changed" in result["detail"]
            status = await call(
                session, "cleanup_status", {"operation_id": declaration["operation_id"]}
            )
            assert status == result
            assert status["outcome"] == "refused"
            assert status["state"] == "completed_without_receipt"
            assert not (receipts / f"{declaration['operation_id']}.sqlite").exists()
            assert logical_rows(home) == before
            assert {key: path.read_bytes() for key, path in paths.items()} == files

    asyncio.run(asyncio.wait_for(check(), 60))


_SQL_READER = """
import sqlite3,sys
connection=sqlite3.connect(sys.argv[1])
connection.execute('BEGIN')
connection.execute('SELECT * FROM threads').fetchall()
print('ready',flush=True)
sys.stdin.readline()
connection.rollback()
connection.close()
"""


def test_native_commit_lock_unknown_observation_never_replays(
    tmp_path: Path, native_helper: Path
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    paths = make_home(home)
    before = logical_rows(home)

    async def check() -> None:
        async with server(home, native_helper, receipts) as session:
            declaration = await prepare(session)
            # A separate process is essential: inventory closes descriptors in the server.
            # A rollback-journal reader allows DELETE and blocks the final state COMMIT.
            reader = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                _SQL_READER,
                str(home / "state_5.sqlite"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert reader.stdout is not None
            assert reader.stdin is not None
            try:
                assert await reader.stdout.readline() == b"ready\n"
                result = await call(session, "apply_cleanup", declaration)
                assert result["outcome"] == "unknown", result
                native = result["roots"][0]["nativeReceipt"]
                assert native["status"] == "unknown"
                assert native["removed_rollouts"] == [str(paths[CHILD])]
                assert set(native["committed_databases"]) == {
                    str(path) for path in home.glob("*.sqlite") if path.name != "state_5.sqlite"
                }
            finally:
                reader.stdin.write(b"exit\n")
                await reader.stdin.drain()
                try:
                    await asyncio.wait_for(reader.wait(), 5)
                except TimeoutError:
                    reader.kill()
                    await reader.wait()
                assert reader.returncode == 0
            assert not paths[CHILD].exists(), "native must reach unlink before the blocked commit"
            assert_selected_effects(before, logical_rows(home), selected_deleted=False)
        after_files = {key: path.read_bytes() for key, path in paths.items() if path.exists()}
        after = logical_rows(home)
        async with server(home, native_helper, receipts) as session:
            operation = {"operation_id": declaration["operation_id"]}
            status = await call(session, "cleanup_status", operation)
            assert status["outcome"] == "unknown", status
            for name, arguments in (
                ("reconcile_cleanup", operation),
                ("apply_cleanup", declaration),
                ("cleanup_status", operation),
            ):
                observed = await call(session, name, arguments)
                assert observed["dispatch_replayed"] is False
        assert logical_rows(home) == after
        assert {
            key: path.read_bytes() for key, path in paths.items() if path.exists()
        } == after_files

    asyncio.run(asyncio.wait_for(check(), 90))


def test_ui_apply_intent_deletes_only_explicit_child_with_real_helper(
    tmp_path: Path, native_helper: Path
) -> None:
    home = tmp_path / "home"
    paths = make_home(home)
    before = logical_rows(home)
    retained = {identifier: paths[identifier].read_bytes() for identifier in (ROOT, OTHER)}
    receipt = tmp_path / "ui-receipt.sqlite"
    original = Intent(
        home, parse_cutoff("2030-01-01"), (ThreadId.parse(ROOT),), (ThreadId.parse(CHILD),)
    )
    reviewed = confirmed_intent(original, preview(original, mode=PreviewMode.CONFIRM))
    result, status = apply_intent(reviewed, native_helper, receipt)
    assert status == 0
    assert result["outcome"] == "confirmed", result
    assert len(result["roots"]) == 1
    assert result["roots"][0]["root"] == ROOT
    assert result["roots"][0]["selectedThreads"] == [CHILD]
    assert_native_confirmed(result, home, paths[CHILD])
    (saved,) = inspect(receipt)
    assert len(saved.units) == 1
    assert json.loads(saved.units[0].unit.evidence.data)["deletion_scope"] == [CHILD]
    request = saved.units[0].request
    assert request is not None
    assert json.loads(request.data)["deletion_scope"] == [CHILD]
    assert {
        identifier: path.read_bytes() for identifier, path in paths.items() if path.exists()
    } == retained
    assert set(home.glob("sessions/*.jsonl")) == {paths[ROOT], paths[OTHER]}
    assert_selected_effects(before, logical_rows(home))


@asynccontextmanager
async def parent_appender(home: Path, path: Path) -> AsyncIterator[None]:
    peer = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _APPENDER,
        str(home),
        str(path),
        ROOT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert peer.stdout is not None
    assert peer.stdin is not None
    try:
        assert await peer.stdout.readline() == b"ready\n"
        peer.stdin.write(b"append\n")
        await peer.stdin.drain()
        assert await peer.stdout.readline() == b"appended\n"
        yield
        peer.stdin.write(b"append\n")
        await peer.stdin.drain()
        assert await peer.stdout.readline() == b"appended\n"
    finally:
        if peer.returncode is None:
            peer.stdin.write(b"exit\n")
            await peer.stdin.drain()
            try:
                await asyncio.wait_for(peer.wait(), 5)
            except TimeoutError:
                peer.kill()
                await peer.wait()
        assert peer.returncode == 0


def assert_guardian_receipt(receipt: Path) -> None:
    (stored,) = inspect(receipt)
    assert len(stored.launches) == 1
    assert stored.launches[0].status is LaunchStatus.CLOSED
    assert stored.lifecycle_unresolved is False
    (unit,) = stored.units
    assert unit.request is not None
    request = json.loads(unit.request.data)
    assert request["deletion_scope"] == [CHILD]
    board = next(
        bound
        for bound in request["databases"]
        if Path(bound["path"]).name == "agent_message_board_1.sqlite"
    )
    assert board["board_tombstone"] == CHILD
    assert request["targets"][0]["parent_relation"] == {
        "kind": "guardian_association",
        "associated_parent_thread_id": ROOT,
    }
    assert json.loads(unit.unit.evidence.data)["guardian_associations"] == [
        {"guardian_thread_id": CHILD, "associated_parent_thread_id": ROOT}
    ]


def test_guardian_sdk_exact_effects_association_restart_and_parent_writer(
    tmp_path: Path, native_helper: Path
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    paths = make_guardian_home(home)
    before = logical_rows(home)
    parent_before, other_before = paths[ROOT].read_bytes(), paths[OTHER].read_bytes()

    async def check() -> None:
        async with parent_appender(home, paths[ROOT]):
            async with server(home, native_helper, receipts) as session:
                catalogue = await call(session, "query_catalogue", {})
                child = next(row for row in catalogue["histories"] if row["id"] == CHILD)
                filtered = await call(
                    session,
                    "query_catalogue",
                    {
                        "snapshot_id": catalogue["snapshot_id"],
                        "cwd": child["cwd"],
                        "archive_state": child["archive_state"],
                        "scope": "guardians",
                    },
                )
                assert [row["id"] for row in filtered["histories"]] == [CHILD]
                prepared = await call(
                    session,
                    "prepare_cleanup",
                    {
                        "snapshot_id": catalogue["snapshot_id"],
                        "thread_ids": [CHILD],
                        "cutoff": "2030-01-01",
                    },
                )
                declaration = prepared["confirmation"]
                assert prepared["report"]["roots"] == [CHILD]
                assert prepared["report"]["deletions"] == [CHILD]
                assert prepared["report"]["guardian_associations"] == [
                    {"guardian_thread_id": CHILD, "associated_parent_thread_id": ROOT}
                ]
                result = await call(session, "apply_cleanup", declaration)
                assert result["outcome"] == "confirmed", result
                assert_native_confirmed(result, home, paths[CHILD])
            receipt = receipts / f"{declaration['operation_id']}.sqlite"
            assert_guardian_receipt(receipt)
            (stored,) = inspect(receipt)
            saved_declaration = json.loads(stored.start.evidence.data)["operation_declaration"]
            assert {key: saved_declaration[key] for key in declaration} == declaration
            after = logical_rows(home)
            assert_selected_effects(before, after, selected_root=True)
            async with server(home, native_helper, receipts) as session:
                for name, arguments in (
                    ("cleanup_status", {"operation_id": declaration["operation_id"]}),
                    ("reconcile_cleanup", {"operation_id": declaration["operation_id"]}),
                    ("apply_cleanup", declaration),
                ):
                    observed = await call(session, name, arguments)
                    assert observed["outcome"] == "confirmed", observed
                    assert observed["dispatch_replayed"] is False
            assert_guardian_receipt(receipt)
            assert logical_rows(home) == after
        assert not paths[CHILD].exists()
        assert paths[ROOT].read_bytes().startswith(parent_before)
        assert paths[ROOT].read_text().count("independent writer continues") == 2
        assert paths[OTHER].read_bytes() == other_before

    asyncio.run(asyncio.wait_for(check(), 90))


def test_guardian_ui_reviewed_apply_exact_effects_with_parent_writer(
    tmp_path: Path, native_helper: Path
) -> None:
    home = tmp_path / "home"
    paths = make_guardian_home(home)
    before = logical_rows(home)
    parent_before, other_before = paths[ROOT].read_bytes(), paths[OTHER].read_bytes()
    receipt = tmp_path / "guardian-ui.sqlite"
    original = Intent(
        home, parse_cutoff("2030-01-01"), (ThreadId.parse(CHILD),), (ThreadId.parse(CHILD),)
    )
    reviewed = confirmed_intent(original, preview(original, mode=PreviewMode.CONFIRM))
    assert reviewed.reviewed_guardian_associations is not None
    (association,) = reviewed.reviewed_guardian_associations
    assert str(association.associated_parent_thread_id.value) == ROOT

    async def check() -> None:
        async with parent_appender(home, paths[ROOT]):
            result, status = await asyncio.to_thread(apply_intent, reviewed, native_helper, receipt)
            assert status == 0
            assert result["outcome"] == "confirmed", result
            assert_native_confirmed(result, home, paths[CHILD])
            assert_guardian_receipt(receipt)
            assert_selected_effects(before, logical_rows(home), selected_root=True)
        assert not paths[CHILD].exists()
        assert paths[ROOT].read_bytes().startswith(parent_before)
        assert paths[ROOT].read_text().count("independent writer continues") == 2
        assert paths[OTHER].read_bytes() == other_before

    asyncio.run(asyncio.wait_for(check(), 60))
