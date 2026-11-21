# Copyright (c) 2026
# ruff: noqa: S101

"""CLI preview of a replacement storage directory."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from codex_session_gc.cli.main import main
from tests.mcp_native_fixture import CHILD, ROOT, attach_history, make_home

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_cli_reports_replacement_as_independent_preview(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report the directory actually inspected when the old path is reused."""
    codex_dir = tmp_path / "home"
    first = make_home(codex_dir)
    attach_history(first, CHILD, ROOT, standalone=True)
    command = [
        "plan",
        "--online",
        "--home",
        str(codex_dir),
        "--closed-work",
        ROOT,
        "--select-thread",
        ROOT,
    ]
    assert main([*command, "--json"]) == 0
    original = json.loads(capsys.readouterr().out)
    codex_dir.rename(tmp_path / "previous")
    replacement = make_home(codex_dir)
    assert main([*command, "--json"]) == 0
    fresh = json.loads(capsys.readouterr().out)
    assert original["home_identity"] != fresh["home_identity"]
    assert fresh["executable"] is False
    assert replacement[ROOT].is_file()
