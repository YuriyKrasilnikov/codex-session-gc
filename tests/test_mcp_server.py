# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004, PT011, ASYNC240

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import mcp.client.session
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from codex_session_gc.cli import console_tasks
from codex_session_gc.cli.console_tasks import (
    Intent,
    PreviewMode,
    apply_intent,
    confirmed_intent,
    preview,
)
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
from codex_session_gc.inventory.query import CatalogueIndex
from codex_session_gc.planning.closed_work import select_closed_work
from codex_session_gc.recovery import AttemptId, inspect
from tests.current_storage_fixture import _A, _B, _LIMITS, _id
from tests.current_storage_fixture import _catalogue_home as _home
from tests.mcp_native_fixture import ROOT, make_home
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
    main = ROOT

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
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "query_catalogue",
                "session_details",
                "prepare_cleanup",
                "apply_cleanup",
                "cleanup_status",
                "reconcile_cleanup",
            }
            queried = _data(await session.call_tool("query_catalogue", {"limit": 1}))
            assert len(queried["histories"]) == 1
            snapshot = queried["snapshot_id"]
            prepared = _data(
                await session.call_tool(
                    "prepare_cleanup",
                    {"snapshot_id": snapshot, "thread_ids": [main], "cutoff": "2030-01-01"},
                )
            )
            assert prepared["report"]["selected"] == [main]
            assert prepared["conditional"]
            assert prepared["can_apply_via_mcp"]
            assert len(prepared["preview_sha256"]) == 64
            refused = await session.call_tool("apply", {})
            assert refused.is_error
            bad = await session.call_tool("query_catalogue", {"limit": 100000})
            assert bad.is_error

    asyncio.run(asyncio.wait_for(check(), timeout=45))


def test_actual_adapter_cancellation_reaps_owned_worker_and_removes_copies(tmp_path: Path) -> None:
    _home(tmp_path)
    marker = tmp_path / "worker.json"
    bootstrap = """
import asyncio,json,os,sys
from pathlib import Path
from codex_session_gc.cli import console_tasks
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
        prepared = await service.prepare_cleanup(listed["snapshot_id"], [ROOT], "2030-01-01")
        assert prepared["source_changed"]
        assert prepared["report"]["selected"] == [ROOT]
        assert set(prepared["report"]["deletions"]) <= {ROOT}
        assert prepared["can_apply_via_mcp"]

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
    with pytest.raises(ValueError, match="identity has changed"):
        service.apply(declaration)
    assert service.status(declaration.operation_id)["state"] == "receipt_absent"
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
    assert service.status(declaration.operation_id)["state"] == "receipt_absent"
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
    with pytest.raises(ValueError, match="identity has changed"):
        service.apply(stale)
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

    def backend(_declaration: object) -> dict[str, Any]:
        started.set()
        assert finish.wait(10)
        joined.set()
        return {"outcome": "unknown"}

    monkeypatch.setattr(service, "apply", backend)

    async def check() -> None:
        # No storage is observed by the bounded synthetic backend.
        root = str(_id(1).value)
        declaration = service.prepare(
            (root,),
            (root,),
            facts.cutoff.timestamp.isoformat(),
            _manifest(facts, (root,), (root,), options["codex_home"]),
        )
        task = asyncio.create_task(service.apply_owned(declaration))
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
        assert service.status(declaration.operation_id)["state"] == "receipt_absent"
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
