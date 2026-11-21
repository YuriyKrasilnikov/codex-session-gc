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
from tests.mcp_native_fixture import CHILD, OTHER, ROOT, make_home, retained_provider
from tests.test_textual_console import CUTOFF

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


_UNINDEXED = "44444444-4444-4444-8444-444444444444"


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


def _used_by(detail: dict[str, Any]) -> str:
    (witness,) = [item for item in detail["items"] if item.get("interpretation") == "used_by"]
    return str(witness["source_subject"]["thread_id"])


async def _provider_stdio(home: Path) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "codex_session_gc", "mcp", "--home", str(home), "--read-only"],
        env=dict(os.environ),
    )
    async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
        await session.initialize()
        observed = (await session.call_tool("storage_overview")).structured_content
        assert observed is not None
        snapshot = {"storage_snapshot_id": observed["storage_snapshot_id"]}
        objects = (await session.call_tool("storage_objects", snapshot)).structured_content
        assert objects is not None
        row = next(row for row in objects["items"] if CHILD in row["display_path"])
        ordinary = (await session.call_tool("storage_diagnostics", snapshot)).structured_content
        assert ordinary is not None
        assert ordinary["coverage"]["providers"] == "not_requested"
        opted_in = {**snapshot, "providers": True}
        measured = (await session.call_tool("storage_diagnostics", opted_in)).structured_content
        assert measured is not None
        assert measured["diagnostic_id"] != ordinary["diagnostic_id"]
        assert (await session.call_tool("storage_diagnostics", opted_in)).structured_content == (
            measured
        )
        evidence = await session.call_tool(
            "storage_explain",
            {"diagnostic_id": measured["diagnostic_id"], "subject_id": row["object_id"]},
        )
        assert evidence.structured_content is not None
        assert _used_by(evidence.structured_content) == OTHER


async def _provider_terminal(home: Path) -> None:
    browser = SessionApp(home, CUTOFF, None, home.parent / "receipts")
    async with browser.run_test(size=(100, 35)) as pilot:
        assert browser.load_task is not None
        await browser.load_task
        await pilot.pause()
        assert await pilot.click("#storage")
        await pilot.pause()
        screen = browser.screen
        assert isinstance(screen, StorageScreen)
        await browser.storage.wait_idle()
        await pilot.pause()
        table = screen.query_one(DataTable)
        provider = next(
            row for row in range(table.row_count) if CHILD in str(table.get_row_at(row)[0])
        )
        table.move_cursor(row=provider)
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        diagnostic = browser.screen
        assert isinstance(diagnostic, StorageDiagnosticScreen)
        await browser.storage.wait_idle()
        await pilot.pause()
        assert await pilot.click("#diagnostic-providers")
        await pilot.pause()
        await browser.storage.wait_idle()
        await pilot.pause()
        rows = diagnostic.query_one(DataTable)
        assert any(
            "used_by" in str(rows.get_row_at(index)[0]) and OTHER in str(rows.get_row_at(index)[0])
            for index in range(rows.row_count)
        )


def test_provider_inspection_is_explicit_and_matches_three_frontends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinguish opt-in header reads and show the same consumer through every adapter."""
    home = tmp_path / "home"
    paths = make_home(home)
    retained_provider(paths[OTHER], paths[CHILD])
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", (OTHER,))
    unindexed = paths[OTHER].with_name(paths[OTHER].name.replace(OTHER, _UNINDEXED))
    records = paths[OTHER].read_text().splitlines(keepends=True)
    header = json.loads(records[0])
    header["payload"].update(id=_UNINDEXED, session_id=_UNINDEXED)
    header["payload"]["history_base"].update(
        thread_id=OTHER, end_byte_offset=paths[OTHER].stat().st_size
    )
    records[0] = json.dumps(header) + "\n"
    unindexed.write_text("".join(records))
    plan = [
        "plan",
        "--online",
        "--home",
        str(home),
        "--cutoff",
        "2030-01-01T00:00:00Z",
        "--closed-work",
        ROOT,
        "--select-thread",
        CHILD,
        "--json",
    ]
    assert main(plan) == 0
    before = json.loads(capsys.readouterr().out)
    assert CHILD not in before["deletions"]
    command = ["storage-diagnostics", "--home", str(home), "--rollout", CHILD, "--json"]
    assert main(command) == 0
    ordinary = json.loads(capsys.readouterr().out)
    assert ordinary["coverage"]["providers"] == "not_requested"
    assert main([*command, "--providers"]) == 0
    assert _used_by(json.loads(capsys.readouterr().out)["page"]) == OTHER
    upstream = ["storage-diagnostics", "--home", str(home), "--rollout", OTHER, "--json"]
    assert main([*upstream, "--providers"]) == 0
    assert _used_by(json.loads(capsys.readouterr().out)["page"]) == _UNINDEXED
    assert main(plan) == 0
    assert json.loads(capsys.readouterr().out) == before
    asyncio.run(asyncio.wait_for(_provider_stdio(home), 90))
    asyncio.run(asyncio.wait_for(_provider_terminal(home), 45))
