# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004, PT011, ASYNC240

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import mcp.client.session
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from codex_session_gc import diagnostics
from codex_session_gc.cli import console_tasks, mcp_server
from codex_session_gc.cli.console_tasks import (
    Intent,
    PreviewMode,
    apply_intent,
    confirmed_intent,
    preview,
)
from codex_session_gc.cli.mcp_capability import McpCapability
from codex_session_gc.cli.mcp_server import CatalogueService
from codex_session_gc.domain import GuardianAssociation, LastActivity, ThreadId, UtcCutoff
from codex_session_gc.execution import closed_work_operation, confirmed_operation
from codex_session_gc.execution.closed_work_apply import apply_closed_work
from codex_session_gc.execution.closed_work_reconcile import FinalState, reconcile
from codex_session_gc.execution.closed_work_request import ClosedWorkFacts, review_targets
from codex_session_gc.execution.confirmed_declaration import (
    Declaration,
    ReviewedSelection,
    create_declaration,
)
from codex_session_gc.execution.physical_manifest import decode_targets
from codex_session_gc.inventory.query import CatalogueIndex, CatalogueQuery, CatalogueResult
from codex_session_gc.planning.closed_work import select_closed_work
from codex_session_gc.recovery import AttemptId, TerminalKind, inspect
from tests.current_storage_fixture import _A, _B, _LIMITS, _id
from tests.current_storage_fixture import _catalogue_home as _home
from tests.mcp_native_fixture import CHILD, ROOT, logical_rows, make_home
from tests.test_catalogue_query import (
    _A as QUERY_ROOT,
)
from tests.test_catalogue_query import (
    _B as QUERY_AGENT,
)
from tests.test_catalogue_query import (
    _C as QUERY_GUARDIAN,
)
from tests.test_catalogue_query import (
    _CWD as QUERY_CWD,
)
from tests.test_catalogue_query import (
    _E as QUERY_UNKNOWN,
)
from tests.test_catalogue_query import _mixed_index
from tests.test_closed_work_apply import _setup
from tests.test_closed_work_reconcile import _delete_selected

if TYPE_CHECKING:
    from mcp.types import CallToolResult


def _manifest(
    facts: ClosedWorkFacts, roots: tuple[str, ...], selected: tuple[str, ...], home: Path
) -> object:
    selection = select_closed_work(
        facts.snapshot,
        facts.cutoff,
        frozenset(ThreadId.parse(owner) for owner in roots),
        selected_threads=frozenset(ThreadId.parse(owner) for owner in selected),
        physical_inventory=facts.physical_inventory,
    )
    selection = replace(
        selection, conditional_candidates=frozenset(ThreadId.parse(owner) for owner in selected)
    )
    return [
        target.model_dump(mode="json") for target in review_targets(facts, selection, home, _LIMITS)
    ]


def _data(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error
    return json.loads(result.content[0].text)  # type: ignore[union-attr,no-any-return]


async def _assert_storage_pages(session: ClientSession) -> None:
    overview = _data(await session.call_tool("storage_overview", {}))
    snapshot = overview["storage_snapshot_id"]
    assert snapshot.startswith("storage/")
    assert overview["totals"]["unique_objects"] > 0
    page = _data(
        await session.call_tool("storage_objects", {"storage_snapshot_id": snapshot, "limit": 1})
    )
    assert page["storage_snapshot_id"] == snapshot
    assert len(page["items"]) == 1
    assert page["items"][0]["display_path"].startswith("sessions/")
    assert _data(await session.call_tool("storage_overview", {}))["storage_snapshot_id"] == snapshot
    diagnostic = _data(
        await session.call_tool("storage_diagnostics", {"storage_snapshot_id": snapshot})
    )
    identity = {"diagnostic_id": diagnostic["diagnostic_id"]}
    subjects = _data(await session.call_tool("storage_diagnostic_subjects", identity))
    assert subjects["diagnostic_id"] == identity["diagnostic_id"]
    object_id = page["items"][0]["object_id"]
    detail = _data(
        await session.call_tool("storage_explain", {**identity, "subject_id": object_id})
    )
    assert detail["subject"]["object_id"] == object_id


def test_cache_exact_pages_refresh_and_bounded_details(tmp_path: Path) -> None:
    _home(tmp_path)

    async def check() -> None:
        service = CatalogueService(tmp_path)
        first = await service.query_catalogue(limit=1)
        snapshot = first["snapshot_id"]
        # Changing disk after observation does not change the cached pages.
        (tmp_path / "state_5.sqlite").unlink()
        second = await service.query_catalogue(snapshot_id=snapshot, offset=1, limit=1)
        assert second["snapshot_id"] == snapshot
        assert second["histories"][0]["id"] != first["histories"][0]["id"]
        details = await service.session_details(snapshot, _B, limit=1)
        assert details["member_count"] == 2
        assert len(details["context"]) == 1
        assert details["permission"] is False
        refreshed = await service.query_catalogue(refresh=True)
        assert refreshed["snapshot_id"] != snapshot
        assert refreshed["total_matches"] == 0
        with pytest.raises(ValueError):
            await service.query_catalogue(snapshot_id=snapshot)
        with pytest.raises(ValueError):
            await service.query_catalogue(limit=101)
        with pytest.raises(ValueError):
            await service.prepare_cleanup(refreshed["snapshot_id"], [], "2030-01-01")

    asyncio.run(asyncio.wait_for(check(), timeout=45))


def test_real_sdk_stdio_current_handshake_tools_and_preview(tmp_path: Path) -> None:
    home = tmp_path / "home"
    make_home(home)

    async def check() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "codex_session_gc.cli.mcp_server", "--home", str(home)],
            env=dict(os.environ),
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.protocol_version == mcp.client.session.LATEST_HANDSHAKE_VERSION
            assert session.protocol_version == initialized.protocol_version
            await _assert_storage_pages(session)
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "storage_overview",
                "storage_objects",
                "storage_diagnostics",
                "storage_diagnostic_subjects",
                "storage_explain",
                "query_catalogue",
                "catalogue_summary",
                "session_details",
                "preview_policy",
                "prepare_cleanup",
                "apply_cleanup",
                "cleanup_status",
                "reconcile_cleanup",
                "compatibility_status",
                "keep_status",
                "keep_set",
                "keep_clear",
            }
            summary = _data(await session.call_tool("catalogue_summary", {"limit": 1}))
            if summary["projects"]:
                row = summary["projects"][0]
                drill = _data(
                    await session.call_tool(
                        "query_catalogue",
                        {"snapshot_id": summary["snapshot_id"], "project_key": row["project_key"]},
                    )
                )
                assert drill["snapshot_id"] == summary["snapshot_id"]
            queried = _data(await session.call_tool("query_catalogue", {"limit": 1}))
            assert len(queried["histories"]) == 1
            snapshot = queried["snapshot_id"]
            ordered = _data(
                await session.call_tool(
                    "query_catalogue",
                    {
                        "snapshot_id": snapshot,
                        "scope": "agents",
                        "order_target": "history",
                        "sort": "stored_bytes",
                        "direction": "asc",
                        "limit": 1,
                    },
                )
            )
            assert [row["id"] for row in ordered["histories"]] == [CHILD]
            assert ordered["order"] == {
                "target": "history",
                "field": "stored_bytes",
                "direction": "asc",
            }
            invalid_order = await session.call_tool(
                "query_catalogue", {"order_target": "history", "sort": "total"}
            )
            assert invalid_order.is_error
            prepared = _data(
                await session.call_tool(
                    "prepare_cleanup",
                    {"snapshot_id": snapshot, "thread_ids": [CHILD], "cutoff": "2030-01-01"},
                )
            )
            for cutoff_argument in ({}, {"cutoff": None}):
                exact = _data(
                    await session.call_tool(
                        "prepare_cleanup",
                        {"snapshot_id": snapshot, "thread_ids": [CHILD], **cutoff_argument},
                    )
                )
                assert exact["report"]["cutoff"] is None
                assert exact["report"]["selected"] == [CHILD]
                assert exact["report"]["deletions"] == [CHILD]
                assert exact["confirmation"]
            assert (await session.call_tool("prepare_cleanup", {"snapshot_id": snapshot})).is_error
            assert prepared["report"]["selected"] == [CHILD]
            assert prepared["report"]["deletions"] == [CHILD]
            assert prepared["conditional"]
            assert prepared["can_apply_via_mcp"]
            assert len(prepared["preview_sha256"]) == 64
            assert set(prepared["confirmation"]) == {"operation_id", "digest"}
            apply_schema = next(
                tool.input_schema for tool in tools.tools if tool.name == "apply_cleanup"
            )
            assert set(apply_schema["properties"]) == {"operation_id", "digest"}
            assert all(value["type"] == "string" for value in apply_schema["properties"].values())
            legacy = await session.call_tool("apply_cleanup", {"declaration": {}})
            assert legacy.is_error
            refused = await session.call_tool("apply", {})
            assert refused.is_error
            bad = await session.call_tool("query_catalogue", {"limit": 100000})
            assert bad.is_error

    asyncio.run(asyncio.wait_for(check(), timeout=45))


