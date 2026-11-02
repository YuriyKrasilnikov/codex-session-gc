# Copyright (c) 2026

"""Selected support observations retain fresh, bounded helper identity."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from codex_session_gc import diagnostics
from codex_session_gc.compatibility import store
from codex_session_gc.compatibility.observation import ImportedSupport, RecordAvailability
from codex_session_gc.compatibility.store import _read_owned, promote_support
from codex_session_gc.diagnostics import _database
from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.installation.provenance import verify_artifact
from tests.test_support_intake import _fixture

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.execution.closed_work_request import DatabaseContract
    from codex_session_gc.installation.read_budget import ReadBudget


def _selection(tmp_path: Path) -> tuple[Path, Path, ImportedSupport]:
    layout, identifier, artifact, identity, source, verifier = _fixture(tmp_path)

    async def promote() -> ImportedSupport:
        with await verify_artifact(artifact, identity, source, trusted_gh=verifier) as lease:
            receipt = promote_support(layout, identifier, lease, 123)
        return ImportedSupport(layout, identifier, identity.sha256, receipt.context_sha256)

    selection = asyncio.run(promote())
    helper = next(layout.releases.iterdir()) / "codex-session-gc-native"
    home = tmp_path / "home"
    home.mkdir()
    return home, helper, selection


def _match_helper(monkeypatch: pytest.MonkeyPatch, helper: Path) -> None:
    contract = replace(
        load_closed_work_contract(),
        binary_sha256=hashlib.sha256(helper.read_bytes()).digest(),
    )
    monkeypatch.setattr(diagnostics, "load_closed_work_contract", lambda: contract)


@pytest.mark.parametrize("kind", ["same", "hardlink", "distinct"])
def test_selected_helper_is_read_once_only_when_it_is_the_same_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A held matching file is hashed once; a different inode is checked independently."""
    home, installed, selection = _selection(tmp_path)
    requested = installed
    if kind != "same":
        requested = tmp_path / "other-helper"
        if kind == "hardlink":
            os.link(installed, requested)
        else:
            requested.write_bytes(installed.read_bytes())
            requested.chmod(0o755)
    _match_helper(monkeypatch, requested)
    identities = {path.stat().st_ino for path in (requested, installed)}
    totals = dict.fromkeys(identities, 0)
    original = os.pread

    def counted(fd: int, size: int, offset: int) -> bytes:
        data = original(fd, size, offset)
        inode = os.fstat(fd).st_ino
        if inode in totals:
            totals[inode] += len(data)
        return data

    monkeypatch.setattr(os, "pread", counted)
    report = diagnostics.inspect_selected_compatibility(home, requested, selection)
    if report.helper.status is not diagnostics.Status.MATCHED:
        pytest.fail("The requested helper was not observed")
    if report.source_support.availability is not RecordAvailability.AVAILABLE:
        pytest.fail("The selected support record was not validated")
    if totals != {inode: requested.stat().st_size for inode in identities}:
        pytest.fail(f"Unexpected helper reads: {totals}")


@pytest.mark.parametrize("mutation", ["replace", "inplace", "replace_during", "inplace_during"])
def test_changed_helper_makes_selected_record_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Replacement or in-place changes before or during support reads cannot reuse a digest."""
    home, helper, selection = _selection(tmp_path)
    _match_helper(monkeypatch, helper)
    changed = False

    def mutate() -> None:
        nonlocal changed
        if changed:
            return
        changed = True
        if mutation.startswith("replace"):
            replacement = helper.with_name("replacement")
            replacement.write_bytes(helper.read_bytes())
            replacement.chmod(0o755)
            replacement.replace(helper)
        else:
            helper.write_bytes(b"changed-native-file")

    if mutation.endswith("_during"):

        def read_owned(
            directory: int, name: str, limit: int, budget: ReadBudget | None = None
        ) -> bytes:
            mutate()
            return _read_owned(directory, name, limit, budget)

        monkeypatch.setattr(store, "_read_owned", read_owned)
    else:

        def database(
            path: Path, contract: DatabaseContract, maximum: int, deadline: float
        ) -> diagnostics.Observation:
            result = _database(path, contract, maximum, deadline)
            mutate()
            return result

        monkeypatch.setattr(diagnostics, "_database", database)
    report = diagnostics.inspect_selected_compatibility(home, helper, selection)
    if not changed or report.source_support.availability is not RecordAvailability.UNKNOWN:
        pytest.fail("Changed helper authorized the selected support observation")
    if report.recorded_verification is not None:
        pytest.fail("Changed helper retained a verified-origin receipt")


def test_missing_record_preserves_helper_and_tiny_budget_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing support is unavailable without discarding an independent helper observation."""
    home, helper, selection = _selection(tmp_path)
    _match_helper(monkeypatch, helper)
    directory = selection.layout.base / "support" / str(selection.installation_id)
    (directory / f"{selection.record_sha256}.json").unlink()
    report = diagnostics.inspect_selected_compatibility(home, helper, selection)
    if report.helper.status is not diagnostics.Status.MATCHED:
        pytest.fail("Missing support discarded the helper observation")
    if report.source_support.availability is not RecordAvailability.UNKNOWN:
        pytest.fail("Missing support was substituted")
    limited = diagnostics.inspect_selected_compatibility(
        home, helper, selection, options=diagnostics.DiagnosticOptions(maximum_read_bytes=1)
    )
    if limited.helper.status is not diagnostics.Status.UNKNOWN:
        pytest.fail("An undersized read budget reported a helper match")


