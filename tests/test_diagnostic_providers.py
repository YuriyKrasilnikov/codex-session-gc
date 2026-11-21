# Copyright (c) 2026
# ruff: noqa: S101, D103, PLR2004, PLC0415, SLF001, ANN401
"""Provider references use physical rollout identities and detached evidence."""

from __future__ import annotations

import json
from compression.zstd import compress
from typing import TYPE_CHECKING, Any

import pytest

from codex_session_gc.domain import RolloutId
from codex_session_gc.inventory.storage import observe_storage
from codex_session_gc.inventory.storage_diagnostics import observe_diagnostics
from tests.current_storage_fixture import _A, _B, _catalogue_home

if TYPE_CHECKING:
    from pathlib import Path

R = "99999999-9999-4999-8999-999999999999"


def _metadata(owner: str, provider: str | None = None) -> bytes:
    payload: dict[str, object] = {"id": owner}
    if provider:
        payload["history_mode"] = "paginated"
        payload["history_base"] = {
            "thread_id": provider,
            "end_ordinal_exclusive": 1,
            "end_byte_offset": 10,
        }
    return (json.dumps({"type": "session_meta", "payload": payload}) + "\n").encode()


def test_provider_identity_unindexed_archive_and_saved_pages(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    sessions = tmp_path / "sessions"
    for path in sessions.iterdir():
        path.write_bytes(b"bad\n")
    consumer = sessions / f"rollout-2026-06-01T00-00-00-{_A}.jsonl"
    consumer.write_bytes(_metadata(_A, R))
    archive = tmp_path / "archived_sessions"
    archive.mkdir(exist_ok=True)
    provider = archive / f"rollout-2026-06-01T00-00-00-{_B}_{R}.jsonl.zst"
    provider.write_bytes(compress(_metadata(_B)))
    snapshot = observe_storage(tmp_path)
    observation = observe_diagnostics(tmp_path, snapshot, providers=True)
    source = observation.lookup(rollout_id=RolloutId.parse(_A))
    edge = next(item for item in source["items"] if item["kind"] == "needs_provider")
    assert edge["provider_rollout_id"] == R
    assert edge["target_subject"]["rollout_id"] == R
    assert edge["interpretation"] == "needs_provider"
    target = observation.lookup(rollout_id=RolloutId.parse(R))
    incoming = next(item for item in target["items"] if item["kind"] == "needs_provider")
    assert incoming["interpretation"] == "used_by"
    provider.unlink()
    assert observation.lookup(rollout_id=RolloutId.parse(R)) == target


def test_missing_is_unknown_after_changed_consumer(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    path = tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{_A}.jsonl"
    path.write_bytes(_metadata(_A, R))
    snapshot = observe_storage(tmp_path)
    path.write_bytes(_metadata(_A, R) + b"changed")
    result = observe_diagnostics(tmp_path, snapshot, providers=True)
    assert result.summary()["coverage"]["providers"] == "partial"
    assert result.summary()["counts"]["provider_header_reasons"]["changed"] == 1


def test_duplicate_rollout_candidates_are_not_selected(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    sessions = tmp_path / "sessions"
    consumer = sessions / f"rollout-2026-06-01T00-00-00-{_A}.jsonl"
    consumer.write_bytes(_metadata(_A, R))
    archive = tmp_path / "archived_sessions"
    archive.mkdir(exist_ok=True)
    for directory, owner in ((sessions, _A), (archive, _B)):
        path = directory / f"rollout-2026-06-01T00-00-00-{owner}_{R}.jsonl"
        path.write_bytes(_metadata(owner))
    result = observe_diagnostics(tmp_path, observe_storage(tmp_path), providers=True)
    candidates = result.lookup(rollout_id=RolloutId.parse(R))
    assert candidates["status"] == "ambiguous"
    assert candidates["total_matching"] == 2
    consumer_view = result.lookup(rollout_id=RolloutId.parse(_A))
    edge = next(item for item in consumer_view["items"] if item["kind"] == "needs_provider")
    assert edge["reason"] == "ambiguous"
    assert edge["target"] is None


def test_absent_provider_uses_filename_coverage(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    sessions = tmp_path / "sessions"
    for path in sessions.iterdir():
        owner = path.name.removesuffix(".jsonl")[-36:]
        path.write_bytes(_metadata(owner, R if owner == _A else None))
    result = observe_diagnostics(tmp_path, observe_storage(tmp_path), providers=True)
    assert result.summary()["coverage"]["providers"] == "complete"
    edge = next(
        item
        for item in result.lookup(rollout_id=RolloutId.parse(_A))["items"]
        if item["kind"] == "needs_provider"
    )
    assert edge["reason"] == "missing"
    another = next(path for path in sessions.iterdir() if _A not in path.name)
    another.write_bytes(b"bad\n")
    partial = observe_diagnostics(tmp_path, observe_storage(tmp_path), providers=True)
    edge = next(
        item
        for item in partial.lookup(rollout_id=RolloutId.parse(_A))["items"]
        if item["kind"] == "needs_provider"
    )
    assert edge["reason"] == "missing"


@pytest.mark.parametrize("unknown_mode", [False, True])
def test_malformed_provider_keeps_name_link_and_problems(
    tmp_path: Path, *, unknown_mode: bool
) -> None:
    _catalogue_home(tmp_path)
    sessions = tmp_path / "sessions"
    for path in sessions.iterdir():
        owner = path.name.removesuffix(".jsonl")[-36:]
        path.write_bytes(_metadata(owner, R if owner == _A else None))
    archive = tmp_path / "archived_sessions"
    archive.mkdir(exist_ok=True)
    provider = archive / f"rollout-2026-06-01T00-00-00-{_B}_{R}.jsonl"
    if unknown_mode:
        envelope = json.loads(_metadata(_B))
        envelope["payload"]["history_mode"] = "unknown"
        provider.write_text(json.dumps(envelope) + "\n")
    else:
        provider.write_bytes(b"invalid\n")
    result = observe_diagnostics(tmp_path, observe_storage(tmp_path), providers=True)
    view = result.lookup(rollout_id=RolloutId.parse(_A))
    edge = next(item for item in view["items"] if item["kind"] == "needs_provider")
    assert edge["reason"] == "provider_header_unavailable"
    assert edge["target_subject"]["rollout_id"] == R
    assert "provider_header_unavailable" in view["subject"]["reasons"]
    assert any(item["rollout_id"] == _A for item in result.subjects(reason="problems")["items"])
    assert any(
        item["kind"] == "needs_provider"
        for item in result.lookup(rollout_id=RolloutId.parse(R))["items"]
    )


def test_clipped_duplicate_is_not_decoded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_session_gc.inventory import storage

    _catalogue_home(tmp_path)
    sessions = tmp_path / "sessions"
    archive = tmp_path / "archived_sessions"
    archive.mkdir(exist_ok=True)
    for directory, owner in ((sessions, _A), (archive, _B)):
        (directory / f"rollout-2026-06-01T00-00-00-{owner}_{R}.jsonl").write_bytes(
            _metadata(owner, _A)
        )
    original = storage._rows

    def clipped(*args: Any, **kwargs: Any) -> Any:
        rows, omitted, paths = original(*args, **kwargs)
        selected = [index for index, row in enumerate(rows) if row.rollout_id != R]
        selected.append(next(index for index, row in enumerate(rows) if row.rollout_id == R))
        selected.sort()
        return tuple(rows[i] for i in selected), omitted + 1, tuple(paths[i] for i in selected)

    monkeypatch.setattr(storage, "_rows", clipped)
    result = observe_diagnostics(tmp_path, observe_storage(tmp_path), providers=True)
    candidates = result.lookup(rollout_id=RolloutId.parse(R))
    assert candidates["status"] == "ambiguous"
    physical = candidates["items"][0]
    assert "header_ambiguous" in physical["reasons"]
    assert not any(
        item["kind"] == "needs_provider" for item in result.explain(physical["subject_id"])["items"]
    )


def test_unique_ancestor_locator_budget_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_session_gc.inventory import storage

    _catalogue_home(tmp_path)
    monkeypatch.setattr(storage, "_HEADER_LOCATOR_NODES", 2)
    snapshot = observe_storage(tmp_path)
    assert snapshot._header_scan is not None
    assert len(snapshot._header_scan.entries) <= 2
    assert not snapshot._header_names_complete
    result = observe_diagnostics(tmp_path, snapshot, providers=True)
    assert result.summary()["coverage"]["providers"] == "partial"
    assert result.summary()["counts"]["provider_header_reasons"]["invalid_locator"] > 0


def test_unterminated_provider_header_keeps_verified_name_link(tmp_path: Path) -> None:
    _catalogue_home(tmp_path)
    sessions = tmp_path / "sessions"
    for path in sessions.iterdir():
        owner = path.name.removesuffix(".jsonl")[-36:]
        path.write_bytes(_metadata(owner, R if owner == _A else None))
    archive = tmp_path / "archived_sessions"
    archive.mkdir(exist_ok=True)
    (archive / f"rollout-2026-06-01T00-00-00-{_B}_{R}.jsonl.zst").write_bytes(
        compress(b"unterminated")
    )
    result = observe_diagnostics(tmp_path, observe_storage(tmp_path), providers=True)
    edge = next(
        item
        for item in result.lookup(rollout_id=RolloutId.parse(_A))["items"]
        if item["kind"] == "needs_provider"
    )
    assert edge["reason"] == "provider_header_unavailable"
    assert edge["target_subject"]["rollout_id"] == R
