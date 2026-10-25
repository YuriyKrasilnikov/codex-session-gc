# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from textual.widgets import DataTable, Input, Select, Static

from codex_session_gc.cli import mcp_server
from codex_session_gc.cli import textual_console as ui
from codex_session_gc.cli.mcp_capability import McpCapability
from codex_session_gc.domain import ArchiveState, ThreadId, UtcCutoff
from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.execution.confirmed_declaration import ReviewedSelection, create_declaration
from codex_session_gc.execution.physical_manifest import decode_targets
from codex_session_gc.execution.progress import ProgressChannel, ProgressEvent, ProgressKind
from codex_session_gc.inventory.catalogue import SessionCatalogue, SessionEntry, SessionSource
from codex_session_gc.inventory.query import CatalogueScope
from codex_session_gc.recovery import AttemptId
from tests.current_storage_fixture import _catalogue_home as catalogue_home
from tests.current_storage_fixture import _review_manifest, _stored_rollout
from tests.test_catalogue_query import _A, _B, _C, _CWD, _E, _mixed_index

if TYPE_CHECKING:
    from codex_session_gc.cli.console_tasks import Intent

ROOT = ThreadId.parse("00000000-0000-7000-8000-000000000001")
AGENT = ThreadId.parse("00000000-0000-7000-8000-000000000002")
CUTOFF = UtcCutoff(datetime(2030, 1, 1, tzinfo=UTC))


def catalogue() -> SessionCatalogue:
    root = SessionEntry(
        ROOT,
        "Website",
        "/project",
        datetime(2029, 1, 1, tzinfo=UTC),
        ArchiveState.NOT_ARCHIVED,
        None,
        ROOT,
        (AGENT,),
        100,
        None,
        None,
        SessionSource.MAIN,
        known_stored_bytes=100,
        physical_rollouts=(_stored_rollout(ROOT, 100),),
    )
    agent = replace(
        root,
        id=AGENT,
        title="x" * 283753 + " Needle",
        parent_id=ROOT,
        children=(),
        stored_bytes=900,
        known_stored_bytes=900,
        physical_rollouts=(_stored_rollout(AGENT, 900),),
        source=SessionSource.AGENT,
    )
    return SessionCatalogue((root, agent), (), source_changed=False)


def report(intent: Intent) -> dict[str, Any]:
    metadata = intent.home.stat()
    declaration = create_declaration(
        ReviewedSelection(
            roots=tuple(str(value.value) for value in sorted(intent.roots)),
            selected=tuple(str(value.value) for value in sorted(intent.selected)),
            cutoff=intent.cutoff.timestamp.isoformat(),
            physical_manifest=decode_targets(_review_manifest(intent.selected)),
        ),
        operation_id=uuid4(),
        home_identity=(metadata.st_dev, metadata.st_ino),
        helper=load_closed_work_contract(),
    )
    return {
        **intent.payload(),
        "guardian_associations": [],
        "physical_manifest": _review_manifest(intent.selected),
        "shared_retained_bytes": 0,
        "conditional_reclaim_bytes": 900 * len(intent.selected),
        "deletions": [str(item.value) for item in intent.selected],
        "sizes": [900] * len(intent.selected),
        "retained": [],
        "errors": [],
        "conditional": True,
        "declaration": declaration.model_dump(mode="json"),
    }


def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ui.SessionApp:
    monkeypatch.setattr(ui, "read_session_catalogue", lambda _home: catalogue())
    return ui.SessionApp(tmp_path, CUTOFF, Path("/qualified-helper"), tmp_path.parent / "receipts")


def test_filters_groups_and_exact_bulk_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        captured = []

        async def preview(intent: Intent, *, mode: ui.PreviewMode) -> dict[str, Any]:
            assert mode is ui.PreviewMode.CONFIRM
            captured.append(intent)
            return report(intent)

        monkeypatch.setattr(ui, "async_preview", preview)
        async with browser.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            browser.query_one("#scope", Select).value = "agents"
            browser.query_one("#search", Input).value = "needle"
            await pilot.pause()
            assert browser.query_one(DataTable).row_count == 1
            await pilot.click("#select")
            assert set(browser.selected) == {AGENT}
            browser.query_one("#search", Input).value = "no match"
            await pilot.pause()
            assert set(browser.selected) == {AGENT}
            browser.query_one("#search", Input).value = "needle"
            await pilot.pause()
            await pilot.click("#preview")
            await pilot.pause()
            assert captured[0].selected == (AGENT,)
            assert captured[0].roots == (ROOT,)
            assert browser.reviewed is not None
            assert browser.reviewed.payload() == captured[0].payload()
            assert captured[0].reviewed_guardian_associations is None
            assert browser.reviewed.reviewed_guardian_associations == ()
            await pilot.click("#cancel")
            assert browser.reviewed is None

    asyncio.run(scenario())