@pytest.mark.parametrize("mismatch", ["profile", "manifest"])
def test_profile_and_manifest_helper_expectations_are_checked_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    """A digest match at one boundary cannot substitute for the other boundary."""
    home, helper, selection = _selection(tmp_path)
    _match_helper(monkeypatch, helper)
    if mismatch == "profile":
        contract = replace(load_closed_work_contract(), binary_sha256=bytes(32))
        monkeypatch.setattr(diagnostics, "load_closed_work_contract", lambda: contract)
    else:
        manifest = helper.parent / "manifest.json"
        value = json.loads(manifest.read_bytes())
        value["payload"]["codex-session-gc-native"]["sha256"] = "0" * 64
        manifest.write_text(json.dumps(value))
        receipt = selection.layout.receipts / f"{selection.installation_id}.json"
        saved = json.loads(receipt.read_bytes())
        saved["binding"]["manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        receipt.write_text(json.dumps(saved))
    report = diagnostics.inspect_selected_compatibility(home, helper, selection)
    if mismatch == "profile":
        if report.helper.status is not diagnostics.Status.MISMATCH:
            pytest.fail("The profile's helper mismatch was hidden")
        if report.source_support.availability is not RecordAvailability.AVAILABLE:
            pytest.fail("The independently valid installation was not observed")
    else:
        if report.helper.status is not diagnostics.Status.MATCHED:
            pytest.fail("The profile's helper observation was discarded")
        if report.source_support.availability is not RecordAvailability.UNKNOWN:
            pytest.fail("The manifest's helper mismatch was accepted")


def test_expired_shared_deadline_refuses_later_support_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper digest cannot extend the caller's deadline for later validation."""
    home, helper, selection = _selection(tmp_path)
    _match_helper(monkeypatch, helper)
    delayed = False
    monotonic = time.monotonic

    def clock() -> float:
        return monotonic() + (60 if delayed else 0)

    def database(
        path: Path, contract: DatabaseContract, maximum: int, deadline: float
    ) -> diagnostics.Observation:
        nonlocal delayed
        delayed = True
        return _database(path, contract, maximum, deadline)

    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(diagnostics, "_database", database)
    report = diagnostics.inspect_selected_compatibility(
        home, helper, selection, options=diagnostics.DiagnosticOptions(timeout_seconds=1)
    )
    if not delayed or report.helper.status is not diagnostics.Status.MATCHED:
        pytest.fail("The helper was not observed before the deadline expired")
    if report.source_support.availability is not RecordAvailability.UNKNOWN:
        pytest.fail("An expired deadline authorized the selected support observation")


@pytest.mark.parametrize("phase", ["database", "support"])
def test_interruption_closes_held_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """An interrupted diagnostic closes the invocation-owned helper descriptor."""
    home, helper, selection = _selection(tmp_path)
    _match_helper(monkeypatch, helper)
    inode = helper.stat().st_ino
    descriptors: set[int] = set()
    original = os.pread

    def counted(fd: int, size: int, offset: int) -> bytes:
        data = original(fd, size, offset)
        if os.fstat(fd).st_ino == inode:
            descriptors.add(fd)
        return data

    def interrupt_database(*_args: object, **_kwargs: object) -> diagnostics.Observation:
        raise asyncio.CancelledError

    def interrupt_support(
        _directory: int, _name: str, _limit: int, _budget: ReadBudget | None = None
    ) -> bytes:
        helper.write_bytes(b"changed-during-interruption")
        raise asyncio.CancelledError

    monkeypatch.setattr(os, "pread", counted)
    if phase == "database":
        monkeypatch.setattr(diagnostics, "_database", interrupt_database)
    else:
        monkeypatch.setattr(store, "_read_owned", interrupt_support)
    with pytest.raises(asyncio.CancelledError):
        diagnostics.inspect_selected_compatibility(home, helper, selection)
    if not descriptors:
        pytest.fail("The helper descriptor was not observed")
    for fd in descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)
