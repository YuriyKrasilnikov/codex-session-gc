# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

import pytest

from codex_session_gc.cli.closed_work_execution import terminal
from codex_session_gc.cli.main import main
from codex_session_gc.domain import GraphSnapshot, LastActivity, UtcCutoff
from codex_session_gc.execution import closed_work_operation as online
from codex_session_gc.execution.closed_work_profile import ContractRefused
from codex_session_gc.execution.confirmed_operation import ConfirmedOperations
from codex_session_gc.recovery import (
    Payload,
    TerminalKind,
    inspect,
    record_clean_shutdown,
    record_launch_absence,
    record_lifecycle_resolution,
)
from tests.current_storage_fixture import _id
from tests.test_closed_work_apply import _setup
from tests.test_closed_work_reconcile import _delete_selected
from tests.test_closed_work_reconcile import _setup as saved_setup

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from codex_session_gc.execution.closed_work_request import ClosedWorkFacts


def _case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "confirmed"
) -> tuple[list[str], dict[str, Any], Path, ClosedWorkFacts]:
    """Synthetic profile/observation only; not native or runtime qualification."""
    facts, _, options, observed = _setup(tmp_path, mode)
    monkeypatch.setattr(online, "load_closed_work_contract", lambda: options["helper"])

    observation = facts
    monkeypatch.setattr(
        online,
        "read_closed_work_facts",
        lambda *_a, **_k: observation,
    )
    argv = [
        "apply",
        "--online",
        "--home",
        str(options["codex_home"]),
        "--cutoff",
        facts.cutoff.timestamp.isoformat(),
        "--closed-work",
        str(_id(1).value),
        "--helper",
        str(options["helper_executable"]),
        "--receipt",
        str(options["journal"]),
        "--json",
    ]
    return argv, options, observed, facts


def test_cli_real_owned_composition_preserves_parent_agents_and_subset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, facts = _case(tmp_path, monkeypatch)
    observations = []

    def observe(*_a: object, **_k: object) -> ClosedWorkFacts:
        observations.append(facts)
        return facts

    monkeypatch.setattr(online, "read_closed_work_facts", observe)
    argv += [
        "--select-thread",
        str(_id(2).value),
        "--default-provider",
        "operator-default",
        "--max-read-bytes",
        "33554432",
        "--timeout-seconds",
        "30",
    ]
    assert main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    (row,) = report["roots"]
    assert row["ownershipScope"] == [str(_id(1).value), str(_id(2).value)]
    assert row["selectedThreads"] == [str(_id(2).value)]
    assert row["originalNativeOutcome"] == "confirmed"
    assert row["finalObservedState"] is None
    assert row["retained"]
    assert report["initialInventoryResourceBounded"] is True
    (stored,) = inspect(options["journal"])
    request = json.loads(stored.units[0].request.data)
    assert request["provider"] == "operator-default"
    context = json.loads(stored.start.evidence.data)
    declaration = context["operation_declaration"]
    assert declaration["operation_id"] == report["attemptId"] == str(stored.start.attempt_id.value)
    assert declaration["selected"] == [str(_id(2).value)]
    assert declaration["physical_manifest"] == request["targets"]
    assert context["journal"] == str(options["journal"])
    assert context["collection_limits"]["maximum_total_bytes"] == 33554432
    assert context["collection_limits"]["deadline_seconds"] == 30
    assert context["dispatch_limits"]["deadline_seconds"] == 30
    assert len(observations) == 1
    assert observed.read_text().splitlines() == [str(_id(1).value)]