def test_exact_directory_archive_and_source_filters_match_mcp_without_storage_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue_data = _mixed_index().catalogue
    reads = []
    known_bytes = 50

    def read(home: Path) -> SessionCatalogue:
        reads.append(home)
        return catalogue_data

    monkeypatch.setattr(ui, "read_session_catalogue", read)
    monkeypatch.setattr(mcp_server, "read_session_catalogue", read)

    async def scenario() -> None:
        service = mcp_server.CatalogueService(tmp_path, capability=McpCapability.READ_ONLY)
        listed = await service.query_catalogue()
        browser = ui.SessionApp(tmp_path, CUTOFF, None, tmp_path.parent / "receipts")
        async with browser.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            browser.query_one("#cwd", Select).value = _CWD
            browser.query_one("#archive", Select).value = ArchiveState.ARCHIVED
            browser.query_one("#scope", Select).value = "agents"
            await pilot.pause()
            result = await service.query_catalogue(
                snapshot_id=listed["snapshot_id"],
                cwd=_CWD,
                archive_state=ArchiveState.ARCHIVED,
                scope=CatalogueScope.AGENTS,
            )
            assert {entry["id"] for entry in result["histories"]} == {str(_B.value), str(_C.value)}
            assert {item for group in browser.groups for item in group.matching_ids} == {_B, _C}
            assert any(
                group.root.id == _A and _A not in group.matching_ids for group in browser.groups
            )
            assert result["groups"][str(_A.value)]["root"]["id"] == str(_A.value)
            await pilot.click("#select")
            assert set(browser.selected) == {_B, _C}
            assert _A not in browser.selected
            for scope, expected in [
                (CatalogueScope.ORDINARY_AGENTS, {_B}),
                (CatalogueScope.GUARDIANS, {_C}),
            ]:
                browser.query_one("#scope", Select).value = scope
                await pilot.pause()
                result = await service.query_catalogue(
                    snapshot_id=listed["snapshot_id"],
                    cwd=_CWD,
                    archive_state=ArchiveState.ARCHIVED,
                    scope=scope,
                )
                assert {item for group in browser.groups for item in group.matching_ids} == expected
                assert {entry["id"] for entry in result["histories"]} == {
                    str(x.value) for x in expected
                }
                assert set(browser.selected) == {_B, _C}
            browser.query_one("#scope", Select).value = CatalogueScope.ALL
            browser.query_one("#archive", Select).value = ArchiveState.UNKNOWN
            browser.query_one("#minimum", Input).value = "50"
            await pilot.pause()
            result = await service.query_catalogue(
                snapshot_id=listed["snapshot_id"],
                cwd=_CWD,
                archive_state=ArchiveState.UNKNOWN,
                min_bytes=50,
            )
            assert {item for group in browser.groups for item in group.matching_ids} == {_E}
            (entry,) = result["histories"]
            assert entry["stored_bytes"] is None
            assert entry["known_stored_bytes"] == known_bytes
            browser.query_one("#maximum", Input).value = "50"
            await pilot.pause()
            assert not browser.groups
            result = await service.query_catalogue(
                snapshot_id=listed["snapshot_id"],
                cwd=_CWD,
                archive_state=ArchiveState.UNKNOWN,
                min_bytes=50,
                max_bytes=50,
            )
            assert not result["histories"]
            assert reads == [tmp_path, tmp_path]

    asyncio.run(scenario())


def test_preview_cancel_waits_for_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def preview(_intent: Intent, *, mode: ui.PreviewMode) -> dict[str, Any]:
            assert mode is ui.PreviewMode.CONFIRM
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                cleaned.set()
            return {}

        monkeypatch.setattr(ui, "async_preview", preview)
        async with browser.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#select")
            await pilot.click("#preview")
            await started.wait()
            await pilot.press("escape")
            assert cleaned.is_set()
            assert browser.receipt is None

    asyncio.run(scenario())