@pytest.mark.parametrize(
    "entrypoint", [("codex_session_gc", "mcp"), ("codex_session_gc.cli.mcp_server",)]
)
def test_read_only_sdk_exposes_observation_without_cleanup_operation_or_receipt(
    tmp_path: Path, entrypoint: tuple[str, ...]
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    make_home(home)
    before = logical_rows(home)

    async def check() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                *entrypoint,
                "--home",
                str(home),
                "--receipt-dir",
                str(receipts),
                "--read-only",
            ],
            env=dict(os.environ),
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            await _assert_storage_pages(session)
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "storage_overview",
                "storage_objects",
                "storage_diagnostics",
                "storage_diagnostic_subjects",
                "storage_explain",
                "query_catalogue",
                "catalogue_summary",
                "session_details",
                "preview_policy",
                "prepare_cleanup",
                "cleanup_status",
                "compatibility_status",
                "keep_status",
            }
            policy_tool = next(tool for tool in tools.tools if tool.name == "preview_policy")
            assert policy_tool.annotations is not None
            assert policy_tool.annotations.read_only_hint
            fresh = _data(
                await session.call_tool(
                    "preview_policy",
                    {"rule": {"version": 1, "work_roots": [ROOT]}},
                )
            )
            assert ROOT in fresh["rule_matches"]
            assert "confirmation" not in fresh
            carried = _data(
                await session.call_tool(
                    "preview_policy",
                    {
                        "rule": {"version": 1, "work_roots": [ROOT]},
                        "prior_ids": [ROOT],
                        "prior_cutoff": "2030-01-01T00:00:00Z",
                    },
                )
            )
            assert set(carried["rule_matches"]) <= {ROOT}
            assert "confirmation" not in carried
            diagnostic_tool = next(
                tool for tool in tools.tools if tool.name == "compatibility_status"
            )
            assert "home" not in diagnostic_tool.input_schema.get("properties", {})
            assert "helper" not in diagnostic_tool.input_schema.get("properties", {})
            support = _data(await session.call_tool("compatibility_status", {}))["source_support"]
            assert support["origin"] == "bundled_static_app_data"
            assert support["availability"] == "available"
            assert support["claim_verified"] is False
            assert support["authorizes_operation"] is False
            assert support["producer_origin"] == "not_observed"
            direct = await asyncio.to_thread(diagnostics.inspect_compatibility, home, None)
            assert support == json.loads(json.dumps(asdict(direct.source_support)))
            catalogue = _data(await session.call_tool("query_catalogue", {}))
            snapshot = catalogue["snapshot_id"]
            details = _data(
                await session.call_tool(
                    "session_details", {"snapshot_id": snapshot, "thread_id": ROOT}
                )
            )
            assert details["session"]["id"] == ROOT
            prepared = _data(
                await session.call_tool(
                    "prepare_cleanup",
                    {"snapshot_id": snapshot, "thread_ids": [CHILD], "cutoff": "2030-01-01"},
                )
            )
            assert not prepared["can_apply_via_mcp"]
            assert "declaration" not in prepared
            assert "confirmation" not in prepared
            assert "declaration" not in prepared["report"]
            assert "operation_id" not in prepared
            assert "operation_id" not in prepared["report"]
            assert prepared["report"]["selected"] == [CHILD]
            assert prepared["report"]["deletions"] == [CHILD]
            operation = str(UUID(int=1))
            status = _data(await session.call_tool("cleanup_status", {"operation_id": operation}))
            assert status["state"] == "receipt_absent"
            for name, arguments in (
                ("apply_cleanup", {"declaration": {}}),
                ("reconcile_cleanup", {"operation_id": operation}),
            ):
                assert (await session.call_tool(name, arguments)).is_error
        assert logical_rows(home) == before
        assert not receipts.exists()

    asyncio.run(asyncio.wait_for(check(), timeout=60))