def test_direct_cli_keeps_requested_retention_and_reconciles_admitted_scope_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, facts = _case(tmp_path, monkeypatch, "partial")
    fresh = replace(
        facts,
        snapshot=replace(
            facts.snapshot,
            threads=tuple(
                replace(record, last_activity=LastActivity(facts.cutoff.timestamp))
                if record.id == _id(1)
                else record
                for record in facts.snapshot.threads
            ),
        ),
    )
    observations = []

    def observe(*_a: object, **_k: object) -> ClosedWorkFacts:
        observations.append(fresh)
        return fresh

    monkeypatch.setattr(online, "read_closed_work_facts", observe)
    assert main(argv) == 4
    report = json.loads(capsys.readouterr().out)
    (row,) = report["roots"]
    assert row["requestedThreads"] == [str(_id(number).value) for number in (1, 2)]
    assert row["selectedThreads"] == [str(_id(2).value)]
    assert row["retained"] == [
        {
            "threadId": str(_id(1).value),
            "reason": "recent_or_unknown_last_activity",
            "witness": None,
        }
    ]
    (saved,) = inspect(options["journal"])
    context = json.loads(saved.start.evidence.data)
    declaration = context["operation_declaration"]
    assert declaration["selected"] == [str(_id(2).value)]
    assert context["scopes"][0]["requested_threads"] == declaration["selected"]
    consumed = observed.read_bytes()
    _delete_selected(options["codex_home"], selected=(str(_id(2).value),))
    assert main(["reconcile", "--online", "--receipt", str(options["journal"]), "--json"]) == 0
    reconciled = json.loads(capsys.readouterr().out)
    assert reconciled["outcome"] == "confirmed"
    assert reconciled["roots"][0]["finalObservedState"] == "CONFIRMED_FINAL_STATE"
    assert observed.read_bytes() == consumed
    assert len(observations) == 1
    with sqlite3.connect(options["codex_home"] / "state_5.sqlite") as connection:
        assert connection.execute(
            "SELECT id FROM threads WHERE id=?", (str(_id(1).value),)
        ).fetchone()


def test_skipped_root_keeps_partial_aggregate_and_reasons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, facts = _case(tmp_path, monkeypatch)
    # The explicit cutoff retains root 3 while the first work remains eligible.

    records = [
        replace(t, last_activity=LastActivity(facts.cutoff.timestamp)) if t.id == _id(3) else t
        for t in facts.snapshot.threads
    ]
    snapshot = GraphSnapshot.from_records(
        threads=records,
        rollouts=facts.snapshot.rollouts,
        ownership_edges=facts.snapshot.ownership_edges,
        spawn_edges=facts.snapshot.spawn_edges,
    )
    observation = replace(facts, snapshot=snapshot)
    monkeypatch.setattr(
        online,
        "read_closed_work_facts",
        lambda *_a, **_k: observation,
    )
    assert main([*argv, "--closed-work", str(_id(3).value)]) == 4
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "partial"
    assert len(report["roots"]) == 2
    skipped = next(x for x in report["roots"] if x["root"] == str(_id(3).value))
    assert skipped["status"] == "not_dispatched"
    assert skipped["retained"]
    assert skipped["retained"][0]["reason"] in terminal(report)
    assert observed.read_text().splitlines() == [str(_id(1).value)]
    (stored,) = inspect(options["journal"])
    assert len(stored.units) == 1
    saved = options["journal"].read_bytes()
    assert main(["reconcile", "--online", "--receipt", str(options["journal"]), "--json"]) == 0
    reconciled = json.loads(capsys.readouterr().out)
    assert reconciled["scope"] == "saved_journal_units"
    assert reconciled["scope"] in terminal(reconciled)
    assert len(reconciled["roots"]) == 1
    assert reconciled["roots"][0]["root"] == str(_id(1).value)
    assert options["journal"].read_bytes() == saved


def test_no_selected_eligible_threads_creates_no_receipt_or_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, _ = _case(tmp_path, monkeypatch)
    # Root 3 is outside the explicitly selected work.
    assert main([*argv, "--select-thread", str(_id(3).value)]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["selectionErrors"]
    assert not options["journal"].exists()
    assert not observed.exists()


def test_invalid_progress_port_refuses_before_profile_inventory_or_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, options, observed, facts = _case(tmp_path, monkeypatch)

    def forbidden() -> None:
        pytest.fail("Invalid progress entered application observation")

    monkeypatch.setattr(online, "load_closed_work_contract", forbidden)
    request = online.ExecutionRequest(
        command="apply",
        receipt=options["journal"],
        home=options["codex_home"],
        cutoff=facts.cutoff,
        closed_work=(_id(1),),
        helper=options["helper_executable"],
    )
    result, status = online.execute(request, progress=object())
    assert status == 3
    assert result["outcome"] == "refused"
    assert not options["journal"].exists()
    assert not observed.exists()


@pytest.mark.parametrize(
    "change",
    [
        lambda a: [*a, "--binary", "/unused/codex"],
        lambda a: [*a, "--closed-clients"],
        lambda a: [*a, "--max-read-bytes", "0"],
        lambda a: [*a, "--timeout-seconds", "nan"],
        lambda a: [*a, "--timeout-seconds", "0"],
        lambda a: [x for x in a if x != "--online"],
        lambda a: [*a, "--helper", "relative-helper"],
        lambda a: [*a, "--receipt", "relative-receipt"],
        lambda a: [*a, "--default-provider", ""],
        lambda a: a[: a.index("--helper")] + a[a.index("--helper") + 2 :],
        lambda a: a[: a.index("--cutoff")] + a[a.index("--cutoff") + 2 :],
        lambda a: a[: a.index("--home")] + a[a.index("--home") + 2 :],
        lambda a: a[: a.index("--closed-work")] + a[a.index("--closed-work") + 2 :],
    ],
)
def test_invalid_argument_modes_have_no_observer_receipt_or_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[list[str]], list[str]],
) -> None:
    argv, options, observed, _ = _case(tmp_path, monkeypatch)

    def forbidden(*_a: object, **_k: object) -> None:
        message = "No profile or observer permitted for invalid CLI inputs"
        raise AssertionError(message)

    monkeypatch.setattr(online, "load_closed_work_contract", forbidden)
    monkeypatch.setattr(online, "read_closed_work_facts", forbidden)
    with pytest.raises(SystemExit) as error:
        main(change(argv))
    assert error.value.code == 2
    assert not options["journal"].exists()
    assert not observed.exists()


