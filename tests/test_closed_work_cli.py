# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.cli.main import main
from tests.current_storage_fixture import _CHILD, _GRANDCHILD, _MAIN, _native_home, _source

if TYPE_CHECKING:
    from pathlib import Path

_INVALID_INPUT_EXIT = 2
_UNAVAILABLE_EXIT = 3


def _arguments(home: Path) -> list[str]:
    return [
        "plan",
        "--online",
        "--home",
        str(home),
        "--cutoff",
        "2030-01-01T00:00:00Z",
        "--closed-work",
        _MAIN,
    ]


def test_explicit_closed_work_cli_is_read_only_and_subset_bounded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _native_home(tmp_path)
    before = _source(tmp_path)
    assert main([*_arguments(tmp_path), "--select-thread", _GRANDCHILD, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "closed_work_selection"
    assert report["roots"] == [_MAIN]
    assert report["selected"] == report["deletions"] == [_GRANDCHILD]
    assert not report["executable"]
    assert not report["closureCertified"]
    assert report["incomingReferencesComplete"]
    assert _source(tmp_path) == before


def test_closed_work_terminal_includes_exact_selection_and_native_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _native_home(tmp_path)
    assert main(_arguments(tmp_path)) == 0
    output = capsys.readouterr().out
    assert _MAIN in output
    assert _CHILD in output
    assert _GRANDCHILD in output
    assert "CONDITIONAL PREVIEW" in output
    assert "Native ownership and fresh checks" in output


def test_selection_requires_root_and_valid_uuid(tmp_path: Path) -> None:
    for flags in (["--select-thread", _CHILD], ["--closed-work", "not-a-uuid"]):
        with pytest.raises(SystemExit) as failure:
            main(
                [
                    "plan",
                    "--online",
                    "--home",
                    str(tmp_path),
                    "--cutoff",
                    "2030-01-01T00:00:00Z",
                    *flags,
                ]
            )
        assert failure.value.code == _INVALID_INPUT_EXIT


def test_unknown_work_is_visible_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _native_home(tmp_path)
    arguments = _arguments(tmp_path)
    arguments[-1] = "11111111-1111-4111-8111-111111111111"
    assert main([*arguments, "--json"]) == _UNAVAILABLE_EXIT
    report = json.loads(capsys.readouterr().out)
    assert report["errors"]
    assert not report["executable"]


@pytest.mark.parametrize("source", ["cli", "mcp", "unknown"])
def test_public_root_origin_requires_positive_sql_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    _native_home(tmp_path)
    with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET source=? WHERE id=?", (source, _MAIN))
    path = next(path for path in tmp_path.rglob("*.jsonl") if _MAIN in path.name)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["payload"]["source"] = source
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    for outside in tmp_path.rglob("*.jsonl"):
        lines = outside.read_text().splitlines()
        envelope = json.loads(lines[0])
        base = envelope["payload"].get("history_base")
        if isinstance(base, dict) and base.get("thread_id") == _MAIN:
            envelope["payload"].pop("history_base")
            lines[0] = json.dumps(envelope)
            outside.write_text("\n".join(lines) + "\n")
    before = _source(tmp_path)
    assert main([*_arguments(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (_MAIN in report["deletions"]) is (source == "cli")
    if source != "cli":
        assert {"id": _MAIN, "reason": "unknown_origin"} in report["retained"]
    assert report["incomingReferencesComplete"]
    assert _source(tmp_path) == before
