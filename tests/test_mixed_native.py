# Copyright (c) 2026
# ruff: noqa: S101, PLR2004, ASYNC110

"""Qualified mixed-format physical effects through actual public cleanup adapters."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from compression.zstd import compress, decompress
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

_SCALED_SELECTED_OWNERS = 64


def _mixed_home(
    home: Path, representation: str, mode: str, *, guardian: bool
) -> tuple[dict[str, Path], Path]:
    paths = make_guardian_home(home) if guardian else make_home(home)
    old = _mixed_rollouts(home, paths, CHILD, representation, mode, old_rollout=str(UUID(int=9)))
    return paths, old


def _mixed_rollouts(  # noqa: PLR0913
    home: Path,
    paths: dict[str, Path],
    owner: str,
    representation: str,
    mode: str,
    *,
    old_rollout: str,
) -> Path:
    """Construct current and historical representations for one synthetic owner."""
    lines = paths[owner].read_bytes().splitlines(keepends=True)
    header = json.loads(lines[0])
    header["payload"]["history_mode"] = mode
    if mode == "paginated":
        header["payload"]["history_base"] = {
            "thread_id": ROOT,
            "end_ordinal_exclusive": 1,
            "end_byte_offset": len(paths[ROOT].read_bytes().splitlines(keepends=True)[0]),
        }
    raw = json.dumps(header).encode() + b"\n" + b"".join(lines[1:])
    old = paths[owner].with_name(
        paths[owner].name.replace(f"{owner}.jsonl", f"{owner}_{old_rollout}.jsonl")
    )
    historic = json.loads(lines[0])
    historic["payload"].update(
        source="exec", model_provider="historical-provider", agent_role="historical-role"
    )
    old.write_bytes(json.dumps(historic).encode() + b"\n" + b"".join(lines[1:]))
    paths[owner].write_bytes(raw)
    if representation == "zstd":
        encoded = paths[owner].with_name(paths[owner].name + ".zst")
        encoded.write_bytes(compress(raw))
        paths[owner].unlink()
        paths[owner] = encoded
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET history_mode=? WHERE id=?", (mode, owner))
    return old


def _scaled_mixed_home(
    home: Path,
) -> tuple[dict[str, Path], dict[str, tuple[Path, Path]], str]:
    """Create a bounded mixed store with independently known ownership and retention."""
    paths = make_home(home)
    selected = (
        CHILD,
        *(str(UUID(int=1000 + number)) for number in range(_SCALED_SELECTED_OWNERS - 1)),
    )
    unselected = str(UUID(int=9000))
    template = paths[CHILD].read_text().splitlines()
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET title='scaled selected 00' WHERE id=?", (CHILD,))
        for number, owner in enumerate((*selected[1:], unselected), 1):
            path = home / "sessions" / f"rollout-2026-06-01T00-00-00-{owner}.jsonl"
            header = json.loads(template[0])
            header["payload"].update(id=owner, session_id=owner)
            path.write_text(json.dumps(header) + "\n" + "\n".join(template[1:]) + "\n")
            paths[owner] = path
            connection.execute(
                "INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,"
                "cwd,title,sandbox_policy,approval_mode,created_at_ms,updated_at_ms,history_mode) "
                "SELECT ?,?,created_at,updated_at,source,model_provider,cwd,?,sandbox_policy,"
                "approval_mode,created_at_ms,updated_at_ms,history_mode FROM threads WHERE id=?",
                (
                    owner,
                    str(path),
                    "retained agent" if owner == unselected else f"scaled selected {number:02}",
                    CHILD,
                ),
            )
            connection.execute("INSERT INTO thread_spawn_edges VALUES(?,?,'closed')", (ROOT, owner))
        for owner in (ROOT, *selected, OTHER, unselected):
            connection.execute(
                "INSERT INTO guardian_review_feedback VALUES(?,?,?)",
                ("feedback:" + owner, owner, b"\x00\xffopaque:" + owner.encode()),
            )
            connection.execute(
                "INSERT INTO thread_read_receipts VALUES(?,?,?,?)",
                (owner, "unread:" + owner, "revision:" + owner, None),
            )
    owned = {}
    for number, owner in enumerate(selected):
        representation = "zstd" if number % 2 else "plain"
        mode = "paginated" if number % 4 >= 2 else "legacy"
        old = _mixed_rollouts(
            home, paths, owner, representation, mode, old_rollout=str(UUID(int=2000 + number))
        )
        owned[owner] = (paths[owner], old)
    # The retained consumer has a readable prefix from the retained provider.
    # This independent byte/ordinal witness does not assert a live Core resume.
    lines = paths[OTHER].read_bytes().splitlines(keepends=True)
    header = json.loads(lines[0])
    provider = paths[ROOT].read_bytes()
    header["payload"].update(
        history_mode="paginated",
        history_base={
            "thread_id": ROOT,
            "end_ordinal_exclusive": 2,
            "end_byte_offset": len(provider),
        },
    )
    body = json.loads(lines[1])
    body["ordinal"] = 2
    paths[OTHER].write_text(json.dumps(header) + "\n" + json.dumps(body) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", (OTHER,))
    return paths, owned, unselected


def _retained_history(paths: dict[str, Path]) -> tuple[dict[str, Any], ...]:
    """Decode the explicit retained prefix and body independently of cleanup code."""
    provider = paths[ROOT].read_bytes()
    consumer = [json.loads(line) for line in paths[OTHER].read_bytes().splitlines()]
    prefix = consumer[0]["payload"]["history_base"]
    assert prefix["thread_id"] == ROOT
    boundary = prefix["end_byte_offset"]
    assert boundary == len(provider)
    assert provider[:boundary].endswith(b"\n")
    inherited = [json.loads(line) for line in provider[:boundary].splitlines()]
    assert len(inherited) == prefix["end_ordinal_exclusive"] == 2
    result = (*inherited, *consumer[1:])
    assert [line["ordinal"] for line in result] == [0, 1, 2]
    assert all(line["type"] == "response_item" for line in result[1:])
    assert all(line["payload"]["content"][0]["text"] == "synthetic history" for line in result[1:])
    return result


def _assert_scaled_formats(owned: dict[str, tuple[Path, Path]]) -> None:
    """Decode every small synthetic object and establish the intended format mix."""
    formats: Counter[tuple[str, str]] = Counter()
    captured = 0
    for owner, (current, old) in owned.items():
        representation = "zstd" if current.name.endswith(".zst") else "plain"
        raw = current.read_bytes()
        decoded = decompress(raw) if representation == "zstd" else raw
        header = json.loads(decoded.splitlines()[0])["payload"]
        historical = json.loads(old.read_bytes().splitlines()[0])["payload"]
        assert header["id"] == historical["id"] == owner
        assert historical["history_mode"] == "legacy"
        assert historical["source"] == "exec"
        formats[representation, header["history_mode"]] += 1
        captured += len(decoded) + old.stat().st_size
    assert len(owned) == _SCALED_SELECTED_OWNERS
    assert formats == {
        (representation, mode): _SCALED_SELECTED_OWNERS // 4
        for representation in ("plain", "zstd")
        for mode in ("legacy", "paginated")
    }
    assert captured < 1024**2


def test_scaled_mixed_owners_filter_confirm_and_remove_only_declared_histories(  # noqa: PLR0915
    tmp_path: Path, native_helper: Path
) -> None:
    """Actual helper effects cover a bounded mixed store; retained bytes stay readable."""
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    paths, owned, unselected = _scaled_mixed_home(home)
    _assert_scaled_formats(owned)
    selected = set(owned)
    declared_paths = {str(path) for owner_paths in owned.values() for path in owner_paths}
    before = logical_rows(home)
    retained = {owner: paths[owner].read_bytes() for owner in (ROOT, OTHER, unselected)}
    readable = _retained_history(paths)
    expected_sizes = {
        owner: sum(path.stat().st_size for path in items) for owner, items in owned.items()
    }

    async def check() -> Path:
        async with server(home, native_helper, receipts) as session:
            catalogue = await call(
                session,
                "query_catalogue",
                {
                    "text": "scaled selected",
                    "scope": "agents",
                    "order_target": "history",
                    "sort": "stored_bytes",
                    "direction": "desc",
                    "limit": _SCALED_SELECTED_OWNERS,
                },
            )
            assert catalogue["total_matches"] == _SCALED_SELECTED_OWNERS
            assert catalogue["next_offset"] is None
            assert {row["id"] for row in catalogue["histories"]} == selected
            assert all(row["physical_complete"] for row in catalogue["histories"])
            assert {
                row["id"]: row["stored_bytes"] for row in catalogue["histories"]
            } == expected_sizes
            prepared = await call(
                session,
                "prepare_cleanup",
                {
                    "snapshot_id": catalogue["snapshot_id"],
                    "thread_ids": [row["id"] for row in catalogue["histories"]],
                    "cutoff": "2030-01-01",
                },
            )
            assert len(json.dumps(prepared).encode()) < 1024**2
            assert prepared["report"]["roots"] == [ROOT]
            assert set(prepared["report"]["deletions"]) == selected
            manifest = prepared["report"]["physical_manifest"]
            assert len(manifest) == _SCALED_SELECTED_OWNERS
            assert {
                item["rollout_path"] for target in manifest for item in target["rollouts"]
            } == declared_paths
            # The confirmation round-trip transports strings, never stat numbers.
            confirmation = json.loads(json.dumps(prepared["confirmation"]))
            assert set(confirmation) == {"operation_id", "digest"}
            assert all(isinstance(value, str) for value in confirmation.values())
            result = await call(session, "apply_cleanup", confirmation)
            assert result["outcome"] == "confirmed", result
            assert len(result["roots"]) == 1
            native = result["roots"][0]["nativeReceipt"]
            assert set(native["removed_rollouts"]) == declared_paths
            operation = confirmation["operation_id"]
        async with server(home, native_helper, receipts) as session:
            status = await call(session, "cleanup_status", {"operation_id": operation})
            assert status["outcome"] == "confirmed", status
            reconciled = await call(session, "reconcile_cleanup", {"operation_id": operation})
            assert reconciled["outcome"] == "confirmed", reconciled
            assert reconciled["dispatch_replayed"] is False
        return receipts / f"{operation}.sqlite"

    receipt = asyncio.run(asyncio.wait_for(check(), 120))
    assert all(not path.exists() for items in owned.values() for path in items)
    assert {owner: paths[owner].read_bytes() for owner in retained} == retained
    assert _retained_history(paths) == readable
    assert {str(path) for path in (home / "sessions").glob("*")} == {
        str(paths[owner]) for owner in retained
    }
    expected = {name: dict(tables) for name, tables in before.items()}
    state = expected["state_5.sqlite"]
    state["threads"] = [row for row in state["threads"] if row[0] not in selected]
    state["thread_spawn_edges"] = [
        row for row in state["thread_spawn_edges"] if not selected.intersection(row[:2])
    ]
    state["guardian_review_feedback"] = [
        row for row in state["guardian_review_feedback"] if row[1] not in selected
    ]
    state["thread_read_receipts"] = [
        row for row in state["thread_read_receipts"] if row[0] not in selected
    ]
    queue = expected["queue_1.sqlite"]
    assert queue["queued_thread_revisions"] == []
    queue["sqlite_sequence"] = sorted(
        [row for row in queue["sqlite_sequence"] if row[0] != "queued_thread_revisions"]
        + [("queued_thread_revisions", 0)],
        key=repr,
    )
    assert logical_rows(home) == expected
    (saved,) = inspect(receipt)
    assert not saved.lifecycle_unresolved
    assert all(launch.status is LaunchStatus.CLOSED for launch in saved.launches)
    assert len(saved.units) == 1
    request = saved.units[0].request
    assert request is not None
    assert len(request.data) < 1024**2
    declaration = json.loads(request.data)
    assert set(declaration["deletion_scope"]) == selected
    assert {
        item["rollout_path"] for target in declaration["targets"] for item in target["rollouts"]
    } == declared_paths


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