def test_dispatch_cannot_quit_or_dispatch_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(ui, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def preview(intent: Intent, *, mode: ui.PreviewMode) -> dict[str, Any]:
            assert mode is ui.PreviewMode.CONFIRM
            return report(intent)

        def execute(
            intent: Intent, _helper: Path, _receipt: Path, *, progress: ProgressChannel | None
        ) -> tuple[dict[str, Any], int]:
            assert progress is not None
            operation = AttemptId(UUID(intent.confirmation().operation_id))
            progress.publish(ProgressEvent(ProgressKind.OPERATION, operation))
            progress.publish(ProgressEvent(ProgressKind.ROOT_STARTED, operation, intent.roots[0]))
            calls.append(intent)
            loop.call_soon_threadsafe(entered.set)

            while not release.is_set():
                time.sleep(0.01)
            return {"outcome": "REFUSED", "roots": []}, 3

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(ui, "async_preview", preview)
        monkeypatch.setattr(ui, "apply_intent", execute)
        async with browser.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#select")
            await pilot.click("#preview")
            await pilot.pause()
            browser.query_one("#confirm", Input).value = "DELETE"
            await pilot.click("#execute")
            await entered.wait()
            clock[0] = 2.5
            await pilot.pause(0.2)
            text = str(browser.query_one("#progress", Static).render())
            assert browser.reviewed is not None
            assert browser.reviewed.confirmation().operation_id in text
            assert "Waiting 2.5s" in text
            assert str(ROOT.value) in text
            assert browser.progress_current == ROOT
            assert not browser.progress_results
            await pilot.click("#execute")
            await pilot.press("ctrl+q")
            assert browser.dispatching
            assert len(calls) == 1
            assert browser.receipt is not None
            release.set()
            assert browser.dispatch_task is not None
            await browser.dispatch_task

    asyncio.run(scenario())


def test_async_preview_public_report_and_cancellation_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_session_gc.cli import console_tasks  # noqa: PLC0415
    from tests.current_storage_fixture import _GRANDCHILD, _MAIN, _native_home  # noqa: PLC0415

    home = tmp_path / "home"
    home.mkdir()
    _native_home(home)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    identifier = ThreadId.parse(_MAIN)
    intent = console_tasks.Intent(home, CUTOFF, (identifier,), (identifier,))

    async def scenario() -> None:
        result = await console_tasks.async_preview(intent)
        assert result["home"] == str(home)
        assert result["selected"] == [_MAIN]
        assert "declaration" not in result
        assert list(temporary.iterdir()) == []
        leaf = console_tasks.Intent(home, CUTOFF, (identifier,), (ThreadId.parse(_GRANDCHILD),))
        bound = await console_tasks.async_preview(leaf, mode=ui.PreviewMode.CONFIRM)
        reviewed = console_tasks.confirmed_intent(leaf, bound)
        assert reviewed.declaration is not None
        metadata = home.stat()
        assert reviewed.declaration.home_identity == (metadata.st_dev, metadata.st_ino)
        assert (
            reviewed.declaration.profile_sha256 == load_closed_work_contract().evidence.digest.hex()
        )
        assert reviewed.declaration.model_dump(mode="json") == bound["declaration"]
        assert list(temporary.iterdir()) == []
        task = asyncio.create_task(console_tasks.async_preview(leaf, mode=ui.PreviewMode.CONFIRM))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(temporary.iterdir()) == []

    asyncio.run(scenario())


def test_narrow_terminal_contains_essential_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        async with browser.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            footer = browser.query_one("Footer").region.y
            for identifier in ("select", "clear", "preview", "cancel", "refresh", "quit"):
                region = browser.query_one("#" + identifier).region
                assert region.x >= 0
                assert region.right <= browser.size.width
                assert region.y >= 0
                assert region.bottom <= footer

    asyncio.run(scenario())


def test_date_and_byte_filters_match_histories_not_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        async with browser.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            browser.query_one("#before", Input).value = "2029-01-01"
            await pilot.pause()
            assert browser.query_one(DataTable).row_count == 0
            browser.query_one("#before", Input).value = "2029-01-02"
            browser.query_one("#minimum", Input).value = "500"
            await pilot.pause()
            await pilot.click("#select")
            assert set(browser.selected) == {AGENT}
            browser.query_one("#scope", Select).value = "main"
            await pilot.pause()
            assert browser.query_one(DataTable).row_count == 0
            assert set(browser.selected) == {AGENT}
            browser.query_one("#before", Input).value = "2029-01-01T00:00:00"
            await pilot.pause()
            assert not browser.valid_filters

    asyncio.run(scenario())


def test_cli_mcp_binds_explicit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_session_gc.cli import mcp_server  # noqa: PLC0415
    from codex_session_gc.cli.main import main  # noqa: PLC0415

    seen = []

    def run(
        home: Path, helper: Path | None, receipts: Path | None, *, capability: McpCapability
    ) -> int:
        assert helper is None
        assert receipts is None
        seen.append((home, capability))
        return 0

    monkeypatch.setattr(mcp_server, "run", run)
    assert main(["mcp", "--home", str(tmp_path)]) == 0
    assert main(["mcp", "--home", str(tmp_path), "--read-only"]) == 0
    assert seen == [
        (tmp_path.resolve(), McpCapability.CLEANUP),
        (tmp_path.resolve(), McpCapability.READ_ONLY),
    ]
    with pytest.raises(SystemExit):
        main(["mcp"])


def test_review_locks_selection_and_resets_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)

        async def preview(intent: Intent, *, mode: ui.PreviewMode) -> dict[str, Any]:
            assert mode is ui.PreviewMode.CONFIRM
            return report(intent)

        monkeypatch.setattr(ui, "async_preview", preview)
        async with browser.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.click("#select")
            await pilot.click("#preview")
            await pilot.pause()
            browser.query_one("#confirm", Input).value = "DELETE"
            assert browser.query_one("#clear").disabled
            assert browser.query_one("#refresh").disabled
            assert browser.query_one("#search").disabled
            before = dict(browser.selected)
            await pilot.click("#clear")
            assert browser.selected == before
            footer = browser.query_one("Footer").region.y
            for identifier in ("confirm", "execute", "cancel", "quit"):
                region = browser.query_one("#" + identifier).region
                assert region.x >= 0
                assert region.right <= browser.size.width
                assert region.bottom <= footer
            await pilot.click("#cancel")
            await pilot.click("#preview")
            await pilot.pause()
            assert browser.query_one("#confirm", Input).value == ""
            assert browser.receipt is None

    asyncio.run(scenario())