def test_absent_bundled_profile_refuses_before_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, _ = _case(tmp_path, monkeypatch)

    def absent() -> None:
        raise ContractRefused

    def forbidden(*_a: object, **_k: object) -> None:
        message = "Missing profile must precede observer"
        raise AssertionError(message)

    monkeypatch.setattr(online, "load_closed_work_contract", absent)
    monkeypatch.setattr(online, "read_closed_work_facts", forbidden)
    assert main(argv) == 3
    assert json.loads(capsys.readouterr().out)["outcome"] == "refused"
    assert not options["journal"].exists()
    assert not observed.exists()


def test_reconcile_never_replays_and_distinguishes_original_from_final_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, _ = _case(tmp_path, monkeypatch, "partial")
    assert main(argv) == 4
    capsys.readouterr()
    before = observed.read_bytes()
    replay = ["reconcile", "--online", "--receipt", str(options["journal"]), "--json"]
    assert main(replay) == 4
    unknown = json.loads(capsys.readouterr().out)
    assert unknown["roots"][0]["originalNativeOutcome"] == "partial"
    assert unknown["roots"][0]["finalObservedState"] == "UNKNOWN"
    _delete_selected(options["codex_home"])
    assert main(replay) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scope"] == "saved_journal_units"
    assert report["roots"][0]["originalNativeOutcome"] == "partial"
    assert report["roots"][0]["finalObservedState"] == "CONFIRMED_FINAL_STATE"
    assert observed.read_bytes() == before
    (stored,) = inspect(options["journal"])
    assert stored.units[0].terminal_kind is TerminalKind.PARTIAL
    receipt_bytes = options["journal"].read_bytes()
    assert main(replay) == 0
    assert json.loads(capsys.readouterr().out)["roots"][0]["observationSource"] == "saved"
    assert options["journal"].read_bytes() == receipt_bytes


@pytest.mark.parametrize(
    "extra",
    [
        ["--home", "/unused/home"],
        ["--helper", "/unused/helper"],
        ["--closed-work", str(_id(1).value)],
        ["--closed-clients"],
    ],
)
def test_online_reconcile_rejects_overrides_before_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
) -> None:
    def forbidden() -> None:
        message = "Invalid reconcile cannot load profile"
        raise AssertionError(message)

    monkeypatch.setattr(online, "load_closed_work_contract", forbidden)
    with pytest.raises(SystemExit) as error:
        main(["reconcile", "--online", "--receipt", str(tmp_path / "receipt"), *extra])
    assert error.value.code == 2


def test_all_retained_work_is_reported_without_journal_or_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, facts = _case(tmp_path, monkeypatch)
    cutoff = UtcCutoff(datetime(2026, 1, 1, tzinfo=UTC))
    observation = replace(facts, cutoff=cutoff)
    monkeypatch.setattr(
        online,
        "read_closed_work_facts",
        lambda *_a, **_k: observation,
    )
    argv[argv.index("--cutoff") + 1] = cutoff.timestamp.isoformat()
    assert main(argv) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["roots"][0]["selectedThreads"] == []
    assert report["roots"][0]["retained"]
    assert report["roots"][0]["status"] == "not_dispatched"
    assert not options["journal"].exists()
    assert not observed.exists()


