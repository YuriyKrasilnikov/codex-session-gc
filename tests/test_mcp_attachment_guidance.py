# Copyright (c) 2026
# ruff: noqa: D100, D103, FBT001, S101, TC003

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.mcp_native_fixture import CHILD, OTHER, ROOT, attach_history, make_home


@pytest.mark.parametrize("read_only", [False, True])
def test_stdio_guidance_does_not_select_attached_history(tmp_path: Path, read_only: bool) -> None:
    home = tmp_path / "home"
    paths = make_home(home)
    attach_history(paths, CHILD, ROOT, standalone=True)
    args = ["-m", "codex_session_gc.cli.mcp_server", "--home", str(home)]
    if read_only:
        args.append("--read-only")
    params = StdioServerParameters(command=sys.executable, args=args, env=dict(os.environ))

    async def check() -> None:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert "cleanup_status" in tools
            assert ("reconcile_cleanup" in tools) is not read_only
            snapshot = json.loads((await client.call_tool("query_catalogue", {})).content[0].text)[
                "snapshot_id"
            ]
            response = await client.call_tool(
                "prepare_cleanup", {"snapshot_id": snapshot, "thread_ids": [ROOT]}
            )
            assert not response.is_error
            prepared = json.loads(response.content[0].text)
            guidance = prepared["report"]["attachment_cleanup"]
            assert guidance["home_identity"] == [home.stat().st_dev, home.stat().st_ino]
            assert guidance["truncated"] is False
            assert guidance["sources"][0]["id"] == ROOT
            assert guidance["sources"][0]["children"][0]["id"] == CHILD
            assert guidance["sources"][0]["children"][0]["selected"] is False
            assert prepared["report"]["deletions"] == []
            assert "confirmation" not in prepared
            mixed = await client.call_tool(
                "prepare_cleanup",
                {"snapshot_id": snapshot, "thread_ids": [ROOT, OTHER]},
            )
            assert not mixed.is_error
            result = json.loads(mixed.content[0].text)
            assert result["report"]["deletions"] == [OTHER]
            assert ("confirmation" in result) is not read_only
            assert paths[CHILD].is_file()

    asyncio.run(asyncio.wait_for(check(), timeout=45))


@pytest.mark.parametrize("read_only", [False, True])
def test_stdio_rejects_replaced_home_and_restart_binds_new_home(
    tmp_path: Path, read_only: bool
) -> None:
    home = tmp_path / "home"
    make_home(home)
    args = ["-m", "codex_session_gc.cli.mcp_server", "--home", str(home)]
    if read_only:
        args.append("--read-only")
    params = StdioServerParameters(command=sys.executable, args=args, env=dict(os.environ))

    async def check() -> None:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            snapshot = json.loads((await client.call_tool("query_catalogue", {})).content[0].text)[
                "snapshot_id"
            ]
            home.rename(tmp_path / "old-home")
            make_home(home)
            rejected = await client.call_tool(
                "prepare_cleanup", {"snapshot_id": snapshot, "thread_ids": [CHILD]}
            )
            assert rejected.is_error
            refreshed = json.loads(
                (await client.call_tool("query_catalogue", {"refresh": True})).content[0].text
            )
            assert (
                await client.call_tool(
                    "prepare_cleanup",
                    {"snapshot_id": refreshed["snapshot_id"], "thread_ids": [CHILD]},
                )
            ).is_error
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            snapshot = json.loads((await client.call_tool("query_catalogue", {})).content[0].text)[
                "snapshot_id"
            ]
            prepared = await client.call_tool(
                "prepare_cleanup", {"snapshot_id": snapshot, "thread_ids": [CHILD]}
            )
            assert not prepared.is_error
            result = json.loads(prepared.content[0].text)
            if read_only:
                assert result["can_apply_via_mcp"] is False
                assert "confirmation" not in result

    # This scenario includes two server starts and cold preview workers.
    asyncio.run(asyncio.wait_for(check(), timeout=90))
