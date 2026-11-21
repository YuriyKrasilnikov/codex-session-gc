# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Fresh saved-rule observations through the ordinary selection planner."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from codex_session_gc.cli.console_tasks import preview_selection
from codex_session_gc.domain import ExactIds, UtcCutoff
from codex_session_gc.inventory.catalogue import read_session_catalogue
from codex_session_gc.inventory.query import CatalogueIndex
from codex_session_gc.inventory.selection_rule import evaluate_rule

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.domain import ThreadId
    from codex_session_gc.inventory.selection_rule import SelectionRule


def preview_policy(
    home: Path,
    rule: SelectionRule,
    *,
    evaluated_at: datetime | None = None,
    prior_ids: frozenset[ThreadId] | None = None,
    prior_cutoff: datetime | None = None,
) -> dict[str, Any]:
    """Observe and plan exactly the current rule matches without minting authority."""
    home = home.expanduser().resolve(strict=True)
    initial = home.stat()
    identity = (initial.st_dev, initial.st_ino)
    catalogue = read_session_catalogue(home)
    index = CatalogueIndex(catalogue)
    evaluation = evaluate_rule(
        index,
        rule,
        evaluated_at or datetime.now(UTC),
        prior_ids=prior_ids,
        prior_cutoff=prior_cutoff,
    )
    ids = frozenset(evaluation.rule_matches)

    def text_id(value: ThreadId) -> str:
        return str(value.value)

    def root_text(value: ThreadId | None) -> str | None:
        return text_id(value) if value is not None else None

    report: dict[str, Any] = {
        "kind": "policy_preview",
        "rule": rule.to_mapping(),
        "home": str(home),
        "home_identity": list(identity),
        "evaluated_at": evaluation.evaluated_at.isoformat(),
        "cutoff": evaluation.cutoff.isoformat() if evaluation.cutoff else None,
        "rule_matches": [text_id(x) for x in evaluation.rule_matches],
        "removed_ids": [text_id(x) for x in evaluation.removed_ids],
        "unresolved_ids": [text_id(x) for x in evaluation.unresolved_ids],
        "match_roots": {
            text_id(x): root_text(index.entries[x].root_id) for x in evaluation.rule_matches
        },
        "roots": [text_id(x) for x in evaluation.matched_roots],
        "catalogue_diagnostics": list(catalogue.diagnostics),
        "catalogue_source_changed": catalogue.source_changed,
        "selected": [text_id(x) for x in evaluation.rule_matches],
        "deletions": [],
        "retained": [],
        "sizes": [],
        "errors": [],
        "conditional": True,
        "executable": False,
    }
    if catalogue.source_changed or any(
        x in catalogue.diagnostics
        for x in (
            "catalogue_unavailable",
            "expected_one_state_database",
            "unsupported_state_schema",
        )
    ):
        report["errors"].append("Catalogue observation is unavailable or stale.")
    elif evaluation.unresolved_ids:
        report["errors"].append("Some rule matches have unresolved work roots.")
    elif ids:
        policy = UtcCutoff(evaluation.cutoff) if evaluation.cutoff else ExactIds(ids)
        planned = preview_selection(home, policy, frozenset(evaluation.matched_roots), ids)
        if tuple(planned.get("home_identity", ())) != identity:
            raise ValueError("The home identity changed between rule observation and preview.")
        if not set(planned["deletions"]) <= {text_id(x) for x in ids}:
            raise ValueError("Planner returned candidates outside the rule matches.")
        report.update(planned)
        report["kind"] = "policy_preview"
    final = home.stat()
    if (final.st_dev, final.st_ino) != identity:
        raise ValueError("The home identity changed during rule preview.")
    return report
