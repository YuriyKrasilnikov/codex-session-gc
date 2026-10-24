# Copyright (c) 2026
# ruff: noqa: S101

"""Selected-work preview behavior through the shared service and CLI."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from codex_session_gc.cli.console_tasks import Intent, confirmed_intent, preview
from codex_session_gc.cli.main import main
from codex_session_gc.domain import ThreadId, UtcCutoff
from codex_session_gc.execution.closed_work_facts import read_closed_work_facts
from codex_session_gc.execution.closed_work_request import PreparationRefused
from codex_session_gc.execution.resource_policy import collection_limits

ROOT = ThreadId(UUID(int=1))
AGENT = ThreadId(UUID(int=2))
CUTOFF = UtcCutoff(datetime(2030, 1, 1, tzinfo=UTC))


def work_home(home: Path) -> Intent:
    """Create current selected-work metadata without any history-cache database."""
    paths = {}
    for identifier in (ROOT, AGENT):
        source = (
            "cli"
            if identifier == ROOT
            else {"subagent": {"thread_spawn": {"parent_thread_id": str(ROOT.value)}}}
        )
        path = home / "sessions" / f"rollout-2026-06-01T00-00-00-{identifier.value}.jsonl"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": str(identifier.value),
                        "source": source,
                        "model_provider": "openai",
                        "history_mode": "legacy",
                    },
                }
            )
            + "\n"
        )
        paths[identifier] = (path, source)
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads(id TEXT PRIMARY KEY, source TEXT, updated_at_ms INTEGER, "
            "archived INTEGER, rollout_path TEXT, agent_role TEXT, "
            "model_provider TEXT, history_mode TEXT)"
        )
        connection.execute(
            "CREATE TABLE thread_spawn_edges("
            "parent_thread_id TEXT,child_thread_id TEXT,status TEXT)"
        )
        connection.executemany(
            "INSERT INTO threads VALUES(?,?,?,0,?,NULL,'openai','legacy')",
            [
                (str(identifier.value), json.dumps(source), 1_780_272_000_000, str(path))
                for identifier, (path, source) in paths.items()
            ],
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges VALUES(?,?,?)",
            (str(ROOT.value), str(AGENT.value), "closed"),
        )
    return Intent(home, CUTOFF, (ROOT,), (AGENT,))


def test_preview_and_cli_share_exact_conditional_selection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The public CLI and UI/MCP service agree without a history-cache database."""
    intent = work_home(tmp_path)
    expected = preview(intent)
    assert expected["deletions"] == [str(AGENT.value)]
    assert expected["incomingReferencesComplete"]
    assert not expected["closureCertified"]
    assert not expected["executable"]
    reviewed = confirmed_intent(intent, expected)
    assert (
        reviewed.payload(),
        intent.reviewed_guardian_associations,
        reviewed.reviewed_guardian_associations,
    ) == (intent.payload(), None, ())
    assert (
        main(
            [
                "plan",
                "--online",
                "--home",
                str(tmp_path),
                "--cutoff",
                CUTOFF.timestamp.isoformat(),
                "--closed-work",
                str(ROOT.value),
                "--select-thread",
                str(AGENT.value),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == expected


def test_preview_reads_selected_headers_independently_of_history_body_size(tmp_path: Path) -> None:
    """Sparse bodies and qualified unrelated empty files do not increase prefix read work."""
    intent = work_home(tmp_path)
    selected = next((tmp_path / "sessions").glob(f"*-{AGENT.value}.jsonl"))
    unrelated = tmp_path / "sessions" / "unrelated.jsonl"
    unrelated.write_bytes(b"")
    expected = preview(intent)
    with selected.open("ab") as stream:
        stream.truncate(2 * 1024**3)
    before_files = set(tmp_path.rglob("*"))
    before = _read_bytes()
    observed = preview(intent)
    consumed = _read_bytes() - before
    assert observed["deletions"] == expected["deletions"]
    assert observed["sizes"] == [2 * 1024**3]
    assert consumed < 1024**2
    assert set(tmp_path.rglob("*")) == before_files


def _read_bytes() -> int:
    """Observe bytes returned by kernel reads, including SQLite and file reads."""
    values = dict(line.split(": ", 1) for line in Path("/proc/self/io").read_text().splitlines())
    return int(values["rchar"])


def test_preview_refuses_selection_outside_the_work(tmp_path: Path) -> None:
    """An explicit foreign identifier cannot become a deletion candidate."""
    intent = work_home(tmp_path)
    foreign = ThreadId(UUID(int=99))
    report = preview(Intent(intent.home, intent.cutoff, intent.roots, (foreign,)))
    assert report["deletions"] == []
    assert report["errors"] == ["selected_thread_outside_closed_work"]
    with pytest.raises(ValueError, match=r".+"):
        confirmed_intent(intent, report)


def test_whole_work_preview_includes_explicitly_selected_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Selecting the whole closed work does not automatically exclude its MAIN."""
    work_home(tmp_path)
    assert (
        main(
            [
                "plan",
                "--online",
                "--home",
                str(tmp_path),
                "--cutoff",
                CUTOFF.timestamp.isoformat(),
                "--closed-work",
                str(ROOT.value),
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert set(report["deletions"]) == {str(ROOT.value), str(AGENT.value)}


def test_confirmation_is_independent_of_input_order(tmp_path: Path) -> None:
    """Permutations of the same explicit set yield one declaration and confirmation."""
    work_home(tmp_path)
    intent = Intent(tmp_path, CUTOFF, (ROOT,), (AGENT, ROOT))
    report = preview(intent)
    assert report == preview(Intent(tmp_path, CUTOFF, (ROOT,), (ROOT, AGENT)))
    assert set(confirmed_intent(intent, report).selected) == {ROOT, AGENT}
    with pytest.raises(ValueError, match=r".+"):
        Intent(tmp_path, CUTOFF, (ROOT,), (AGENT, AGENT))


@pytest.mark.parametrize("location", ["sql", "header"])
@pytest.mark.parametrize(
    "source",
    [
        {"subagent": {"thread_spawn": {}}},
        *[
            {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
            for parent in (None, True, 7, [], {}, "", "invalid")
        ],
        *[{"subagent": {"thread_spawn": spawn}} for spawn in (None, [], "agent")],
        {
            "subagent": {
                "thread_spawn": {"parent_thread_id": str(ROOT.value)},
                "other": "guardian",
            }
        },
        {
            "subagent": {"thread_spawn": {"parent_thread_id": str(ROOT.value)}},
            "extra": True,
        },
    ],
)
def test_malformed_spawn_source_refuses_shared_facts_preview_and_public_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    location: str,
    source: object,
) -> None:
    """Malformed source metadata yields one explicit refusal without mutation or a traceback."""
    intent = work_home(tmp_path)
    if location == "sql":
        with sqlite3.connect(tmp_path / "state_5.sqlite") as connection:
            connection.execute(
                "UPDATE threads SET source=? WHERE id=?", (json.dumps(source), str(AGENT.value))
            )
    else:
        path = next((tmp_path / "sessions").glob(f"*-{AGENT.value}.jsonl"))
        envelope = json.loads(path.read_text())
        envelope["payload"]["source"] = source
        path.write_text(json.dumps(envelope) + "\n")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(PreparationRefused) as refused:
        read_closed_work_facts(
            intent.home,
            intent.cutoff,
            frozenset(intent.roots),
            frozenset(intent.selected),
            collection_limits(),
        )
    with pytest.raises(PreparationRefused):
        preview(intent)
    unavailable_exit = 3
    assert (
        main(
            [
                "plan",
                "--online",
                "--home",
                str(tmp_path),
                "--cutoff",
                CUTOFF.timestamp.isoformat(),
                "--closed-work",
                str(ROOT.value),
                "--select-thread",
                str(AGENT.value),
                "--json",
            ]
        )
        == unavailable_exit
    )
    result = json.loads(capsys.readouterr().out)
    assert result["errors"] == [str(refused.value)]
    assert result["executable"] is False
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