def test_retention_and_errors_visible_without_executable_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)

        async def preview(intent: Intent, *, mode: ui.PreviewMode) -> dict[str, Any]:
            assert mode is ui.PreviewMode.CONFIRM
            return {
                **report(intent),
                "deletions": [],
                "retained": [{"id": str(AGENT.value), "reason": "provider retained"}],
                "errors": ["Activity is unknown"],
            }

        monkeypatch.setattr(ui, "async_preview", preview)
        async with browser.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.click("#select")
            await pilot.click("#preview")
            await pilot.pause()
            text = str(browser.query_one("#review-text", Static).render())
            assert "provider retained" in text
            assert "Activity is unknown" in text
            assert browser.reviewed is None
            assert not browser.query_one("#execute").display

    asyncio.run(scenario())


def test_forced_teardown_joins_dispatched_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()

        async def preview(intent: Intent, *, mode: ui.PreviewMode) -> dict[str, Any]:
            assert mode is ui.PreviewMode.CONFIRM
            return report(intent)

        def execute(
            _intent: Intent, _helper: Path, _receipt: Path, *, progress: ProgressChannel | None
        ) -> tuple[dict[str, Any], int]:
            assert progress is not None
            loop.call_soon_threadsafe(entered.set)
            while not release.is_set():
                time.sleep(0.01)
            return {"outcome": "REFUSED", "roots": []}, 3

        async def complete_later() -> None:
            await asyncio.sleep(0.05)
            release.set()

        monkeypatch.setattr(ui, "async_preview", preview)
        monkeypatch.setattr(ui, "apply_intent", execute)
        async with browser.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#select")
            await pilot.click("#preview")
            await pilot.pause()
            browser.query_one("#confirm", Input).value = "DELETE"
            await pilot.click("#execute")
            await entered.wait()
            completion = asyncio.create_task(complete_later())
            browser.exit()
        await completion
        assert browser.completed_result == ("Outcome: REFUSED", 3)
        assert not browser.dispatching

    asyncio.run(scenario())


