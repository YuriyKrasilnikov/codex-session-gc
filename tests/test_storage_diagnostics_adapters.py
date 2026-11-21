# Copyright (c) 2026
# ruff: noqa: S101

"""Agreement and lifetime of the public storage diagnostic views."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from typing import TYPE_CHECKING

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from textual.widgets import DataTable, Static

from codex_session_gc.cli.main import INVALID_INPUT_EXIT, main
from codex_session_gc.cli.storage_diagnostic_screen import StorageDiagnosticScreen
from codex_session_gc.cli.storage_screen import StorageScreen
from codex_session_gc.cli.textual_console import SessionApp
from tests.mcp_native_fixture import CHILD, ROOT, make_home
from tests.test_textual_console import CUTOFF

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def _rows(page: dict[str, Any]) -> set[tuple[str, str | None, tuple[str, ...]]]:
    return {(item["kind"], item["thread_id"], tuple(item["reasons"])) for item in page["items"]}


def _fixture(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, dict[str, Any]]:
    home = tmp_path / "home"
    make_home(home)
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("DELETE FROM threads WHERE id=?", (ROOT,))
    cmd = ["storage-diagnostics", "--home", str(home), "--json"]
    assert main(cmd) == 0
    expected = json.loads(capsys.readouterr().out)
    assert any(row[1] == ROOT for row in _rows(expected["page"]))
    assert main([*cmd, "--thread", CHILD]) == 0
    thread = json.loads(capsys.readouterr().out)
    assert thread["page"]["status"] == "observed"
    assert any(
        (item["source_subject"] or {}).get("thread_id") == ROOT for item in thread["page"]["items"]
    )
    assert main(["storage-diagnostics", "--home", str(home), "--thread", CHILD]) == 0
    assert ROOT in capsys.readouterr().out
    assert main([*cmd, "--thread", "00000000-0000-4000-8000-000000000021"]) == 0
    assert json.loads(capsys.readouterr().out)["page"]["status"] == "subject_not_observed"
    with pytest.raises(SystemExit) as invalid:
        main([*cmd, "--rollout", "invalid"])
    assert invalid.value.code == INVALID_INPUT_EXIT
    return home, expected["page"]


async def _stdio(home: Path, expected: dict[str, Any]) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_session_gc", "mcp", "--home", str(home), "--read-only"],
        env=dict(os.environ),
    )
    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert {"storage_diagnostics", "storage_diagnostic_subjects", "storage_explain"} <= (
            tools.keys()
        )
        observed = await session.call_tool("storage_overview")
        assert not observed.is_error
        assert observed.structured_content is not None
        begun = await session.call_tool(
            "storage_diagnostics",
            {"storage_snapshot_id": observed.structured_content["storage_snapshot_id"]},
        )
        assert not begun.is_error
        assert begun.structured_content is not None
        identity = {"diagnostic_id": begun.structured_content["diagnostic_id"]}
        page = await session.call_tool("storage_diagnostic_subjects", identity)
        assert not page.is_error
        assert page.structured_content is not None
        assert _rows(page.structured_content) == _rows(expected)
        row = next(item for item in page.structured_content["items"] if item["kind"] == "endpoint")
        evidence = await session.call_tool(
            "storage_explain", {**identity, "subject_id": row["subject_id"]}
        )
        assert not evidence.is_error
        assert evidence.structured_content is not None
        assert evidence.structured_content["subject"]["thread_id"] == ROOT
        assert any(
            (item["target_subject"] or {}).get("thread_id") == CHILD
            for item in evidence.structured_content["items"]
        )
        changed = await session.call_tool("storage_overview", {"refresh": True})
        assert not changed.is_error
        stale = await session.call_tool("storage_diagnostic_subjects", identity)
        assert stale.is_error


async def _terminal(home: Path, expected: dict[str, Any]) -> None:
    browser = SessionApp(home, CUTOFF, None, home.parent / "receipts")
    async with browser.run_test(size=(150, 45)) as pilot:
        await pilot.pause()
        if browser.load_task is not None:
            await browser.load_task
        await pilot.click("#storage")
        await pilot.pause()
        screen = browser.screen
        assert isinstance(screen, StorageScreen)
        await browser.storage.wait_idle()
        await pilot.pause()
        await pilot.click("#storage-problems")
        await pilot.pause()
        diagnostic = browser.screen
        assert isinstance(diagnostic, StorageDiagnosticScreen)
        await browser.storage.wait_idle()
        await pilot.pause()
        table = diagnostic.query_one(DataTable)
        assert table.row_count == len(expected["items"])
        endpoint = next(
            row
            for row in range(table.row_count)
            if "endpoint" in str(table.get_row_at(row)[0]) and ROOT in str(table.get_row_at(row)[0])
        )
        table.move_cursor(row=endpoint)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        connection = next(
            row for row in range(table.row_count) if CHILD in str(table.get_row_at(row)[0])
        )
        table.move_cursor(row=connection)
        await pilot.press("enter")
        await pilot.pause()
        assert CHILD in str(diagnostic.query_one("#diagnostic-summary", Static).content)
        await pilot.press("escape")
        await pilot.pause()
        assert browser.screen is screen


def test_diagnostic_evidence_matches_cli_stdio_and_textual(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Present the same missing-parent witness and keep each page generation-bound."""
    home, expected = _fixture(tmp_path, capsys)
    asyncio.run(asyncio.wait_for(_stdio(home, expected), 90))
    asyncio.run(asyncio.wait_for(_terminal(home, expected), 45))
