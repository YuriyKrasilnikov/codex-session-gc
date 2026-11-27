# Copyright (c) 2026

"""Pinned producer selection must verify the actual executable bytes."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

_PATH = Path(__file__).resolve().parents[2] / "native/helper/tests/stock_spawn_lifecycle.py"
_SPEC = importlib.util.spec_from_file_location("stock_spawn_lifecycle", _PATH)
if _SPEC is None or _SPEC.loader is None:
    message = "stock lifecycle probe cannot be loaded"
    raise RuntimeError(message)
_PROBE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PROBE)


def test_stock_identity_checks_pin_and_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept only pinned identities that match the actual binary."""
    old = tmp_path / "old"
    new = tmp_path / "new"
    unpinned = tmp_path / "unpinned"
    old.write_bytes(b"old executable")
    new.write_bytes(b"new executable")
    unpinned.write_bytes(b"unlisted executable")
    old_sha = hashlib.sha256(old.read_bytes()).hexdigest()
    new_sha = hashlib.sha256(new.read_bytes()).hexdigest()
    unpinned_sha = hashlib.sha256(unpinned.read_bytes()).hexdigest()
    monkeypatch.setattr(_PROBE, "STOCK_SHA256_BY_VERSION", {"0.171.0": old_sha, "0.172.0": new_sha})
    if not _PROBE.qualified_stock_identity(old, old_sha):
        message = "old pinned executable rejected"
        raise AssertionError(message)
    if not _PROBE.qualified_stock_identity(new, new_sha):
        message = "new pinned executable rejected"
        raise AssertionError(message)
    if _PROBE.qualified_stock_identity(old, new_sha):
        message = "old executable accepted with new pin"
        raise AssertionError(message)
    if _PROBE.qualified_stock_identity(new, old_sha):
        message = "new executable accepted with old pin"
        raise AssertionError(message)
    if _PROBE.qualified_stock_identity(unpinned, unpinned_sha):
        message = "unpinned executable accepted with matching digest"
        raise AssertionError(message)