def test_read_only_prepare_allocates_no_operation_and_direct_calls_refuse_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, receipts = tmp_path / "home", tmp_path / "receipts"
    make_home(home)

    async def check() -> None:
        service = CatalogueService(home, receipts=receipts, capability=McpCapability.READ_ONLY)
        listed = await service.query_catalogue()
        monkeypatch.setattr(
            confirmed_operation, "uuid4", lambda: pytest.fail("Allocated an operation")
        )
        monkeypatch.setattr(console_tasks, "uuid4", lambda: pytest.fail("Allocated an operation"))
        prepared = await service.prepare_cleanup(listed["snapshot_id"], [CHILD], "2030-01-01")
        assert not prepared["can_apply_via_mcp"]
        assert "declaration" not in prepared
        assert "confirmation" not in prepared
        assert prepared["report"]["deletions"] == [CHILD]
        with pytest.raises(PermissionError, match="Read-only"):
            await service.apply_cleanup(str(UUID(int=1)), "0" * 64)
        with pytest.raises(PermissionError, match="Read-only"):
            await service.reconcile_cleanup(str(UUID(int=1)))
        assert not receipts.exists()

    asyncio.run(asyncio.wait_for(check(), timeout=45))


def test_read_only_status_reads_existing_cleanup_without_appending_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, _, options, _ = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    monkeypatch.setattr(closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: facts)
    receipts = tmp_path / "receipts"
    operations = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], receipts
    )
    root, child = (str(_id(number).value) for number in (1, 2))
    declaration = operations.prepare(
        (root,),
        (root, child),
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, (root,), (root, child), options["codex_home"]),
    )
    assert operations.apply(declaration)["outcome"] == "confirmed"
    receipt = receipts / f"{declaration.operation_id}.sqlite"
    before = hashlib.sha256(receipt.read_bytes()).digest()

    async def check() -> None:
        service = CatalogueService(
            options["codex_home"], receipts=receipts, capability=McpCapability.READ_ONLY
        )
        status = await service.cleanup_status(declaration.operation_id)
        assert status["outcome"] == "confirmed"
        assert status["operation_id"] == declaration.operation_id
        with pytest.raises(PermissionError, match="Read-only"):
            await service.apply_cleanup(declaration.operation_id, declaration.digest)
        with pytest.raises(PermissionError, match="Read-only"):
            await service.reconcile_cleanup(declaration.operation_id)

    asyncio.run(asyncio.wait_for(check(), timeout=30))
    assert hashlib.sha256(receipt.read_bytes()).digest() == before


def test_sdk_exact_directory_archive_and_source_predicates_share_one_snapshot(
    tmp_path: Path,
) -> None:
    bootstrap = """
import sys
from pathlib import Path
from codex_session_gc.cli import mcp_server
from codex_session_gc.cli.mcp_capability import McpCapability
from tests.test_catalogue_query import _mixed_index
calls=0
def read(home):
    global calls
    calls+=1
    if calls>1: raise RuntimeError('Unexpected catalogue refresh')
    return _mixed_index().catalogue
mcp_server.read_session_catalogue=read
mcp_server.run(Path(sys.argv[1]),capability=McpCapability.READ_ONLY)
"""

    async def check() -> None:
        parameters = StdioServerParameters(
            command=sys.executable, args=["-c", bootstrap, str(tmp_path)], env=dict(os.environ)
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            initial = _data(await session.call_tool("query_catalogue", {}))
            arguments = {
                "snapshot_id": initial["snapshot_id"],
                "cwd": QUERY_CWD,
                "archive_state": "archived",
            }
            for scope, expected in [
                ("agents", {QUERY_AGENT, QUERY_GUARDIAN}),
                ("ordinary_agents", {QUERY_AGENT}),
                ("guardians", {QUERY_GUARDIAN}),
            ]:
                result = _data(
                    await session.call_tool("query_catalogue", {**arguments, "scope": scope})
                )
                assert {row["id"] for row in result["histories"]} == {
                    str(x.value) for x in expected
                }
                if QUERY_AGENT in expected:
                    assert result["groups"][str(QUERY_ROOT.value)]["root"]["id"] == str(
                        QUERY_ROOT.value
                    )
                    assert str(QUERY_ROOT.value) not in {row["id"] for row in result["histories"]}
            literal = _data(
                await session.call_tool(
                    "query_catalogue", {**arguments, "cwd": "/project/Alpha + draft"}
                )
            )
            assert literal["histories"] == []
            incomplete = _data(
                await session.call_tool(
                    "query_catalogue", {**arguments, "archive_state": "unknown", "min_bytes": 50}
                )
            )
            assert [row["id"] for row in incomplete["histories"]] == [str(QUERY_UNKNOWN.value)]
            assert incomplete["histories"][0]["known_stored_bytes"] == 50
            assert incomplete["histories"][0]["stored_bytes"] is None
            refused = await session.call_tool(
                "query_catalogue", {**arguments, "archive_state": "invalid"}
            )
            assert refused.is_error

    asyncio.run(asyncio.wait_for(check(), timeout=60))


def test_actual_adapter_cancellation_reaps_owned_worker_and_removes_copies(tmp_path: Path) -> None:
    _home(tmp_path)
    marker = tmp_path / "worker.json"
    bootstrap = """
import asyncio,json,os,sys
from pathlib import Path
from codex_session_gc import diagnostics
from codex_session_gc.cli import console_tasks, mcp_server
from codex_session_gc.cli.mcp_server import run
original=asyncio.create_subprocess_exec
async def slow_worker(*args,**kwargs):
    directory=args[-1]
    code=("import os,time,json; from pathlib import Path; "
          "Path("+repr(sys.argv[2])+").write_text(json.dumps({'pid':os.getpid(),"
          "'directory':"+repr(directory)+"})); time.sleep(60)")
    return await original(sys.executable,'-c',code,**kwargs)
console_tasks.asyncio.create_subprocess_exec=slow_worker
run(Path(sys.argv[1]))
"""

    async def check() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", bootstrap, str(tmp_path), str(marker)],
            env=dict(os.environ),
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            snapshot = _data(await session.call_tool("query_catalogue", {}))["snapshot_id"]
            preparing = asyncio.create_task(
                session.call_tool(
                    "prepare_cleanup",
                    {"snapshot_id": snapshot, "thread_ids": [_A], "cutoff": "2030-01-01"},
                )
            )
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(0.025)
            assert marker.exists()
            worker = json.loads(marker.read_text())
            preparing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await preparing
            for _ in range(400):
                if (
                    not Path(worker["directory"]).exists()
                    and not Path(f"/proc/{worker['pid']}").exists()
                ):
                    break
                await asyncio.sleep(0.025)
            assert not Path(worker["directory"]).exists()
            assert not Path(f"/proc/{worker['pid']}").exists()
            assert _data(await session.call_tool("query_catalogue", {}))["snapshot_id"] == snapshot

    asyncio.run(asyncio.wait_for(check(), timeout=45))


def test_changed_advisory_still_prepares_exact_fresh_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    make_home(home)

    async def check() -> None:
        service = CatalogueService(home)
        listed = await service.query_catalogue()
        assert service.index is not None
        service.index = CatalogueIndex(replace(service.index.catalogue, source_changed=True))
        child_id = ThreadId.parse(CHILD)
        observed = service.index.entries[child_id]
        path = observed.physical_rollouts[0].path
        with path.open("ab") as stream:
            stream.write(b"\n")
        prepared = await service.prepare_cleanup(listed["snapshot_id"], [CHILD], "2030-01-01")
        fresh = preview(
            Intent(
                home,
                UtcCutoff(datetime.fromisoformat("2030-01-01T00:00:00+00:00")),
                (ThreadId.parse(ROOT),),
                (child_id,),
            )
        )
        assert prepared["report"]["selection_summary"] == fresh["selection_summary"]
        assert prepared["report"]["selection_summary"]["stored_bytes"] == observed.stored_bytes + 1
        assert prepared["source_changed"]
        assert prepared["report"]["selected"] == [CHILD]
        assert prepared["report"]["selection_summary"]["history_count"] == 1
        assert prepared["report"]["selection_summary"]["missing"] == 0
        assert prepared["report"]["deletions"] == [CHILD]
        assert prepared["can_apply_via_mcp"]
        assert set(prepared["confirmation"]) == {"operation_id", "digest"}

    asyncio.run(asyncio.wait_for(check(), timeout=45))


def test_confirmed_operation_concurrent_duplicate_payload_binding_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    monkeypatch.setattr(closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: facts)
    service = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    root = str(_id(1).value)
    roots, selected = (root,), (root, str(_id(2).value))
    declaration = service.prepare(
        roots,
        selected,
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, roots, selected, options["codex_home"]),
    )
    assert service.status(declaration.operation_id)["state"] == "receipt_absent"
    assert service.status(declaration.operation_id)["outcome"] == "unknown"
    with ThreadPoolExecutor(max_workers=2) as workers:
        calls = [workers.submit(service.apply, declaration) for _ in range(2)]
        results = [call.result(timeout=30) for call in calls]
    assert all(result["outcome"] == "confirmed" for result in results), json.dumps(results)
    assert observed.read_text().splitlines() == [root]
    path = tmp_path / "receipts" / f"{declaration.operation_id}.sqlite"
    (saved,) = inspect(path)
    assert str(saved.start.attempt_id.value) == declaration.operation_id
    assert json.loads(saved.start.evidence.data)["operation_declaration"] == declaration.model_dump(
        mode="json"
    )
    restarted = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    monkeypatch.setattr(
        closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: pytest.fail("replayed")
    )
    assert restarted.apply(declaration)["outcome"] == "confirmed"
    assert restarted.reconcile(declaration.operation_id)["outcome"] == "confirmed"
    assert restarted.reconcile(declaration.operation_id)["skipped_roots"] == []
    changed = declaration.model_copy(update={"cutoff": "2031-01-01T00:00:00+00:00"})
    changed = changed.model_copy(update={"digest": changed.calculated_digest()})
    with pytest.raises(ValueError, match="different declaration"):
        restarted.apply(changed)
    assert observed.read_text().splitlines() == [root]