def test_missing_postdispatch_result_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def close_without_result(browser: ui.SessionApp) -> None:
        browser.receipt = tmp_path.parent / "preserved-receipt.sqlite"

    monkeypatch.setattr(ui.SessionApp, "run", close_without_result)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    arguments = argparse.Namespace(
        home=tmp_path, cutoff=CUTOFF, helper=Path("/helper"), receipt_dir=tmp_path.parent
    )
    unknown_status = 4
    assert ui.run(arguments) == unknown_status
    output = capsys.readouterr().out
    assert "UNKNOWN" in output
    assert "Receipt:" in output
    assert "do not retry" in output


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
def test_title_and_three_sizes_visible_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    import xml.etree.ElementTree as ET  # noqa: PLC0415

    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        own, agents = 9 * 1024**4, 3 * 1024**4
        base = catalogue()
        root = replace(
            base.entries[0],
            title="DistinctTitle" + "x" * 283753,
            stored_bytes=own,
            known_stored_bytes=own,
            physical_rollouts=(_stored_rollout(ROOT, own),),
        )
        agent = replace(
            base.entries[1],
            stored_bytes=agents,
            known_stored_bytes=agents,
            physical_rollouts=(_stored_rollout(AGENT, agents),),
        )
        monkeypatch.setattr(
            ui,
            "read_session_catalogue",
            lambda _home: SessionCatalogue((root, agent), (), source_changed=False),
        )
        async with browser.run_test(size=size) as pilot:
            await pilot.pause()
            document = ET.fromstring(browser.export_screenshot())  # noqa: S314 — owned rendering
            text = " ".join(
                "".join(node.itertext()) for node in document.iter() if node.tag.endswith("}text")
            ).replace("\u00a0", " ")
            headers = [
                str(column.label) for column in browser.query_one(DataTable).ordered_columns[:4]
            ]
            for visible in (*headers, "DistinctTitle", "9.00 TiB", "3.00 TiB", "12.00 TiB"):
                assert visible in text
            browser.query_one(DataTable).focus()
            await pilot.press("down", "up")
            details = str(browser.query_one("#details", Static).render())
            for value in (own, agents, own + agents):
                assert f"{value:,} stored bytes" in details

    asyncio.run(scenario())


@pytest.mark.parametrize("answer", ["delete", "Y", "DELETE ", ""])
def test_dispatch_requires_literal_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        calls = []

        async def preview(intent: Intent, *, mode: ui.PreviewMode) -> dict[str, Any]:
            assert mode is ui.PreviewMode.CONFIRM
            return report(intent)

        def execute(*_args: object, progress: ProgressChannel | None) -> tuple[dict[str, Any], int]:
            assert progress is not None
            calls.append(True)
            return {"outcome": "REFUSED", "roots": []}, 3

        monkeypatch.setattr(ui, "async_preview", preview)
        monkeypatch.setattr(ui, "apply_intent", execute)
        async with browser.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#select")
            await pilot.click("#preview")
            await pilot.pause()
            browser.query_one("#confirm", Input).value = answer
            await pilot.click("#execute")
            assert not calls
            assert browser.receipt is None
            assert browser.reviewed is not None

    asyncio.run(scenario())


def test_preview_cancellation_reaps_actual_owned_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_session_gc.cli import console_tasks  # noqa: PLC0415

    async def scenario() -> None:
        launched = asyncio.Event()
        processes = []
        original = asyncio.create_subprocess_exec

        async def launch(*_args: object, **kwargs: object) -> asyncio.subprocess.Process:
            process = await original(sys.executable, "-c", "import time;time.sleep(60)", **kwargs)
            processes.append(process)
            launched.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        intent = console_tasks.Intent(tmp_path / "home", CUTOFF, (ROOT,), (ROOT,))
        task = asyncio.create_task(console_tasks.async_preview(intent))
        await asyncio.wait_for(launched.wait(), timeout=5)
        assert processes[0].returncode is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []

    asyncio.run(scenario())


