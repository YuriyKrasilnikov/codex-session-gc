# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from codex_session_gc.cli import main as cli
from codex_session_gc.cli import policy_preview
from codex_session_gc.domain import ExactIds, ThreadId, UtcCutoff
from codex_session_gc.inventory.catalogue import SessionCatalogue
from codex_session_gc.inventory.selection_rule import SelectionRule
from tests.test_catalogue_query import _A, _B, _DATE, _entry


def test_preview_uses_finite_matches_and_fixed_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = SessionCatalogue((_entry(_A, 100), _entry(_B, 300, _A)), (), source_changed=False)
    monkeypatch.setattr(policy_preview, "read_session_catalogue", lambda _: catalogue)
    calls: list[tuple[Path, UtcCutoff | ExactIds, frozenset[ThreadId], frozenset[ThreadId]]] = []

    def plan(
        home: Path,
        cutoff: UtcCutoff | ExactIds,
        roots: frozenset[ThreadId],
        selected: frozenset[ThreadId],
    ) -> dict[str, Any]:
        calls.append((home, cutoff, roots, selected))
        return {
            "home_identity": [home.stat().st_dev, home.stat().st_ino],
            "deletions": [str(_B.value)],
            "retained": [],
            "errors": [],
        }

    monkeypatch.setattr(policy_preview, "preview_selection", plan)
    rule = SelectionRule.from_mapping(
        {"version": 1, "agent_status": "closed", "older_than_days": 90}
    )
    now = datetime(2026, 6, 1, tzinfo=UTC)
    report = policy_preview.preview_policy(tmp_path, rule, evaluated_at=now)
    assert report["rule_matches"] == [str(_B.value)]
    assert report["match_roots"] == {str(_B.value): str(_A.value)}
    assert calls[0][1] == UtcCutoff(now - timedelta(days=90))
    assert calls[0][2:] == (frozenset({_A}), frozenset({_B}))
    changed = SessionCatalogue(
        (_entry(_A, 100), replace(_entry(_B, 300, _A), agent_status="open")),
        (),
        source_changed=False,
    )
    monkeypatch.setattr(policy_preview, "read_session_catalogue", lambda _: changed)
    refreshed = policy_preview.preview_policy(
        tmp_path,
        rule,
        evaluated_at=now + timedelta(days=1),
        prior_ids=frozenset({_B}),
        prior_cutoff=now - timedelta(days=90),
    )
    assert refreshed["rule_matches"] == []
    assert refreshed["removed_ids"] == [str(_B.value)]
    assert len(calls) == 1


def test_unresolved_root_refuses_planner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalogue = SessionCatalogue((replace(_entry(_B, 1), root_id=None),), (), source_changed=False)
    monkeypatch.setattr(policy_preview, "read_session_catalogue", lambda _: catalogue)
    report = policy_preview.preview_policy(
        tmp_path,
        SelectionRule.from_mapping({"version": 1, "history_kind": "all"}),
        evaluated_at=_DATE,
    )
    assert report["unresolved_ids"] == [str(_B.value)]
    assert report["errors"]
    assert not report["deletions"]


def test_cli_rule_is_exclusive_and_outside_home(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    rule = home / "rule.json"
    rule.write_text('{"version":1,"history_kind":"all"}')
    code = cli.main(["plan", "--online", "--home", str(home), "--rule", str(rule)])
    assert code
    assert "outside" in capsys.readouterr().out
    code = cli.main(
        [
            "plan",
            "--online",
            "--home",
            str(home),
            "--rule",
            str(rule),
            "--closed-work",
            str(_A.value),
        ]
    )
    assert code
    assert "combined" in capsys.readouterr().out