def test_shared_binding_preserves_mcp_bounds_and_one_shot_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    monkeypatch.setattr(closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: facts)
    root, child = (str(_id(number).value) for number in (1, 2))
    metadata = options["codex_home"].stat()
    inputs = ReviewedSelection(
        roots=(root,),
        selected=(root, child),
        cutoff=facts.cutoff.timestamp.isoformat(),
        physical_manifest=decode_targets(
            _manifest(facts, (root,), (root, child), options["codex_home"])
        ),
    )
    operation = UUID("00000000-0000-4000-8000-000000000001")
    declaration = create_declaration(
        inputs,
        operation_id=operation,
        home_identity=(metadata.st_dev, metadata.st_ino),
        helper=options["helper"],
    )
    equivalent = create_declaration(
        replace(inputs, physical_manifest=decode_targets(declaration.physical_manifest)),
        operation_id=operation,
        home_identity=(metadata.st_dev, metadata.st_ino),
        helper=options["helper"],
    )
    assert equivalent == declaration
    request = closed_work_operation.confirmed_execution_request(
        declaration,
        home=options["codex_home"],
        helper=options["helper_executable"],
        receipt=options["journal"],
    )
    assert request.attempt_id == AttemptId(operation)
    assert request.closed_work == (_id(1),)
    assert request.select_thread == (_id(1), _id(2))
    assert request.declaration == declaration.model_dump(mode="json")
    assert request.receipt == options["journal"]
    assert request.home == options["codex_home"]
    assert request.helper == options["helper_executable"]
    assert not options["journal"].exists()
    result, status = closed_work_operation.execute(request)
    assert status == 0, result
    assert observed.read_text().splitlines() == [root]
    (saved,) = inspect(options["journal"])
    assert saved.start.attempt_id == AttemptId(operation)
    assert json.loads(saved.start.evidence.data)["operation_declaration"] == declaration.model_dump(
        mode="json"
    )


@pytest.mark.parametrize(
    "mutation", ["home", "profile", "cutoff", "attempt", "manifest", "guardian"]
)
def test_shared_execution_binding_refuses_changed_inputs_before_receipt_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    helper = options["helper"]
    monkeypatch.setattr(closed_work_operation, "load_closed_work_contract", lambda: helper)
    monkeypatch.setattr(closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: facts)
    service = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    root, child = (str(_id(number).value) for number in (1, 2))
    declaration = service.prepare(
        (root,),
        (root, child),
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, (root,), (root, child), options["codex_home"]),
    )
    request = closed_work_operation.confirmed_execution_request(
        declaration,
        home=options["codex_home"],
        helper=options["helper_executable"],
        receipt=options["journal"],
    )
    if mutation == "home":
        other = tmp_path / "other-home"
        other.mkdir()
        request = replace(request, home=other)
    elif mutation == "profile":
        helper = replace(helper, evidence=replace(helper.evidence, data=b"changed profile"))
    elif mutation == "cutoff":
        request = replace(request, cutoff=UtcCutoff(facts.cutoff.timestamp.replace(year=2031)))
    elif mutation == "attempt":
        request = replace(request, attempt_id=AttemptId(UUID(int=42)))
    elif mutation == "manifest":
        request = replace(request, reviewed_physical_manifest=None)
    else:
        request = replace(
            request, reviewed_guardian_associations=(GuardianAssociation(_id(1), _id(3)),)
        )
    result, status = closed_work_operation.execute(request)
    assert status == 3
    assert result["outcome"] == "refused"
    assert not options["journal"].exists()
    assert not observed.exists()


def test_mcp_replaced_startup_home_refuses_before_receipt_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    service = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    root = str(_id(1).value)
    declaration = service.prepare(
        (root,),
        (root,),
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, (root,), (root,), options["codex_home"]),
    )
    options["codex_home"].rename(tmp_path / "original-home")
    options["codex_home"].mkdir()
    result = service.apply(declaration)
    assert result["outcome"] == "refused"
    assert "identity has changed" in result["detail"]
    assert service.apply(declaration) == result
    assert service.status(declaration.operation_id) == result
    assert not observed.exists()