def test_orphan_guardian_and_unknown_sources_are_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        browser = app(tmp_path, monkeypatch)
        template = catalogue().entries[0]
        guardian = replace(template, title="Guardian history", source=SessionSource.GUARDIAN)
        unknown = replace(
            template,
            id=AGENT,
            root_id=AGENT,
            children=(),
            title="Unclassified history",
            source=SessionSource.UNKNOWN,
        )
        monkeypatch.setattr(
            ui,
            "read_session_catalogue",
            lambda _home: SessionCatalogue((guardian, unknown), (), source_changed=False),
        )
        async with browser.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = browser.query_one(DataTable)
            captions = [str(table.get_row_at(index)[0]) for index in range(table.row_count)]
            assert any("Guardian history [guardian]" in caption for caption in captions)
            assert any("Unclassified history [unknown]" in caption for caption in captions)
            table.move_cursor(row=0)
            await pilot.pause()
            assert "Source:" in str(browser.query_one("#details", Static).render())
            browser.query_one("#scope", Select).value = "main"
            await pilot.pause()
            assert table.row_count == 0

    asyncio.run(scenario())


def test_mixed_format_catalogue_has_same_stored_total_in_textual_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from compression.zstd import compress  # noqa: PLC0415

    from codex_session_gc.cli.mcp_server import CatalogueService  # noqa: PLC0415
    from codex_session_gc.inventory.catalogue import read_session_catalogue  # noqa: PLC0415
    from tests.current_storage_fixture import _A  # noqa: PLC0415
    from tests.current_storage_fixture import _catalogue_home as make_home  # noqa: PLC0415

    make_home(tmp_path)
    original = tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{_A}.jsonl"
    compressed = original.with_name(original.name + ".zst")
    compressed.write_bytes(compress(original.read_bytes()))
    original.unlink()
    old = original.with_name(original.name.replace(f"{_A}.jsonl", f"{_A}_{AGENT.value}.jsonl"))
    old.write_bytes(b"old immutable history" * 10)
    snapshot = read_session_catalogue(tmp_path)
    own = compressed.stat().st_size + old.stat().st_size
    monkeypatch.setattr(ui, "read_session_catalogue", lambda _home: snapshot)

    async def scenario() -> None:
        public = await CatalogueService(tmp_path).query_catalogue()
        row = next(item for item in public["histories"] if item["id"] == _A)
        assert row["stored_bytes"] == row["known_stored_bytes"] == own
        assert row["physical_lineage"] == "unknown"
        browser = ui.SessionApp(
            tmp_path, CUTOFF, Path("/qualified-helper"), tmp_path.parent / "receipts"
        )
        async with browser.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            browser.query_one(DataTable).focus()
            await pilot.press("down", "up")
            details = str(browser.query_one("#details", Static).render())
            assert f"{own:,} stored bytes" in details
            assert "plain, zstd" in details
            assert str(AGENT.value) in details
            assert "physical lineage unknown" in details
            assert "conditional reclaim: unknown" in details

    asyncio.run(scenario())


