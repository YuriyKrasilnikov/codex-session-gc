# Copyright (c) 2026
# ruff: noqa: S101, ASYNC240, ASYNC110
"""Actual transport interruption while native deletion awaits an independent SQL reader.

Readiness is observable: selected rollout already absent, original native child
registered and alive, and original receipt still nonterminal. No implementation
hook or replacement native engine participates in these witnesses.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from codex_session_gc.recovery import LaunchStatus, inspect
from tests.mcp_native_fixture import CHILD, logical_rows, make_home
from tests.test_mcp_native import (
    _SQL_READER,
    assert_selected_effects,
    call,
    native_helper,
    prepare,
    server,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mcp import ClientSession

__all__ = ["native_helper"]


@asynccontextmanager
async def reader(home: Path) -> AsyncIterator[asyncio.subprocess.Process]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-c",
        _SQL_READER,
        str(home / "state_5.sqlite"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"ready\n"
        yield process
    finally:
        if process.returncode is None:
            process.stdin.write(b"release\n")
            await process.stdin.drain()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        assert process.returncode == 0


async def interrupted_launch(receipts: Path, declaration: dict[str, Any], target: Path) -> int:
    with anyio.fail_after(30):
        while target.exists():
            await anyio.sleep(0.005)
    (stored,) = inspect(receipts / f"{declaration['operation_id']}.sqlite")
    assert len(stored.launches) == 1
    registration = stored.launches[0].registration
    assert registration is not None
    assert stored.units[0].request is not None
    assert stored.units[0].terminal_kind is None, "native completed before interruption readiness"
    identity = json.loads(registration.incarnation.data)
    pid = identity["pid"]
    assert isinstance(pid, int)
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    assert stat[0] not in {"Z", "X"}, "native child must still be running"
    assert int(stat[19]) == identity["start_ticks"], "recorded child incarnation must match"
    return pid


async def release_reader(process: asyncio.subprocess.Process) -> None:
    assert process.stdin is not None
    process.stdin.write(b"release\n")
    await process.stdin.drain()
    await asyncio.wait_for(process.wait(), 5)
    assert process.returncode == 0


async def settled(session: ClientSession, declaration: dict[str, Any]) -> dict[str, Any]:
    with anyio.fail_after(30):
        while True:
            status = await call(
                session, "cleanup_status", {"operation_id": declaration["operation_id"]}
            )
            if status.get("state") == "saved" and status.get("lifecycle_unresolved") is False:
                return status
            await anyio.sleep(0.02)


async def assert_restarted_observation(
    home: Path,
    helper: Path,
    receipts: Path,
    declaration: dict[str, Any],
    before: dict[str, Any],
) -> None:
    after = logical_rows(home)
    async with server(home, helper, receipts) as session:
        operation = {"operation_id": declaration["operation_id"]}
        status = await settled(session, declaration)
        assert status["outcome"] in {"confirmed", "unknown"}, status
        if status["outcome"] == "confirmed":
            assert_selected_effects(before, after)
        else:
            assert_selected_effects(before, after, selected_deleted=False)
        for name, arguments in (
            ("reconcile_cleanup", operation),
            ("apply_cleanup", {"declaration": declaration}),
        ):
            observed = await call(session, name, arguments)
            assert observed["dispatch_replayed"] is False
    assert logical_rows(home) == after
    (stored,) = inspect(receipts / f"{declaration['operation_id']}.sqlite")
    assert len(stored.launches) == 1
    assert stored.launches[0].status is LaunchStatus.CLOSED
    assert stored.lifecycle_unresolved is False


def test_sdk_cancelled_destructive_request_joins_original_native_launch(
    tmp_path: Path, native_helper: Path
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    paths = make_home(home)
    before = logical_rows(home)

    async def check() -> None:
        with anyio.fail_after(100):
            async with server(home, native_helper, receipts) as session:
                declaration = await prepare(session)
                scopes: list[anyio.CancelScope] = []
                cancelled = anyio.Event()

                async def applying() -> None:
                    with anyio.CancelScope() as scope:
                        scopes.append(scope)
                        await session.call_tool("apply_cleanup", {"declaration": declaration})
                    assert scope.cancelled_caught
                    cancelled.set()

                async with reader(home) as blocker, anyio.create_task_group() as tasks:
                    tasks.start_soon(applying)
                    child_pid = await interrupted_launch(receipts, declaration, paths[CHILD])
                    scopes[0].cancel()
                    # The SDK flushes its courtesy cancellation before this waiter settles.
                    with anyio.fail_after(2):
                        await cancelled.wait()
                    await release_reader(blocker)
                    status = await settled(session, declaration)
                    assert status["outcome"] in {"confirmed", "unknown"}, status
            assert not Path(f"/proc/{child_pid}").exists()
            assert not paths[CHILD].exists()
            await assert_restarted_observation(home, native_helper, receipts, declaration, before)

    anyio.run(check)


async def raw_request(
    process: asyncio.subprocess.Process,
    identifier: int,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(
        (
            json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
            + "\n"
        ).encode()
    )
    await process.stdin.drain()
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), 30)
        assert line, "server closed before its expected response"
        value = json.loads(line)
        if value.get("id") == identifier:
            assert "error" not in value, value
            result = value["result"]
            assert isinstance(result, dict)
            return result


def raw_tool(value: dict[str, Any]) -> dict[str, Any]:
    assert not value.get("isError"), value
    result = json.loads(value["content"][0]["text"])
    assert isinstance(result, dict)
    return result


def test_genuine_stdin_eof_joins_inflight_backend_and_reaps_native_child(
    tmp_path: Path, native_helper: Path
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    paths = make_home(home)
    before = logical_rows(home)

    async def check() -> None:
        # Raw client controls real stdin EOF; SDK stdio_client imposes a2s kill policy.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            "codex_session_gc.cli.mcp_server",
            "--home",
            str(home),
            "--helper",
            str(native_helper),
            "--receipt-dir",
            str(receipts),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdin is not None
        try:
            with anyio.fail_after(100):
                await raw_request(
                    process,
                    1,
                    "initialize",
                    {
                        "protocolVersion": LATEST_HANDSHAKE_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "bounded-eof-witness", "version": "1"},
                    },
                )
                process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await process.stdin.drain()
                catalogue = raw_tool(
                    await raw_request(
                        process, 2, "tools/call", {"name": "query_catalogue", "arguments": {}}
                    )
                )
                prepared = raw_tool(
                    await raw_request(
                        process,
                        3,
                        "tools/call",
                        {
                            "name": "prepare_cleanup",
                            "arguments": {
                                "snapshot_id": catalogue["snapshot_id"],
                                "thread_ids": [CHILD],
                                "cutoff": "2030-01-01",
                            },
                        },
                    )
                )
                declaration = prepared["declaration"]
                async with reader(home) as blocker:
                    process.stdin.write(
                        (
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": 4,
                                    "method": "tools/call",
                                    "params": {
                                        "name": "apply_cleanup",
                                        "arguments": {"declaration": declaration},
                                    },
                                }
                            )
                            + "\n"
                        ).encode()
                    )
                    await process.stdin.drain()
                    child_pid = await interrupted_launch(receipts, declaration, paths[CHILD])
                    process.stdin.close()
                    await process.stdin.wait_closed()
                    await release_reader(blocker)
                await asyncio.wait_for(process.wait(), 15)
                assert process.returncode == 0
                assert not Path(f"/proc/{child_pid}").exists()
                assert not paths[CHILD].exists()
                await assert_restarted_observation(
                    home, native_helper, receipts, declaration, before
                )
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if process.stdout is not None:
                await process.stdout.read()
            if process.stderr is not None:
                await process.stderr.read()

    anyio.run(check)
