# Copyright (c) 2026
# ruff: noqa: S101, PLR2004, PLC0415, ANN401, EM101

"""Frozen descriptor identity and byte accounting for diagnostic headers."""

from __future__ import annotations

import json
import os
from compression.zstd import compress
from typing import TYPE_CHECKING, Any, NoReturn

from codex_session_gc.inventory.diagnostic_headers import (
    HeaderLimits,
    HeaderReason,
    observe_headers,
)
from codex_session_gc.inventory.storage_walk import scan_storage

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from codex_session_gc.inventory.storage_walk import StorageScan

OWNER = "11111111-1111-4111-8111-111111111111"
NAME = f"rollout-2026-01-01T00-00-00-{OWNER}.jsonl"


def _scan(home: Path) -> StorageScan:
    return scan_storage(home, cancelled=lambda: False, max_operations=1000, max_path_bytes=100000)


def test_verified_header_and_changed_leaf(tmp_path: Path) -> None:
    """Only the observed regular inode supplies decoded metadata."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    path = directory / NAME
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": OWNER}}) + "\nbody")
    snapshot = _scan(tmp_path)
    entry = next(x for x in snapshot.entries if x.parts == ("sessions", NAME))
    result = observe_headers(snapshot, (entry,), cancelled=lambda: False)
    assert result.root_valid
    assert result.results[0].reason is HeaderReason.OBSERVED
    facts = result.results[0].facts
    assert facts is not None
    assert str(facts.ownership.thread_id.value) == OWNER
    path.unlink()
    path.write_text("other")
    assert (
        observe_headers(snapshot, (entry,), cancelled=lambda: False).results[0].reason
        == HeaderReason.CHANGED
    )


def test_link_and_fifo_not_read(tmp_path: Path) -> None:
    """Reject nonregular locators before requesting a data descriptor."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    path = directory / NAME
    os.mkfifo(path)
    snapshot = _scan(tmp_path)
    entry = next(x for x in snapshot.entries if x.parts == ("sessions", NAME))
    assert (
        observe_headers(snapshot, (entry,), cancelled=lambda: False).results[0].reason
        == HeaderReason.UNSUPPORTED
    )
    path.unlink()
    target = tmp_path / "unrelated"
    target.write_bytes(b"synthetic sentinel")
    path.symlink_to(target)
    snapshot = _scan(tmp_path)
    entry = next(x for x in snapshot.entries if x.parts == ("sessions", NAME))
    assert (
        observe_headers(snapshot, (entry,), cancelled=lambda: False).results[0].reason
        == HeaderReason.UNSUPPORTED
    )


def test_compressed_and_decoded_budget(tmp_path: Path) -> None:
    """Charge decoder read-ahead against the independent decoded cap."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    path = directory / (NAME + ".zst")
    raw = (
        json.dumps({"type": "session_meta", "payload": {"id": OWNER}}) + "\n" + "x" * 10000
    ).encode()
    path.write_bytes(compress(raw))
    snapshot = _scan(tmp_path)
    entry = next(x for x in snapshot.entries if x.parts[-1:] == (path.name,))
    result = observe_headers(snapshot, (entry,), cancelled=lambda: False)
    assert result.results[0].reason == HeaderReason.OBSERVED
    assert result.physical_bytes > 0
    assert result.decoded_bytes >= len(raw.split(b"\n")[0])
    limit = 10
    limited = observe_headers(
        snapshot, (entry,), cancelled=lambda: False, limits=HeaderLimits(max_decoded_bytes=limit)
    )
    assert limited.results[0].reason == HeaderReason.LIMIT
    assert limited.decoded_bytes <= limit


def test_failed_compressed_read_charges_physical_budget(tmp_path: Path) -> None:
    """Charge consumed input from an unsuccessful compressed read."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    path = directory / (NAME + ".zst")
    path.write_bytes(compress(b"{" + b"x" * 10000 + b"}\n"))
    limit = 10
    snapshot = _scan(tmp_path)
    entry = next(x for x in snapshot.entries if x.parts[-1:] == (path.name,))
    result = observe_headers(
        snapshot, (entry,), cancelled=lambda: False, limits=HeaderLimits(max_physical_bytes=limit)
    )
    assert result.root_valid
    assert result.results[0].reason == HeaderReason.LIMIT
    assert result.physical_bytes == limit