def test_changed_reviewed_candidate_set_refuses_before_receipt_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reviewed, _, options, observed = _setup(tmp_path)
    fresh = replace(
        reviewed,
        snapshot=replace(
            reviewed.snapshot,
            threads=tuple(
                replace(record, last_activity=LastActivity(reviewed.cutoff.timestamp))
                if record.id == _id(3)
                else record
                for record in reviewed.snapshot.threads
            ),
        ),
    )
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    monkeypatch.setattr(closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: fresh)
    service = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    roots = tuple(str(_id(number).value) for number in (1, 3))
    selected = tuple(str(_id(number).value) for number in (1, 2, 3))
    declaration = service.prepare(
        roots,
        selected,
        reviewed.cutoff.timestamp.isoformat(),
        _manifest(reviewed, roots, selected, options["codex_home"]),
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(service.apply, (declaration, declaration)))
    assert all(result["outcome"] == "refused" for result in results)
    assert all("manifest changed" in result["detail"] for result in results)
    assert service.status(declaration.operation_id) == results[0]
    assert not observed.exists()


def test_confirmed_operation_stale_profile_and_corrupt_claim_never_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    service = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    identifier = "00000000-0000-0000-0000-000000000001"
    declaration = service.prepare(
        (identifier,),
        (identifier,),
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, (identifier,), (identifier,), options["codex_home"]),
    )
    monkeypatch.setattr(
        confirmed_operation,
        "load_closed_work_contract",
        lambda: replace(
            options["helper"], evidence=replace(options["helper"].evidence, data=b"changed")
        ),
    )
    stale = declaration
    restarted = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    assert restarted.apply(declaration)["state"] == "receipt_absent"
    assert not observed.exists()
    refused = service.apply(stale)
    assert refused["outcome"] == "refused"
    assert "identity has changed" in refused["detail"]
    assert service.apply(stale) == refused
    assert service.status(stale.operation_id) == refused
    assert not observed.exists()
    (tmp_path / "receipts").mkdir()
    path = tmp_path / "receipts" / f"{declaration.operation_id}.sqlite"
    path.write_bytes(b"interrupted exclusive journal claim")
    assert service.status(declaration.operation_id)["outcome"] == "unknown"
    # A corrupt exclusive claim is never replaced with a fresh deletion attempt.
    assert service.apply(stale)["outcome"] == "unknown"
    assert not observed.exists()


def test_confirmed_operation_transport_cancellation_joins_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    facts, _, options, _ = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    service = confirmed_operation.ConfirmedOperations(
        options["codex_home"], None, tmp_path / "receipts"
    )
    started, finish, joined = threading.Event(), threading.Event(), threading.Event()

    def backend(_operation_id: str, _digest: str) -> dict[str, Any]:
        started.set()
        assert finish.wait(10)
        joined.set()
        return {"outcome": "unknown"}

    monkeypatch.setattr(service, "apply_reference", backend)

    async def check() -> None:
        # No storage is observed by the bounded synthetic backend.
        root = str(_id(1).value)
        declaration = service.prepare(
            (root,),
            (root,),
            facts.cutoff.timestamp.isoformat(),
            _manifest(facts, (root,), (root,), options["codex_home"]),
        )
        task = asyncio.create_task(
            service.apply_owned(declaration.operation_id, declaration.digest)
        )
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert joined.is_set()

    asyncio.run(asyncio.wait_for(check(), 15))


@pytest.mark.parametrize("mutation", [None, "digest", "unknown_field", "selection", "attempt"])
def test_saved_confirmed_declaration_reconciles_exact_valid_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str | None
) -> None:
    facts, selections, options, observed = _setup(tmp_path, "partial")
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    service = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    root, child = (str(_id(number).value) for number in (1, 2))
    declaration = service.prepare(
        (root,),
        (root, child),
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, (root,), (root, child), options["codex_home"]),
    )
    payload = declaration.model_dump(mode="json")
    if mutation == "digest":
        payload["digest"] = "0" * 64
    elif mutation == "unknown_field":
        payload["helper_path"] = "/arbitrary/helper"
    elif mutation is not None:
        payload.update({"selected": [root]} if mutation == "selection" else {"operation_id": root})
        if mutation == "selection":
            payload["physical_manifest"] = payload["physical_manifest"][:1]
        payload["digest"] = Declaration.model_validate(payload).calculated_digest()
    applied = apply_closed_work(
        facts,
        (selections[0],),
        **options,
        attempt_id=AttemptId(UUID(declaration.operation_id)),
        declaration=payload,
    )
    consumed = observed.read_bytes()
    _delete_selected(options["codex_home"])
    result = reconcile(
        options["journal"],
        applied.attempt_id,
        applied.roots[0].unit_id,
        helper=options["helper"],
        limits=_LIMITS,
    )
    assert result.state is (FinalState.CONFIRMED if mutation is None else FinalState.UNKNOWN)
    assert observed.read_bytes() == consumed


@pytest.mark.parametrize("adapter", ["ui", "mcp"])
def test_reviewed_guardian_association_change_refuses_before_receipt_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    home = options["codex_home"]
    target = next(item.path for item in facts.rollouts if item.thread_id == _id(3))
    source = {"subagent": {"other": "guardian"}}
    metadata = json.loads(target.read_text())
    metadata["payload"].update(source=source, parent_thread_id=str(_id(1).value))
    target.write_text(json.dumps(metadata) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET source=? WHERE id=?", (json.dumps(source), str(_id(3).value))
        )
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    original = Intent(home, facts.cutoff, (_id(3),), (_id(3),))
    monkeypatch.setattr(console_tasks, "load_closed_work_contract", lambda: options["helper"])
    report = preview(original, mode=PreviewMode.CONFIRM)
    reviewed = confirmed_intent(original, report)
    associations = reviewed.reviewed_guardian_associations
    assert associations is not None
    assert associations == (GuardianAssociation(_id(3), _id(1)),)
    service = confirmed_operation.ConfirmedOperations(
        home, options["helper_executable"], tmp_path / "receipts"
    )
    declaration = service.prepare(
        (str(_id(3).value),),
        (str(_id(3).value),),
        facts.cutoff.timestamp.isoformat(),
        report["physical_manifest"],
        associations,
    )
    metadata["payload"]["parent_thread_id"] = str(_id(2).value)
    target.write_text(json.dumps(metadata) + "\n")
    if adapter == "ui":
        result, status = apply_intent(reviewed, options["helper_executable"], options["journal"])
        assert status != 0
        assert not options["journal"].exists()
    else:
        result = service.apply(declaration)
        assert service.status(declaration.operation_id) == result
    assert result["outcome"] == "refused"
    assert "association changed" in result["detail"]
    assert not observed.exists()