def test_unidentifiable_receipt_remains_unknown_without_changes_or_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, options, observed, _ = _case(tmp_path, monkeypatch)
    with sqlite3.connect(options["journal"]) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    before = options["journal"].read_bytes()
    assert main(["reconcile", "--online", "--receipt", str(options["journal"]), "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["outcome"] == "unknown"
    assert options["journal"].read_bytes() == before
    assert not observed.exists()


@pytest.mark.parametrize("payload", [b"{", b"{}"])
def test_partial_receipt_with_unrenderable_unit_stays_unknown_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: bytes,
) -> None:
    argv, options, observed, _ = _case(tmp_path, monkeypatch, "partial")
    assert main(argv) == 4
    capsys.readouterr()
    consumed = observed.read_bytes()
    (before,) = inspect(options["journal"])
    original_request = before.units[0].request
    # Independently form digest-valid malformed saved declarations, not damaged SQLite.
    with sqlite3.connect(options["journal"]) as connection:
        connection.execute(
            "UPDATE units SET evidence=?,evidence_digest=?",
            (payload, sha256(payload).digest()),
        )
    assert main(["reconcile", "--online", "--receipt", str(options["journal"]), "--json"]) == 4
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "unknown"
    (row,) = report["roots"]
    assert row["unitId"] == str(before.units[0].unit.unit_id.value)
    assert row["root"] is None
    assert row["ownershipScope"] is None
    assert row["originalNativeOutcome"] == "partial"
    assert row["finalObservedState"] == "UNKNOWN"
    (after,) = inspect(options["journal"])
    assert after.units[0].request == original_request
    assert after.units[0].terminal_kind is TerminalKind.PARTIAL
    assert observed.read_bytes() == consumed


def test_unreadable_partial_receipt_budget_does_not_claim_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, _ = _case(tmp_path, monkeypatch, "partial")
    assert main(argv) == 4
    capsys.readouterr()
    consumed = observed.read_bytes()
    saved = options["journal"].read_bytes()
    assert (
        main(
            [
                "reconcile",
                "--online",
                "--receipt",
                str(options["journal"]),
                "--max-read-bytes",
                "1",
                "--json",
            ]
        )
        == 4
    )
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "unknown"
    assert report["attemptId"] is None
    assert report["roots"] == []
    assert report["lifecycleUnresolved"] is None
    assert options["journal"].read_bytes() == saved
    assert observed.read_bytes() == consumed


def test_positive_reconciliation_reports_refreshed_lifecycle_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, options, observed, _ = _case(tmp_path, monkeypatch, "partial")
    assert main(argv) == 4
    capsys.readouterr()
    consumed = observed.read_bytes()
    _delete_selected(options["codex_home"])
    # Simulate missing lifecycle resolution after owned-launch closure.
    with sqlite3.connect(options["journal"]) as connection:
        connection.execute("DELETE FROM lifecycle_resolutions")
    (before,) = inspect(options["journal"])
    assert before.lifecycle_unresolved
    assert main(["reconcile", "--online", "--receipt", str(options["journal"]), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    (after,) = inspect(options["journal"])
    assert not after.lifecycle_unresolved
    assert report["lifecycleUnresolved"] is False
    assert report["roots"][0]["originalNativeOutcome"] == "partial"
    assert report["roots"][0]["finalObservedState"] == "CONFIRMED_FINAL_STATE"
    assert observed.read_bytes() == consumed


def test_cli_reads_terminal_receipt_with_opaque_saved_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reading terminal evidence requires no historical profile decoder or observation."""
    setup = saved_setup(
        tmp_path,
        profile=Payload("test/opaque-profile", 1, b"opaque saved bytes"),
        terminal=TerminalKind.CONFIRMED,
    )
    receipt, attempt = setup[:2]
    (stored,) = inspect(receipt)
    launch = stored.launches[0]
    record_launch_absence(
        receipt,
        attempt,
        launch.intent.launch_id,
        launch.intent.generation,
        Payload("test/absence", 1, b"synthetic"),
    )
    record_lifecycle_resolution(
        receipt,
        attempt,
        launch.intent.generation,
        TerminalKind.CONFIRMED,
        Payload("test/shutdown", 1, b"synthetic"),
    )
    monkeypatch.setattr(
        online,
        "load_closed_work_contract",
        lambda: replace(setup[4], evidence=Payload("test/current-profile", 1, b"current")),
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Terminal receipt reading attempted fresh observation or historical decoding")

    monkeypatch.setattr(online, "reconciliation_contract", forbidden)
    monkeypatch.setattr(online, "reconcile", forbidden)
    before = receipt.read_bytes()
    assert main(["reconcile", "--online", "--receipt", str(receipt), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "confirmed"
    assert report["roots"][0]["originalNativeOutcome"] == "confirmed"
    assert report["roots"][0]["observationSource"] == "saved"
    assert receipt.read_bytes() == before


def test_cli_refuses_noncurrent_unresolved_profile_without_observation_or_redispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = saved_setup(
        tmp_path, profile=Payload("test/noncurrent-profile", 1, b"opaque saved bytes")
    )
    receipt = setup[0]
    monkeypatch.setattr(
        online,
        "load_closed_work_contract",
        lambda: replace(setup[4], evidence=Payload("test/current-profile", 1, b"current")),
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Noncurrent unresolved receipt attempted observation or redispatch")

    monkeypatch.setattr(online, "reconcile", forbidden)
    monkeypatch.setattr(online, "apply_closed_work", forbidden)
    before = receipt.read_bytes()
    assert main(["reconcile", "--online", "--receipt", str(receipt), "--json"]) == 4
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "unknown"
    assert "current qualified receipt profile" in report["detail"]
    (stored,) = inspect(receipt)
    (unit,) = stored.units
    (row,) = report["roots"]
    scope = json.loads(unit.unit.evidence.data)
    assert report["attemptId"] == str(stored.start.attempt_id.value)
    assert report["lifecycleUnresolved"] == stored.lifecycle_unresolved
    assert row["unitId"] == str(unit.unit.unit_id.value)
    assert row["root"] == scope["root"]
    assert row["ownershipScope"] == scope["ownership_scope"]
    assert row["selectedThreads"] == scope["deletion_scope"]
    assert row["originalNativeOutcome"] == "partial"
    assert row["status"] == "unknown"
    assert row["observationSource"] == "unavailable"
    assert row["finalObservedState"] is None
    assert row["detail"] == report["detail"]
    assert "native=partial" in terminal(report)
    assert receipt.read_bytes() == before


@pytest.mark.parametrize("terminal_kind", [TerminalKind.CONFIRMED, TerminalKind.PARTIAL])
def test_wire4_receipt_terminal_metadata_survives_without_old_request_decoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    terminal_kind: TerminalKind,
) -> None:
    old_profile = Payload("test/retired-wire4", 1, b"retired-profile")
    receipt, attempt, _, request, helper, _ = saved_setup(
        tmp_path, terminal=terminal_kind, profile=old_profile, malformed="wire4"
    )
    # Completed metadata is independent of old request syntax; unresolved work
    # requires the current profile and must not decode or replay an old request.
    current = replace(helper, evidence=Payload("test/current-wire5", 1, b"current-profile"))
    monkeypatch.setattr(online, "load_closed_work_contract", lambda: current)
    if terminal_kind is TerminalKind.CONFIRMED:
        (saved,) = inspect(receipt)
        record_launch_absence(
            receipt,
            attempt,
            saved.launches[0].intent.launch_id,
            saved.launch_generation,
            Payload("test/absence", 1, b"absent"),
        )
        record_lifecycle_resolution(
            receipt,
            attempt,
            saved.launch_generation,
            TerminalKind.CONFIRMED,
            Payload("test/lifecycle", 1, b"settled"),
        )
        record_clean_shutdown(receipt, attempt, Payload("test/shutdown", 1, b"clean"))
    expected = 0 if terminal_kind is TerminalKind.CONFIRMED else 4
    assert main(["reconcile", "--online", "--receipt", str(receipt), "--json"]) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["roots"][0]["originalNativeOutcome"] == terminal_kind.value
    if terminal_kind is TerminalKind.PARTIAL:
        assert "current qualified receipt profile" in result["detail"]
    else:
        assert result["outcome"] == "confirmed"
        relocated = receipt.with_name(f"{attempt.value}.sqlite")
        receipt.rename(relocated)
        service = ConfirmedOperations(tmp_path / "home", None, tmp_path)
        observed = service.status(str(attempt.value))
        assert observed["roots"][0]["original_native_outcome"] == "confirmed"
        assert observed["outcome"] == "confirmed"
        assert observed["dispatch_replayed"] is False
    assert json.loads(request.data)["version"] == 4