@pytest.mark.parametrize("teardown", ["quit", "unmount"])
@pytest.mark.parametrize("read_error", [False, True])
def test_catalogue_load_teardown_joins_actual_metadata_borrowers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, teardown: str, *, read_error: bool
) -> None:
    catalogue_home(tmp_path)
    entered, release = threading.Event(), threading.Event()
    loans: list[int] = []
    workers: set[threading.Thread] = set()
    lock = threading.Lock()
    original_stat = os.stat

    def delayed_stat(path: Any, **kwargs: Any) -> os.stat_result:  # noqa: ANN401
        if kwargs.get("dir_fd") is not None:
            with lock:
                first = not loans
                loans.append(kwargs["dir_fd"])
                workers.add(threading.current_thread())
            if first:
                entered.set()
                assert release.wait(10)
                if read_error:
                    message = "metadata observation failed"
                    raise RuntimeError(message)
        return original_stat(path, **kwargs)

    monkeypatch.setattr(os, "stat", delayed_stat)

    async def scenario() -> None:
        browser = ui.SessionApp(tmp_path, CUTOFF, None, tmp_path / "unused-receipts")
        stopping = asyncio.Event()

        async def drive() -> None:
            async with browser.run_test(size=(120, 40)):
                assert await asyncio.to_thread(entered.wait, 5)
                stopping.set()
                if teardown == "quit":
                    await browser.action_quit()
                else:
                    browser.exit()

        driver = asyncio.create_task(drive())
        try:
            await asyncio.wait_for(stopping.wait(), 10)
            await asyncio.sleep(0.05)
            assert not driver.done()
            assert loans
            assert os.fstat(loans[0])
        finally:
            release.set()
            await asyncio.wait_for(driver, 15)
        assert all(not worker.is_alive() for worker in workers)
        for descriptor in set(loans):
            with pytest.raises(OSError):  # noqa: PT011
                os.fstat(descriptor)
        assert browser.completed_result is None
        assert not (tmp_path / "unused-receipts").exists()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_global_history_order_matches_mcp_pages_and_preserves_exact_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The largest agent can belong to the smaller work; context never becomes selection."""
    root, agent = catalogue().entries
    root = replace(
        root,
        title="Large work",
        stored_bytes=1000,
        known_stored_bytes=1000,
        physical_rollouts=(_stored_rollout(ROOT, 1000),),
    )
    agent = replace(
        agent,
        title="Smaller agent",
        stored_bytes=100,
        known_stored_bytes=100,
        physical_rollouts=(_stored_rollout(AGENT, 100),),
    )
    small = replace(
        root,
        id=_C,
        root_id=_C,
        title="Small work",
        children=(_E,),
        stored_bytes=1,
        known_stored_bytes=1,
        physical_rollouts=(_stored_rollout(_C, 1),),
    )
    largest = replace(
        agent,
        id=_E,
        root_id=_C,
        parent_id=_C,
        title="Largest agent",
        stored_bytes=500,
        known_stored_bytes=500,
        physical_rollouts=(_stored_rollout(_E, 500),),
        last_activity=datetime(2025, 1, 1, tzinfo=UTC),
    )
    observed = SessionCatalogue((root, agent, small, largest), (), source_changed=False)
    monkeypatch.setattr(ui, "read_session_catalogue", lambda _home: observed)
    monkeypatch.setattr(mcp_server, "read_session_catalogue", lambda _home: observed)
    reviewed = []

    async def preview(intent: Intent, **_kwargs: object) -> dict[str, Any]:
        reviewed.append(intent)
        return report(intent)

    monkeypatch.setattr(ui, "async_preview", preview)

    async def scenario() -> None:
        service = mcp_server.CatalogueService(tmp_path)
        first = await service.query_catalogue(
            scope="agents", order_target="history", sort="stored_bytes", direction="desc", limit=1
        )
        second = await service.query_catalogue(
            snapshot_id=first["snapshot_id"],
            scope="agents",
            order_target="history",
            sort="stored_bytes",
            direction="desc",
            offset=1,
            limit=1,
        )
        expected, work_totals = [_E, AGENT], (501, 1100)
        assert [row["id"] for page in (first, second) for row in page["histories"]] == [
            str(value.value) for value in expected
        ]
        assert first["order"] == {"target": "history", "field": "stored_bytes", "direction": "desc"}
        assert (
            first["groups"][str(_C.value)]["total_stored_bytes"],
            second["groups"][str(ROOT.value)]["total_stored_bytes"],
        ) == work_totals
        oldest = await service.query_catalogue(
            snapshot_id=first["snapshot_id"],
            scope="agents",
            roots=[str(_C.value)],
            order_target="history",
            sort="activity",
            direction="asc",
        )
        assert [row["id"] for row in oldest["histories"]] == [str(_E.value)]
        for target, field in (("work", "stored_bytes"), ("history", "total")):
            with pytest.raises(ValueError, match=r"(?i)(order|sort)"):
                await service.query_catalogue(order_target=target, sort=field)
        browser = ui.SessionApp(
            tmp_path, CUTOFF, Path("/qualified-helper"), tmp_path.parent / "receipts"
        )
        async with browser.run_test(size=(120, 40)) as pilot:
            while not browser.query_one(DataTable).row_count:
                await pilot.pause(0.01)
            browser.query_one("#scope", Select).value = CatalogueScope.AGENTS
            browser.query_one("#order-target", Select).value = "history"
            await pilot.pause()
            assert browser.query_one("#sort", Select).value == "stored_bytes"
            assert [identifier for _group, identifier in browser.row_map.values()] == expected
            table = browser.query_one(DataTable)
            table.move_cursor(row=0)
            table.focus()
            await pilot.press("space")
            assert browser.selected == {_E: _C}
            browser.query_one("#direction", Select).value = "asc"
            await pilot.pause()
            assert [identifier for _group, identifier in browser.row_map.values()] == [AGENT, _E]
            assert browser.selected == {_E: _C}
            await pilot.click("#preview")
            while not browser.query_one("#confirm", Input).display:
                await pilot.pause(0.01)
            assert reviewed[0].selected == (_E,)
            assert reviewed[0].roots == (_C,)

    asyncio.run(asyncio.wait_for(scenario(), timeout=45))