@pytest.mark.parametrize("mutation", ["home", "profile"])
def test_terminal_confirmation_retains_preview_identity_until_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    helper = options["helper"]
    monkeypatch.setattr(console_tasks, "load_closed_work_contract", lambda: helper)
    monkeypatch.setattr(closed_work_operation, "load_closed_work_contract", lambda: helper)
    monkeypatch.setattr(closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: facts)
    original = Intent(options["codex_home"], facts.cutoff, (_id(1),), (_id(1), _id(2)))
    report = preview(original, mode=PreviewMode.CONFIRM)
    reviewed = confirmed_intent(original, report)
    assert reviewed.declaration is not None
    assert reviewed.declaration.model_dump(mode="json") == report["declaration"]
    if mutation == "home":
        options["codex_home"].rename(tmp_path / "previous-home")
        options["codex_home"].mkdir()
    else:
        helper = replace(helper, evidence=replace(helper.evidence, data=b"changed profile"))
    result, status = apply_intent(reviewed, options["helper_executable"], options["journal"])
    assert status == 3
    assert result["outcome"] == "refused"
    assert not options["journal"].exists()
    assert not observed.exists()


def test_terminal_confirmation_uses_one_preview_uuid_and_never_replays_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(console_tasks, "load_closed_work_contract", lambda: options["helper"])
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    original = Intent(options["codex_home"], facts.cutoff, (_id(1),), (_id(1), _id(2)))
    reviewed = confirmed_intent(original, preview(original, mode=PreviewMode.CONFIRM))
    assert reviewed.declaration is not None
    result, status = apply_intent(reviewed, options["helper_executable"], options["journal"])
    assert status == 0, result
    assert result["attemptId"] == reviewed.declaration.operation_id
    (saved,) = inspect(options["journal"])
    assert str(saved.start.attempt_id.value) == reviewed.declaration.operation_id
    assert json.loads(saved.start.evidence.data)["operation_declaration"] == (
        reviewed.declaration.model_dump(mode="json")
    )
    consumed = observed.read_bytes()
    _, repeated_status = apply_intent(reviewed, options["helper_executable"], options["journal"])
    assert repeated_status == 3
    assert observed.read_bytes() == consumed


def test_terminal_confirmation_binds_only_preview_candidates_from_larger_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(console_tasks, "load_closed_work_contract", lambda: options["helper"])
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    with sqlite3.connect(options["codex_home"] / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET updated_at_ms=? WHERE id=?",
            (int(facts.cutoff.timestamp.timestamp() * 1000), str(_id(3).value)),
        )
    original = Intent(
        options["codex_home"], facts.cutoff, (_id(1), _id(3)), (_id(1), _id(2), _id(3))
    )
    report = preview(original, mode=PreviewMode.CONFIRM)
    assert report["selected"] == [str(_id(number).value) for number in (1, 2, 3)]
    assert report["deletions"] == [str(_id(number).value) for number in (1, 2)]
    assert report["retained"]
    reviewed = confirmed_intent(original, report)
    assert reviewed.selected == (_id(1), _id(2))
    result, status = apply_intent(reviewed, options["helper_executable"], options["journal"])
    assert status == 4, result
    assert observed.read_text().splitlines() == [str(_id(1).value)]
    (saved,) = inspect(options["journal"])
    assert (
        json.loads(saved.start.evidence.data)["operation_declaration"]["selected"]
        == (report["deletions"])
    )


