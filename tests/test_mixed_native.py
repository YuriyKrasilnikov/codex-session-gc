# Copyright (c) 2026
# ruff: noqa: S101, PLR2004, ASYNC110

"""Qualified mixed-format physical effects through actual public cleanup adapters."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from compression.zstd import compress
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from textual.widgets import DataTable, Input, Select

from codex_session_gc.cli.console_tasks import parse_cutoff
from codex_session_gc.cli.main import main
from codex_session_gc.cli.textual_console import SessionApp
from codex_session_gc.domain import ArchiveState
from codex_session_gc.recovery import LaunchStatus, inspect
from tests.mcp_native_fixture import CHILD, OTHER, ROOT, logical_rows, make_guardian_home, make_home
from tests.test_mcp_native import assert_selected_effects, call, native_helper, server

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

__all__ = ["native_helper"]


def _mixed_home(
    home: Path, representation: str, mode: str, *, guardian: bool
) -> tuple[dict[str, Path], Path]:
    paths = make_guardian_home(home) if guardian else make_home(home)
    lines = paths[CHILD].read_bytes().splitlines(keepends=True)
    header = json.loads(lines[0])
    header["payload"]["history_mode"] = mode
    if mode == "paginated":
        header["payload"]["history_base"] = {
            "thread_id": ROOT,
            "end_ordinal_exclusive": 1,
            "end_byte_offset": len(paths[ROOT].read_bytes().splitlines(keepends=True)[0]),
        }
    raw = json.dumps(header).encode() + b"\n" + b"".join(lines[1:])
    old = paths[CHILD].with_name(
        paths[CHILD].name.replace(f"{CHILD}.jsonl", f"{CHILD}_{UUID(int=9)}.jsonl")
    )
    historic = json.loads(lines[0])
    historic["payload"].update(
        source="exec", model_provider="historical-provider", agent_role="historical-role"
    )
    old.write_bytes(json.dumps(historic).encode() + b"\n" + b"".join(lines[1:]))
    paths[CHILD].write_bytes(raw)
    if representation == "zstd":
        encoded = paths[CHILD].with_name(paths[CHILD].name + ".zst")
        encoded.write_bytes(compress(raw))
        paths[CHILD].unlink()
        paths[CHILD] = encoded
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET history_mode=? WHERE id=?", (mode, CHILD))
    return paths, old


@pytest.mark.parametrize(
    ("adapter", "representation", "mode", "guardian"),
    [
        ("mcp", "plain", "legacy", False),
        ("mcp", "plain", "paginated", False),
        ("mcp", "zstd", "legacy", False),
        ("mcp", "zstd", "paginated", False),
        ("cli", "zstd", "paginated", True),
        ("textual", "zstd", "paginated", True),
    ],
)
def test_actual_adapters_remove_all_owned_revert_formats_and_preserve_provider(  # noqa: PLR0913, PLR0915, PLR0917
    tmp_path: Path,
    native_helper: Path,
    capsys: pytest.CaptureFixture[str],
    adapter: str,
    representation: str,
    mode: str,
    *,
    guardian: bool,
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    paths, old = _mixed_home(home, representation, mode, guardian=guardian)
    before = logical_rows(home)
    retained = {identifier: paths[identifier].read_bytes() for identifier in (ROOT, OTHER)}
    owned = {str(paths[CHILD]), str(old)}
    expected_size = paths[CHILD].stat().st_size + old.stat().st_size
    root = CHILD if guardian else ROOT
    receipt = tmp_path / "cli-receipt.sqlite"
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        (stored_cwd,) = connection.execute(
            "SELECT cwd FROM threads WHERE id=?", (CHILD,)
        ).fetchone()

    async def mcp() -> Path:
        async with server(home, native_helper, receipts) as session:
            catalogue = await call(session, "query_catalogue", {})
            row = next(item for item in catalogue["histories"] if item["id"] == CHILD)
            assert row["stored_bytes"] == expected_size
            prepared = await call(
                session,
                "prepare_cleanup",
                {
                    "snapshot_id": catalogue["snapshot_id"],
                    "thread_ids": [CHILD],
                    "cutoff": "2030-01-01",
                },
            )
            assert prepared["report"]["sizes"] == [expected_size]
            declaration = prepared["confirmation"]
            assert len(prepared["report"]["physical_manifest"][0]["rollouts"]) == 2
            result = await call(session, "apply_cleanup", declaration)
            assert result["outcome"] == "confirmed", result
            assert set(result["roots"][0]["nativeReceipt"]["removed_rollouts"]) == owned
            operation = declaration["operation_id"]
        async with server(home, native_helper, receipts) as session:
            observed = await call(session, "reconcile_cleanup", {"operation_id": operation})
            assert observed["outcome"] == "confirmed", observed
            assert observed["dispatch_replayed"] is False
        return receipts / f"{operation}.sqlite"

    async def textual() -> Path:
        browser = SessionApp(home, parse_cutoff("2030-01-01"), native_helper, receipts)
        async with browser.run_test(size=(120, 40)) as pilot:
            while not browser.query_one(DataTable).row_count:
                await pilot.pause(0.01)
            browser.query_one("#cwd", Select).value = stored_cwd
            browser.query_one("#archive", Select).value = ArchiveState.NOT_ARCHIVED
            browser.query_one("#scope", Select).value = "guardians"
            browser.query_one("#search", Input).value = CHILD
            await pilot.pause()
            await pilot.click("#select")
            assert {str(identifier.value) for identifier in browser.selected} == {CHILD}
            await pilot.click("#preview")
            while not browser.query_one("#confirm", Input).display:
                await pilot.pause(0.01)
            browser.query_one("#confirm", Input).value = "DELETE"
            await pilot.click("#execute")
            while browser.completed_result is None:
                await asyncio.sleep(0.01)
        assert browser.completed_result is not None
        assert browser.completed_result[1] == 0, browser.completed_result
        assert browser.receipt is not None
        return browser.receipt

    if adapter == "mcp":
        receipt = asyncio.run(asyncio.wait_for(mcp(), 90))
    elif adapter == "textual":
        receipt = asyncio.run(asyncio.wait_for(textual(), 90))
    else:
        assert (
            main(
                [
                    "apply",
                    "--online",
                    "--home",
                    str(home),
                    "--cutoff",
                    "2030-01-01T00:00:00Z",
                    "--closed-work",
                    root,
                    "--select-thread",
                    CHILD,
                    "--helper",
                    str(native_helper),
                    "--receipt",
                    str(receipt),
                    "--json",
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["outcome"] == "confirmed", result
        assert set(result["roots"][0]["nativeReceipt"]["removed_rollouts"]) == owned
    assert not paths[CHILD].exists()
    assert not old.exists()
    assert {identifier: paths[identifier].read_bytes() for identifier in (ROOT, OTHER)} == retained
    assert_selected_effects(before, logical_rows(home), selected_root=guardian)
    (saved,) = inspect(receipt)
    assert not saved.lifecycle_unresolved
    assert all(launch.status is LaunchStatus.CLOSED for launch in saved.launches)
    request = saved.units[0].request
    assert request is not None
    payload: dict[str, Any] = json.loads(request.data)
    assert payload["version"] == 6
    assert payload["deletion_scope"] == [CHILD]
    assert {item["rollout_path"] for item in payload["targets"][0]["rollouts"]} == owned
