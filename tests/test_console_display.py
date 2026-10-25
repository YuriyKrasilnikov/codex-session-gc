# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from codex_session_gc.cli import console_display as ui
from codex_session_gc.cli import console_tasks as tasks
from codex_session_gc.cli.main import main
from codex_session_gc.domain import GuardianAssociation, ThreadId, UtcCutoff
from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.execution.confirmed_declaration import ReviewedSelection, create_declaration
from codex_session_gc.execution.parent_relation import GuardianParent, relation_payload
from codex_session_gc.execution.physical_manifest import decode_targets
from codex_session_gc.execution.selection_provenance import association_bindings
from tests.current_storage_fixture import _review_manifest

if TYPE_CHECKING:
    from pathlib import Path

_ROOT = ThreadId.parse("00000000-0000-7000-8000-000000000001")
_AGENT = ThreadId.parse("00000000-0000-7000-8000-000000000002")
_UNKNOWN = ThreadId.parse("00000000-0000-7000-8000-000000000003")
_DATE = UtcCutoff(datetime(2030, 1, 1, tzinfo=UTC))


def _report(
    intent: tasks.Intent,
    deletions: tuple[ThreadId, ...] | None = None,
    associations: tuple[GuardianAssociation, ...] = (),
) -> dict[str, Any]:
    selected = deletions or intent.selected
    targets = _review_manifest(selected)
    parents = {
        str(association.guardian_thread_id.value): GuardianParent(
            association.associated_parent_thread_id
        )
        for association in associations
    }
    for target in targets:
        if target["thread_id"] in parents:
            target["source"] = {"subagent": {"other": "guardian"}}
            target["parent_relation"] = relation_payload(parents[target["thread_id"]])
    metadata = intent.home.stat()
    declaration = create_declaration(
        ReviewedSelection(
            roots=tuple(str(value.value) for value in sorted(intent.roots)),
            selected=tuple(str(value.value) for value in sorted(selected)),
            cutoff=intent.cutoff.timestamp.isoformat(),
            physical_manifest=decode_targets(targets),
            guardian_associations=associations,
        ),
        operation_id=uuid4(),
        home_identity=(metadata.st_dev, metadata.st_ino),
        helper=load_closed_work_contract(),
    )
    return {
        **intent.payload(),
        "guardian_associations": [
            binding.model_dump(mode="json") for binding in association_bindings(associations)
        ],
        "physical_manifest": targets,
        "declaration": declaration.model_dump(mode="json"),
        "shared_retained_bytes": 0,
        "conditional_reclaim_bytes": None,
        "deletions": [str(x.value) for x in deletions or intent.selected],
        "retained": [],
        "sizes": [None for _ in deletions or intent.selected],
        "errors": [],
    }


def test_confirmation_freezes_exact_preview_candidates(tmp_path: Path) -> None:
    original = tasks.Intent(tmp_path, _DATE, (_ROOT,), (_ROOT, _AGENT))
    confirmed = tasks.confirmed_intent(original, _report(original, (_AGENT,)))
    assert confirmed.selected == (_AGENT,)
    assert confirmed.roots == (_ROOT,)
    for changed in (
        {"home": "/different"},
        {"cutoff": "2040-01-01"},
        {"deletions": [str(_UNKNOWN.value)]},
        {"errors": ["uncertain"]},
    ):
        with pytest.raises(ValueError, match="Preview"):
            tasks.confirmed_intent(original, {**_report(original), **changed})


def test_receipts_cannot_be_placed_inside_selected_home(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        ui.receipt_path(tmp_path / "receipts", tmp_path)
    assert not (tmp_path / "receipts").exists()


def test_helper_discovery_checks_bytes_without_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "helper"
    helper.write_bytes(b"not a program")
    helper.chmod(0o700)
    expected = hashlib.sha256(helper.read_bytes()).digest()
    monkeypatch.setattr(
        ui,
        "load_closed_work_contract",
        lambda: SimpleNamespace(binary_sha256=expected),
    )
    monkeypatch.setattr(ui.shutil, "which", lambda _name: str(helper))
    assert ui.discover_helper() == helper
    helper.write_bytes(b"wrong bytes")
    assert ui.discover_helper() is None


def test_non_tty_ui_error_is_clear(capsys: pytest.CaptureFixture[str]) -> None:
    usage_error = 2
    assert main(["ui"]) == usage_error
    assert "interactive terminal" in capsys.readouterr().err


def test_control_characters_are_not_terminal_commands() -> None:
    assert ui.clean("Title\x1b[2J\n\u202eend") == "Title?[2J??end"


def test_date_filter_is_utc_and_editable() -> None:
    assert tasks.parse_cutoff("2030-01-01") == _DATE
    assert tasks.parse_cutoff("2030-01-01T00:00:00Z") == _DATE
    with pytest.raises(ValueError):  # noqa: PT011
        tasks.parse_cutoff("2030-01-01T12:00:00")


def test_preview_names_exact_ids_and_retained_reasons(tmp_path: Path) -> None:
    intent = tasks.Intent(tmp_path, _DATE, (_ROOT,), (_ROOT, _AGENT))
    report = _report(intent, (_AGENT,))
    report["labels"] = {str(_ROOT.value): "Website | /projects/site"}
    report["retained"] = [{"id": str(_ROOT.value), "reason": "provider retained"}]
    text = "\n".join(ui.preview_lines(report))
    assert "Website | /projects/site" in text
    assert str(_AGENT.value) in text
    assert "provider retained" in text
    assert "unknown sizes: 1" in text


def test_result_keeps_per_work_reasons_and_sanitizes_terminal_control() -> None:
    text = ui.result_text(
        {
            "outcome": "partial",
            "roots": [
                {"root": str(_ROOT.value), "status": "confirmed"},
                {
                    "root": str(_AGENT.value),
                    "status": "refused",
                    "nativeReceipt": {"status": "refused", "detail": "writer busy\x1b[31m"},
                    "retained": [{"threadId": str(_AGENT.value), "reason": "provider required"}],
                },
                {
                    "root": str(_UNKNOWN.value),
                    "status": "not_dispatched",
                    "selectionErrors": ["unknown dependency"],
                },
            ],
        }
    )
    for value in (
        "confirmed",
        "writer busy",
        "provider required",
        "unknown dependency",
        "unattempted",
        "do not retry",
    ):
        assert value in text
    assert "\x1b" not in text


def test_guardian_review_displays_and_freezes_historical_parent(tmp_path: Path) -> None:
    intent = tasks.Intent(tmp_path, _DATE, (_AGENT,), (_AGENT,))
    report = _report(intent, associations=(GuardianAssociation(_AGENT, _UNKNOWN),))
    text = "\n".join(ui.preview_lines(report))
    assert str(_UNKNOWN.value) in text
    reviewed = tasks.confirmed_intent(intent, report)
    report["guardian_associations"][0]["associated_parent_thread_id"] = str(_ROOT.value)
    assert reviewed.reviewed_guardian_associations == (GuardianAssociation(_AGENT, _UNKNOWN),)