def test_string_confirmation_survives_javascript_without_echoing_physical_integers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary JSON client returns only strings; the exact reviewed bytes stay owned."""
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    monkeypatch.setattr(closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: facts)
    operations = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    root, child = (str(_id(number).value) for number in (1, 2))
    declaration = operations.prepare(
        (root,),
        (root, child),
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, (root,), (root, child), options["codex_home"]),
    )
    assert any(
        rollout.physical_identity.mtime_ns > 2**53
        for target in declaration.physical_manifest
        for rollout in target.rollouts
    )
    confirmation = {"operation_id": declaration.operation_id, "digest": declaration.digest}
    node = shutil.which("node")
    assert node is not None, "JavaScript confirmation acceptance requires node"
    client = subprocess.run(  # noqa: S603
        [
            node,
            "-e",
            (
                "let s='';process.stdin.on('data',x=>s+=x);"
                "process.stdin.on('end',()=>process.stdout.write(JSON.stringify(JSON.parse(s))))"
            ),
        ],
        input=json.dumps(
            {
                "confirmation": confirmation,
                "review": declaration.model_dump(mode="json"),
                "wide_integer": 2**53 + 1,
            }
        ),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    returned = json.loads(client.stdout)
    assert returned["confirmation"] == confirmation
    assert returned["wide_integer"] == 2**53
    # The client may round review numbers; apply never consumes that lossy review.
    with pytest.raises(ValueError, match="digest differs"):
        operations.apply_reference(declaration.operation_id, "0" * 64)
    assert not observed.exists()
    assert not (tmp_path / "receipts").exists()
    assert operations.apply_reference(**returned["confirmation"])["outcome"] == "confirmed"
    assert observed.read_text().splitlines() == [root]
    (saved,) = inspect(tmp_path / "receipts" / f"{declaration.operation_id}.sqlite")
    assert json.loads(saved.start.evidence.data)["operation_declaration"] == declaration.model_dump(
        mode="json"
    )
    restarted = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], tmp_path / "receipts"
    )
    assert restarted.apply_reference(**confirmation)["outcome"] == "confirmed"
    with pytest.raises(ValueError, match="digest differs"):
        restarted.apply_reference(declaration.operation_id, "0" * 64)
    assert observed.read_text().splitlines() == [root]


def test_unclaimed_reference_expires_with_server_and_never_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    receipts = tmp_path / "receipts"
    operations = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], receipts
    )
    root = str(_id(1).value)
    declaration = operations.prepare(
        (root,),
        (root,),
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, (root,), (root,), options["codex_home"]),
    )
    restarted = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], receipts
    )
    for _ in range(2):
        result = restarted.apply_reference(declaration.operation_id, declaration.digest)
        assert result["state"] == "receipt_absent"
        assert result["outcome"] == "unknown"
    assert not receipts.exists()
    assert not observed.exists()


@pytest.mark.parametrize("claimed", [False, True])
def test_engine_exception_preserves_saved_outcome_or_unknown_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, claimed: bool
) -> None:
    """An exception after engine entry never proves that deletion was refused."""
    facts, _, options, observed = _setup(tmp_path)
    monkeypatch.setattr(confirmed_operation, "load_closed_work_contract", lambda: options["helper"])
    monkeypatch.setattr(
        closed_work_operation, "load_closed_work_contract", lambda: options["helper"]
    )
    monkeypatch.setattr(closed_work_operation, "read_closed_work_facts", lambda *_a, **_k: facts)
    receipts = tmp_path / "receipts"
    operations = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], receipts
    )
    root, child = (str(_id(number).value) for number in (1, 2))
    declaration = operations.prepare(
        (root,),
        (root, child),
        facts.cutoff.timestamp.isoformat(),
        _manifest(facts, (root,), (root, child), options["codex_home"]),
    )
    original = confirmed_operation.execute
    calls = 0

    def failing_engine(
        request: closed_work_operation.ExecutionRequest,
    ) -> tuple[dict[str, Any], int]:
        nonlocal calls
        calls += 1
        if claimed:
            report, status = original(request)
            assert status == 0, report
        message = "injected failure after engine entry"
        raise OSError(message)

    monkeypatch.setattr(confirmed_operation, "execute", failing_engine)
    reference = {"operation_id": declaration.operation_id, "digest": declaration.digest}
    result = operations.apply_reference(**reference)
    assert result["outcome"] == ("confirmed" if claimed else "unknown")
    status = operations.status(declaration.operation_id)
    repeated = operations.apply_reference(**reference)
    if claimed:
        observed_times = [
            datetime.fromisoformat(item["observed_at"]) for item in (result, status, repeated)
        ]
        assert all(value.utcoffset() == UTC.utcoffset(value) for value in observed_times)
        assert observed_times == sorted(observed_times)
        assert {key: value for key, value in status.items() if key != "observed_at"} == {
            key: value for key, value in result.items() if key != "observed_at"
        }
        assert {key: value for key, value in repeated.items() if key != "observed_at"} == {
            key: value for key, value in result.items() if key != "observed_at"
        }
    else:
        assert status == result
        assert repeated == result
    assert calls == 1
    path = receipts / f"{declaration.operation_id}.sqlite"
    if claimed:
        assert observed.read_text().splitlines() == [root]
        (saved,) = inspect(path)
        assert saved.units[0].effective_terminal_kind is TerminalKind.CONFIRMED
    else:
        assert result["detail"] == "injected failure after engine entry"
        assert not path.exists()
        assert not observed.exists()
    restarted = confirmed_operation.ConfirmedOperations(
        options["codex_home"], options["helper_executable"], receipts
    )
    assert restarted.apply_reference(**reference)["outcome"] == result["outcome"]
    assert calls == 1


def test_project_summary_cache_and_generation_drilldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_session_gc.cli import mcp_server  # noqa: PLC0415
    from codex_session_gc.inventory.catalogue import SessionCatalogue  # noqa: PLC0415

    source = _mixed_index().catalogue
    entries = list(source.entries)
    entries[1] = replace(entries[1], cwd="/other")
    catalogue = SessionCatalogue(tuple(entries), (), source_changed=False)
    reads = queries = 0
    original = CatalogueIndex.query

    def read(_home: Path) -> SessionCatalogue:
        nonlocal reads
        reads += 1
        return catalogue

    def query(index: CatalogueIndex, value: CatalogueQuery) -> CatalogueResult:
        nonlocal queries
        queries += 1
        return original(index, value)

    monkeypatch.setattr(mcp_server, "read_session_catalogue", read)
    monkeypatch.setattr(CatalogueIndex, "query", query)
    service = CatalogueService(tmp_path)

    async def check() -> None:
        first = await service.catalogue_summary(text="PROJECT", limit=1)
        second = await service.catalogue_summary(text="project", offset=1, limit=1)
        assert first["next_offset"] == 1
        assert len(second["projects"]) == 1
        assert reads == queries == 1
        assert first["overall"] == second["overall"]
        matched = [
            entry
            for entry in catalogue.entries
            if "project" in (f"{entry.title}\n{entry.cwd}\n{entry.id.value}".casefold())
        ]
        assert first["overall"]["total"]["history_count"] == len(matched)
        assert first["overall"]["total"]["known_stored_bytes"] == sum(
            entry.known_stored_bytes for entry in matched
        )
        row = first["projects"][0]
        selected = await service.query_catalogue(
            snapshot_id=first["snapshot_id"], project_key=row["project_key"]
        )
        assert all(item["cwd"] == row["cwd_display"] for item in selected["histories"])
        for kwargs in (
            {"refresh": True, "min_bytes": -1},
            {
                "refresh": True,
                "project_key": row["project_key"],
                "snapshot_id": first["snapshot_id"],
            },
        ):
            with pytest.raises(ValueError):
                await service.query_catalogue(**kwargs)
        assert reads == 1
        refreshed = await service.catalogue_summary(refresh=True)
        assert reads == 2
        assert refreshed["snapshot_id"] != first["snapshot_id"]
        with pytest.raises(ValueError):
            await service.query_catalogue(
                snapshot_id=first["snapshot_id"], project_key=row["project_key"]
            )

    asyncio.run(check())


def test_stdio_progress_is_scoped_to_each_request(tmp_path: Path) -> None:
    """Overlapping progress tokens receive independent monotone observations."""
    home = tmp_path / "home"
    make_home(home)

    async def check() -> None:
        # The client acknowledges its own in-flight frame before the reply can finish.
        acknowledgements = tmp_path / "progress"
        acknowledgements.mkdir()
        bootstrap = """
import asyncio, sys
from pathlib import Path
from codex_session_gc.execution.timing import current
from codex_session_gc.cli.mcp_server import CatalogueService, run
acks = Path(sys.argv[2])
original_prepare = CatalogueService.prepare_cleanup
async def gated_prepare(self, *args, **kwargs):
    result = await original_prepare(self, *args, **kwargs)
    observer = current()
    assert observer is not None
    count = observer.snapshot()["worker_counts"]["selected_requested"]
    marker = acks / str(count)
    async with asyncio.timeout(10):
        while not marker.is_file():
            await asyncio.sleep(0.01)
    return result
