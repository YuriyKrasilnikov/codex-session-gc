# Copyright (c) 2026

"""Exercise stable source identity while preparing a support signing subject."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools import support_record


def _source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, bytes]:
    raw = (Path.cwd() / "src/codex_session_gc/compatibility/support.json").read_bytes()
    source = tmp_path / "source.json"
    source.write_bytes(raw)
    monkeypatch.setattr(support_record, "_SOURCE", source)
    return source, raw


def test_first_read_atime_update_is_not_source_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal relatime access-time update leaves the exact subject admissible."""
    source, raw = _source(tmp_path, monkeypatch)
    before = source.stat()
    old_access_ns = 1_000_000_000
    os.utime(source, ns=(old_access_ns, before.st_mtime_ns))
    output = tmp_path / "output.json"
    support_record.prepare_support_record(output)
    if output.read_bytes() != raw:
        pytest.fail("Source bytes changed during the atime-only read")
    if source.stat().st_atime_ns == old_access_ns:
        pytest.fail("The filesystem did not exercise the first-read atime update")


@pytest.mark.parametrize("change", ["content", "inode"])
def test_changed_source_refuses_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """Changes to source bytes or source pathname identity refuse publication."""
    source, raw = _source(tmp_path, monkeypatch)
    real_fstat = os.fstat
    count = 0
    after_read = 2

    def changing_fstat(fd: int) -> os.stat_result:
        nonlocal count
        count += 1
        if count == after_read:
            if change == "content":
                source.write_bytes(raw + b" ")
            else:
                replacement = tmp_path / "replacement.json"
                replacement.write_bytes(raw)
                replacement.replace(source)
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", changing_fstat)
    output = tmp_path / "output.json"
    with pytest.raises(ValueError, match="changed during reading"):
        support_record.prepare_support_record(output)
    if output.exists():
        pytest.fail("Changed source was published")