def test_replaced_root_invalidates_entire_batch(tmp_path: Path) -> None:
    """Refuse publication if the observed root was replaced."""
    home = tmp_path / "home"
    home.mkdir()
    directory = home / "sessions"
    directory.mkdir()
    path = directory / NAME
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": OWNER}}) + "\n")
    snapshot = _scan(home)
    entry = next(x for x in snapshot.entries if x.parts == ("sessions", NAME))
    home.rename(tmp_path / "old")
    home.mkdir()
    result = observe_headers(snapshot, (entry,), cancelled=lambda: False)
    assert not result.root_valid


def test_physical_budget_is_shared_across_files(tmp_path: Path) -> None:
    """Carry input charges across all locators in the pass."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    first = directory / NAME
    second_owner = "22222222-2222-4222-8222-222222222222"
    second = directory / NAME.replace(OWNER, second_owner)
    raw = json.dumps({"type": "session_meta", "payload": {"id": OWNER}}).encode() + b"\n"
    first.write_bytes(raw)
    second.write_bytes(raw.replace(OWNER.encode(), second_owner.encode()))
    snapshot = _scan(tmp_path)
    entries = tuple(x for x in snapshot.entries if x.parts[-1:] in ((first.name,), (second.name,)))
    assert len(entries) == 2
    for order in (entries, entries[::-1]):
        result = observe_headers(
            snapshot,
            order,
            cancelled=lambda: False,
            limits=HeaderLimits(max_physical_bytes=len(raw) + 5),
        )
        assert result.root_valid
        assert result.results[0].reason == HeaderReason.OBSERVED
        assert result.results[1].reason == HeaderReason.LIMIT
        assert result.physical_bytes == len(raw) + 5


def test_six_operations_reserve_root_rebinding(tmp_path: Path) -> None:
    """Keep an already verified positive while reporting the remaining limit."""
    first = tmp_path / NAME
    second = tmp_path / NAME.replace(OWNER, "22222222-2222-4222-8222-222222222222")
    first.write_text(json.dumps({"type": "session_meta", "payload": {"id": OWNER}}) + "\n")
    second.write_text("anything\n")
    snapshot = _scan(tmp_path)
    entries = tuple(x for x in snapshot.entries if x.parts in ((first.name,), (second.name,)))
    entries = tuple(sorted(entries, key=lambda x: x.parts))
    result = observe_headers(
        snapshot, entries, cancelled=lambda: False, limits=HeaderLimits(max_operations=6)
    )
    assert result.root_valid
    assert result.operations == 6
    assert result.results[0].reason == HeaderReason.OBSERVED
    assert result.results[1].reason == HeaderReason.LIMIT


def test_deep_json_is_local_malformed(tmp_path: Path) -> None:
    """Deep metadata fails locally after its locator has been verified."""
    path = tmp_path / NAME
    path.write_bytes(b"[" * 2000 + b"0" + b"]" * 2000 + b"\n")
    snapshot = _scan(tmp_path)
    entry = next(x for x in snapshot.entries if x.parts == (NAME,))
    result = observe_headers(snapshot, (entry,), cancelled=lambda: False)
    assert result.root_valid
    assert result.results[0].reason == HeaderReason.MALFORMED
    assert result.results[0].locator_verified


def test_failed_read_with_changed_leaf_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read failure cannot certify a name whose descriptor changed during the read."""
    from codex_session_gc.execution.bounded_header import HeaderReadError
    from codex_session_gc.inventory import diagnostic_headers

    path = tmp_path / NAME
    path.write_bytes(b"unterminated")
    snapshot = _scan(tmp_path)
    entry = next(item for item in snapshot.entries if item.parts == (NAME,))

    def failed_read(*_args: Any, **_kwargs: Any) -> NoReturn:
        path.write_bytes(b"changed metadata")
        raise HeaderReadError("synthetic")

    monkeypatch.setattr(diagnostic_headers, "read_bounded_header", failed_read)
    result = observe_headers(snapshot, (entry,), cancelled=lambda: False)
    assert result.results[0].reason == HeaderReason.CHANGED
    assert not result.results[0].locator_verified