CatalogueService.prepare_cleanup = gated_prepare
run(Path(sys.argv[1]))
"""
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", bootstrap, str(home), str(acknowledgements)],
            env=dict(os.environ),
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            for tool in tools.tools:
                assert "ctx" not in tool.input_schema.get("properties", {})
            snapshot = _data(await session.call_tool("query_catalogue", {}))["snapshot_id"]
            observations: list[list[tuple[float, str | None]]] = [[], []]

            async def receive(index: int, value: float, message: str | None) -> None:
                observations[index].append((value, message))
                counts = json.loads(message or "{}").get("worker_counts")
                if counts is not None and counts.get("selected_requested") == index + 1:
                    await asyncio.to_thread((acknowledgements / str(index + 1)).touch)

            async def first(value: float, _total: float | None, message: str | None) -> None:
                await receive(0, value, message)

            async def second(value: float, _total: float | None, message: str | None) -> None:
                await receive(1, value, message)

            args = {"snapshot_id": snapshot, "thread_ids": [ROOT], "cutoff": "2030-01-01"}
            larger = {**args, "thread_ids": [ROOT, CHILD]}
            results = await asyncio.gather(
                session.call_tool("prepare_cleanup", args, progress_callback=first),
                session.call_tool("prepare_cleanup", larger, progress_callback=second),
            )
            for result in results:
                assert "timings" not in _data(result)["report"]
            for index, frames in enumerate(observations):
                assert frames
                values = [value for value, _ in frames]
                assert values == sorted(set(values))
                assert all(str(home) not in (message or "") for _, message in frames)
                assert all(
                    json.loads(message or "{}")["scope"] == "preview" for _, message in frames
                )
                worker = [
                    json.loads(message or "{}")
                    for _, message in frames
                    if json.loads(message or "{}").get("worker_elapsed_ns") is not None
                ]
                assert worker
                assert worker[-1]["worker_counts"]["selected_requested"] == index + 1
            ordinary = _data(await session.call_tool("prepare_cleanup", args))
            assert "timings" not in ordinary["report"]

    asyncio.run(asyncio.wait_for(check(), timeout=60))


def test_stalled_progress_sender_does_not_hold_business_result() -> None:
    """A blocked transport is cancelled and joined independently of the backend."""
    from codex_session_gc.cli.mcp_server import _observed_call  # noqa: PLC0415
    from codex_session_gc.execution.timing import TimingObserver  # noqa: PLC0415

    async def check() -> None:
        entered = asyncio.Event()
        closed = asyncio.Event()

        class Stalled:
            async def report_progress(
                self, _value: float, total: float | None = None, message: str | None = None
            ) -> None:
                del total, message
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        async def backend() -> dict[str, Any]:
            await entered.wait()
            return {"outcome": "confirmed"}

        result = await asyncio.wait_for(
            _observed_call(Stalled(), TimingObserver("apply"), backend()),
            timeout=2,  # type: ignore[arg-type]
        )
        assert result == {"outcome": "confirmed"}
        assert closed.is_set()

    asyncio.run(check())


def test_observation_preserves_both_preview_response_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep timing outside the digest and both serialized payload limits."""
    from codex_session_gc.cli import mcp_server  # noqa: PLC0415
    from codex_session_gc.cli.mcp_server import _REPORT_BYTES  # noqa: PLC0415
    from codex_session_gc.execution import timing  # noqa: PLC0415

    home = tmp_path / "home"
    make_home(home)
    report = {"detail": ""}

    async def render(_intent: Intent) -> dict[str, str]:
        timing.count(timing.Count.ROOTS_REQUESTED)
        return dict(report)

    monkeypatch.setattr(mcp_server, "async_preview", render)

    async def check() -> None:
        service = CatalogueService(home, capability=McpCapability.READ_ONLY)
        identity = (await service.query_catalogue())["snapshot_id"]
        arguments = (identity, [ROOT], "2030-01-01")
        envelope = await service.prepare_cleanup(*arguments)
        payload_bytes = len(json.dumps(envelope, ensure_ascii=False).encode())
        report["detail"] = "x" * (_REPORT_BYTES - payload_bytes)
        ordinary = await service.prepare_cleanup(*arguments)
        observer = timing.TimingObserver("preview")
        with timing.observe(observer):
            measured = await service.prepare_cleanup(*arguments)
            assert measured == ordinary
            assert observer.snapshot()["counts"] == {"roots_requested": 1}
            report["detail"] += "x"
            with pytest.raises(ValueError, match="Complete preparation exceeds"):
                await service.prepare_cleanup(*arguments)
            report["detail"] = "x" * _REPORT_BYTES
            with pytest.raises(ValueError, match="Full preview exceeds"):
                await service.prepare_cleanup(*arguments)

    asyncio.run(check())


def test_worker_summary_wakes_request_sender() -> None:
    """A newly accepted worker snapshot wakes its request's sender while work continues."""
    from codex_session_gc.cli.mcp_server import _observed_call  # noqa: PLC0415
    from codex_session_gc.execution.timing import (  # noqa: PLC0415
        Count,
        TimingObserver,
        signal_worker_summary,
    )

    async def check() -> None:
        observed = asyncio.Event()
        parent = TimingObserver("preview")
        worker = TimingObserver("preview")
        worker.count(Count.SELECTED_REQUESTED, 2)
        worker.finish()

        class Receiver:
            async def report_progress(
                self, _value: float, total: float | None = None, message: str | None = None
            ) -> None:
                del total
                if json.loads(message or "{}").get("worker_elapsed_ns") is not None:
                    observed.set()

        async def backend() -> dict[str, Any]:
            assert parent.accept_worker(worker.snapshot())
            signal_worker_summary()
            await asyncio.wait_for(observed.wait(), timeout=1)
            return {"outcome": "confirmed"}

        result = await _observed_call(Receiver(), parent, backend())  # type: ignore[arg-type]
        assert result == {"outcome": "confirmed"}

    asyncio.run(check())


def test_empty_survivors_never_create_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_session_gc.cli import mcp_server  # noqa: PLC0415

    _home(tmp_path)

    async def no_candidates(intent: Intent) -> dict[str, Any]:
        return {
            **intent.payload(),
            "deletions": [],
            "errors": [],
            "physical_manifest": [],
            "guardian_associations": [],
        }

    async def check() -> None:
        service = CatalogueService(tmp_path, receipts=tmp_path.parent / "receipts")
        listed = await service.query_catalogue()
        monkeypatch.setattr(mcp_server, "async_preview", no_candidates)
        monkeypatch.setattr(service.operations, "prepare", lambda *_args: pytest.fail("allocated"))
        prepared = await service.prepare_cleanup(listed["snapshot_id"], [_B])
        assert not prepared["can_apply_via_mcp"]
        assert "confirmation" not in prepared
        assert prepared["report"]["cutoff"] is None

    asyncio.run(check())


def test_policy_preview_read_only_and_prior_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def evaluate(_home: Path, _rule: object, **kwargs: object) -> dict[str, Any]:
        calls.append(kwargs)
        return {"rule_matches": [], "removed_ids": [], "cutoff": None}

    monkeypatch.setattr(mcp_server, "evaluate_policy", evaluate)
    service = CatalogueService(tmp_path, capability=McpCapability.READ_ONLY)

    async def exercise() -> None:
        result = await service.preview_policy(
            {"version": 1, "history_kind": "all"},
            [str(QUERY_ROOT.value)],
            "2030-01-01T00:00:00+00:00",
        )
        assert result["rule_matches"] == []
        assert calls[0]["prior_ids"] == frozenset({QUERY_ROOT})
        with pytest.raises(ValueError):
            await service.preview_policy({"version": 1, "history_kind": "all"}, None, "2030-01-01")
        assert not (tmp_path / "receipts").exists()

    asyncio.run(exercise())
