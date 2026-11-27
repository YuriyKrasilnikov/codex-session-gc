# Copyright (c) 2026
# ruff: noqa: D100, D103, S101

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from codex_session_gc.execution import receipt_effects
from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.execution.confirmed_declaration import Declaration, schema_version
from codex_session_gc.execution.operation_status import project_operation_status
from codex_session_gc.execution.receipt_effects import project_receipt_effects
from codex_session_gc.recovery import (
    AttemptId,
    LaunchId,
    LaunchIntent,
    LaunchPurpose,
    Payload,
    StartEnvelope,
    TerminalKind,
    Unit,
    UnitId,
    create,
    fence_launch,
    inspect,
    record_clean_shutdown,
    record_dispatch_intent,
    record_launch_intent,
    record_lifecycle_resolution,
    record_terminal_evidence,
)
from tests.current_storage_fixture import _fixture, _prepare


def _payload(value: bytes) -> Payload:
    return Payload("test/operation-status", 1, value)


def _receipt(path: Path, outcomes: tuple[TerminalKind | None, ...]) -> AttemptId:
    attempt = AttemptId(uuid4())
    evidence = _payload(b"declared")
    units = tuple(Unit(UnitId(uuid4()), index, evidence) for index in range(len(outcomes)))
    create(path, StartEnvelope(attempt, evidence, evidence, evidence, evidence), units)
    for unit, kind in zip(units, outcomes, strict=True):
        if kind is None:
            continue
        request = _payload(str(unit.unit_id.value).encode())
        record_dispatch_intent(path, attempt, unit.unit_id, request, evidence, evidence)
        record_terminal_evidence(path, attempt, unit.unit_id, request.digest, kind, evidence)
    return attempt


def test_exact_lifecycle_settlement_resolves_missing_original_shutdown(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    attempt = _receipt(path, (TerminalKind.CONFIRMED,))
    launch = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"launch"))
    record_launch_intent(path, attempt, launch)
    fence_launch(path, attempt, launch.launch_id, 1, _payload(b"never granted"))
    record_lifecycle_resolution(path, attempt, 1, TerminalKind.CONFIRMED, _payload(b"settled"))

    (stored,) = inspect(path)
    result = project_operation_status(stored)

    assert stored.startup_unresolved
    assert stored.shutdown is None
    assert result.outcome is TerminalKind.CONFIRMED
    assert not result.lifecycle_unresolved


def test_no_launch_is_not_positive_process_settlement(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    _receipt(path, (TerminalKind.CONFIRMED,))
    (stored,) = inspect(path)
    assert not stored.lifecycle_unresolved

    result = project_operation_status(stored)

    assert result.outcome is TerminalKind.UNKNOWN
    assert result.lifecycle_unresolved


@pytest.mark.parametrize(
    ("outcomes", "skipped", "expected"),
    [
        ((TerminalKind.CONFIRMED,), (), TerminalKind.CONFIRMED),
        ((TerminalKind.CONFIRMED,), ("skipped-root",), TerminalKind.PARTIAL),
        ((TerminalKind.CONFIRMED, TerminalKind.REFUSED), (), TerminalKind.PARTIAL),
        ((TerminalKind.CONFIRMED, None), (), TerminalKind.PARTIAL),
        ((TerminalKind.REFUSED, None), (), TerminalKind.REFUSED),
        ((None,), (), TerminalKind.REFUSED),
        ((TerminalKind.PARTIAL,), (), TerminalKind.PARTIAL),
        ((TerminalKind.UNKNOWN,), (), TerminalKind.UNKNOWN),
    ],
)
def test_settled_delivery_aggregation_preserves_unattempted_and_skipped_roots(
    tmp_path: Path,
    outcomes: tuple[TerminalKind | None, ...],
    skipped: tuple[str, ...],
    expected: TerminalKind,
) -> None:
    path = tmp_path / "receipt.sqlite"
    attempt = _receipt(path, outcomes)
    record_clean_shutdown(path, attempt, _payload(b"clean"))

    result = project_operation_status(inspect(path)[0], skipped_roots=skipped)

    assert result.outcome is expected
    assert not result.lifecycle_unresolved


def test_dispatched_unit_without_reply_is_unknown_after_shutdown(tmp_path: Path) -> None:
    path = tmp_path / "receipt.sqlite"
    attempt = _receipt(path, (None,))
    (stored,) = inspect(path)
    payload = _payload(b"sent")
    record_dispatch_intent(path, attempt, stored.units[0].unit.unit_id, payload, payload, payload)
    record_clean_shutdown(path, attempt, _payload(b"clean"))

    assert project_operation_status(inspect(path)[0]).outcome is TerminalKind.UNKNOWN


@pytest.mark.parametrize("generation", [None, 0, 2])
def test_lifecycle_payload_without_exact_positive_generation_cannot_settle(
    tmp_path: Path, generation: int | None
) -> None:
    path = tmp_path / "receipt.sqlite"
    attempt = _receipt(path, (TerminalKind.CONFIRMED,))
    launch = LaunchIntent(LaunchId(uuid4()), 1, LaunchPurpose.APPLY, _payload(b"launch"))
    record_launch_intent(path, attempt, launch)
    (stored,) = inspect(path)
    claimed = replace(
        stored,
        lifecycle=_payload(b"claimed"),
        lifecycle_kind=TerminalKind.CONFIRMED,
        lifecycle_generation=generation,
    )

    result = project_operation_status(claimed)

    assert result.outcome is TerminalKind.UNKNOWN
    assert result.lifecycle_unresolved


def test_receipt_effects_validate_native_result_and_keep_reconciliation_separate(
    tmp_path: Path,
) -> None:
    facts, selection, helper = _fixture(tmp_path)
    request = _prepare(tmp_path, facts, selection, helper)
    targets = json.loads(request.data)["targets"]
    paths = [rollout["rollout_path"] for target in targets for rollout in target["rollouts"]]
    size = sum(
        rollout["physical_identity"]["size"] for target in targets for rollout in target["rollouts"]
    )
    _receipt(tmp_path / "receipt.sqlite", (None,))
    (attempt,) = inspect(tmp_path / "receipt.sqlite")
    unit = attempt.units[0]
    raw_request = {
        **json.loads(request.data),
        "attempt_id": str(attempt.start.attempt_id.value),
        "unit_id": str(unit.unit.unit_id.value),
    }
    request = Payload(request.content_type, request.version, json.dumps(raw_request).encode())
    helper = load_closed_work_contract()
    context = Payload(
        "application/vnd.codex-session-gc.closed-work-apply+json",
        1,
        json.dumps(
            {"helper_sha256": helper.binary_sha256.hex(), "source_identity": helper.source_identity}
        ).encode(),
    )
    attempt = replace(
        attempt,
        start=replace(attempt.start, profile=helper.evidence, evidence=context, process=context),
    )

    def native(status: str, removed: list[str], digest: list[int]) -> Payload:
        data = json.dumps(
            {"status": status, "removed_rollouts": removed, "request_sha256": digest}
        ).encode()
        return Payload("application/vnd.codex-session-gc.native-outcome+json", 1, data)

    unit = replace(
        unit,
        request=request,
        terminal_kind=TerminalKind.CONFIRMED,
        terminal=native("confirmed", paths, list(request.digest)),
    )
    summary = project_receipt_effects(replace(attempt, units=(unit,), shutdown=_payload(b"clean")))
    assert summary["units"][0]["recorded_removed_rollout_count"] == len(paths)
    assert summary["units"][0]["recorded_removed_logical_bytes"] == size
    assert summary["units"][0]["completeness"] == "complete"
    altered = dict(raw_request, unit_id=str(uuid4()))
    wrong_request = Payload(request.content_type, 1, json.dumps(altered).encode())
    wrong = replace(
        unit, request=wrong_request, terminal=native("confirmed", paths, list(wrong_request.digest))
    )
    assert (
        project_receipt_effects(replace(attempt, units=(wrong,)))["units"][0]["completeness"]
        == "unavailable"
    )
    duplicate = project_receipt_effects(replace(attempt, units=(unit, unit)))
    assert duplicate["effects"]["recorded_removed_rollout_count"] is None
    assert duplicate["effects"]["unavailable_unit_count"] == len(duplicate["units"])
    refused = replace(
        unit,
        terminal_kind=TerminalKind.REFUSED,
        terminal=native("refused", [], list(request.digest)),
    )
    refused_result = project_receipt_effects(replace(attempt, units=(refused,)))
    assert refused_result["units"][0]["recorded_removed_rollout_count"] == 0
    assert refused_result["units"][0]["completeness"] == "complete"
    for bad in (paths + paths, paths[:-1], ["/foreign"]):
        changed = replace(unit, terminal=native("confirmed", bad, list(request.digest)))
        assert (
            project_receipt_effects(replace(attempt, units=(changed,)))["units"][0]["completeness"]
            == "unavailable"
        )
    changed = replace(unit, terminal=native("confirmed", paths, [0] * 32))
    assert (
        project_receipt_effects(replace(attempt, units=(changed,)))["units"][0]["completeness"]
        == "unavailable"
    )
    changed = replace(
        unit,
        terminal_kind=TerminalKind.PARTIAL,
        terminal=native("partial", paths[:1], list(request.digest)),
    )
    result = project_receipt_effects(replace(attempt, units=(changed,)))
    assert result["units"][0]["completeness"] == "partial"
    assert result["units"][0]["recorded_removed_rollout_count"] == 1
    changed = replace(
        unit,
        terminal=None,
        terminal_kind=TerminalKind.UNKNOWN,
        resolution_kind=TerminalKind.UNKNOWN,
        resolution=Payload(
            "application/vnd.codex-session-gc.closed-work-final-state+json",
            1,
            json.dumps(
                {
                    "state": "UNKNOWN",
                    "request_sha256": request.digest.hex(),
                    "helper_sha256": helper.binary_sha256.hex(),
                    "profile_sha256": helper.evidence.digest.hex(),
                    "original_terminal": "unknown",
                }
            ).encode(),
        ),
    )
    result = project_receipt_effects(replace(attempt, units=(changed,)))
    assert result["units"][0]["recorded_removed_rollout_count"] is None
    assert result["units"][0]["observed_final_state"] == "UNKNOWN"
    assert result["recommended_action"] == "reconcile"
    resolution = json.loads(changed.resolution.data)
    resolution["state"] = "CONFIRMED_FINAL_STATE"
    settled = replace(
        changed,
        resolution_kind=TerminalKind.CONFIRMED,
        resolution=Payload(changed.resolution.content_type, 1, json.dumps(resolution).encode()),
    )
    result = project_receipt_effects(
        replace(attempt, units=(settled,), shutdown=_payload(b"clean"))
    )
    assert result["recommended_action"] == "none"
    assert result["units"][0]["recorded_removed_rollout_count"] is None


def _effects_case(tmp_path: Path) -> tuple[object, object, object]:
    facts, selection, synthetic = _fixture(tmp_path)
    request = _prepare(tmp_path, facts, selection, synthetic)
    _receipt(tmp_path / "effects.sqlite", (None,))
    (attempt,) = inspect(tmp_path / "effects.sqlite")
    unit = attempt.units[0]
    scope = Payload("application/vnd.codex-session-gc.closed-work-apply+json", 1, b"{}")
    unit = replace(unit, unit=replace(unit.unit, evidence=scope))
    raw = json.loads(request.data)
    raw.update(attempt_id=str(attempt.start.attempt_id.value), unit_id=str(unit.unit.unit_id.value))
    request = Payload(request.content_type, 1, json.dumps(raw).encode())
    helper = load_closed_work_contract()
    context = Payload(
        "application/vnd.codex-session-gc.closed-work-apply+json",
        1,
        json.dumps(
            {"helper_sha256": helper.binary_sha256.hex(), "source_identity": helper.source_identity}
        ).encode(),
    )
    attempt = replace(
        attempt,
        start=replace(attempt.start, profile=helper.evidence, evidence=context, process=context),
    )
    return attempt, replace(unit, request=request, terminal_kind=TerminalKind.UNKNOWN), helper


def test_effects_reject_cross_bound_and_incomplete_requests(tmp_path: Path) -> None:
    attempt, unit, _ = _effects_case(tmp_path)
    original = json.loads(unit.request.data)
    for changes in (
        {"attempt_id": str(uuid4())},
        {"unit_id": str(uuid4())},
        {"version": True},
        {"targets": []},
        {"deletion_scope": []},
    ):
        payload = Payload(
            unit.request.content_type, 1, json.dumps({**original, **changes}).encode()
        )
        row = project_receipt_effects(replace(attempt, units=(replace(unit, request=payload),)))
        assert row["units"][0]["evidence_reason"] == "invalid_or_unsupported_evidence"
        assert row["recommended_action"] == "inspect_receipt"
    malformed = Payload(unit.request.content_type, 1, b"[" * 2000 + b"]" * 2000)
    assert (
        project_receipt_effects(replace(attempt, units=(replace(unit, request=malformed),)))[
            "recommended_action"
        ]
        == "inspect_receipt"
    )


def test_effects_whitelist_transport_and_bind_resolution(tmp_path: Path) -> None:
    attempt, unit, helper = _effects_case(tmp_path)
    transport = Payload(
        "application/codex-session-gc.one-shot-observation+json",
        1,
        json.dumps(
            {
                "detail": "lost",
                "returncode": None,
                "stdout_sha256": "0" * 64,
                "stderr_sha256": "0" * 64,
            }
        ).encode(),
    )
    unit = replace(unit, terminal=transport)
    report = project_receipt_effects(replace(attempt, units=(unit,)))
    assert report["units"][0]["evidence_reason"] == "missing_native_result"
    assert report["recommended_action"] == "reconcile"
    unsupported = replace(unit, terminal=Payload("application/other", 1, b"{}"))
    assert (
        project_receipt_effects(replace(attempt, units=(unsupported,)))["recommended_action"]
        == "inspect_receipt"
    )
    evidence = {
        "state": "CONFIRMED_FINAL_STATE",
        "request_sha256": unit.request.digest.hex(),
        "profile_sha256": attempt.start.profile.digest.hex(),
        "helper_sha256": helper.binary_sha256.hex(),
        "original_terminal": "unknown",
    }
    for changes in (
        {"request_sha256": "bad"},
        {"profile_sha256": "bad"},
        {"helper_sha256": "bad"},
        {"state": "invented"},
        {"original_terminal": "refused"},
    ):
        payload = Payload(
            "application/vnd.codex-session-gc.closed-work-final-state+json",
            1,
            json.dumps({**evidence, **changes}).encode(),
        )
        changed = replace(unit, resolution=payload, resolution_kind=TerminalKind.CONFIRMED)
        result = project_receipt_effects(
            replace(attempt, units=(changed,), shutdown=_payload(b"clean"))
        )
        assert result["units"][0]["observed_final_state"] is None
        assert result["recommended_action"] == "inspect_receipt"
    payload = Payload(
        "application/vnd.codex-session-gc.closed-work-final-state+json",
        1,
        json.dumps(evidence).encode(),
    )
    for wrong in (
        Payload("application/other", 1, payload.data),
        Payload(payload.content_type, 2, payload.data),
    ):
        changed = replace(unit, resolution=wrong, resolution_kind=TerminalKind.CONFIRMED)
        assert (
            project_receipt_effects(replace(attempt, units=(changed,)))["recommended_action"]
            == "inspect_receipt"
        )
    changed = replace(unit, resolution=payload, resolution_kind=TerminalKind.CONFIRMED)
    result = project_receipt_effects(
        replace(attempt, units=(changed,), shutdown=_payload(b"clean"))
    )
    assert result["units"][0]["observed_final_state"] == "CONFIRMED_FINAL_STATE"
    assert result["units"][0]["recorded_removed_rollout_count"] is None
    assert result["recommended_action"] == "none"


def test_effects_require_supported_lifecycle_for_not_dispatched(tmp_path: Path) -> None:
    attempt, unit, _ = _effects_case(tmp_path)
    unit = replace(unit, request=None, terminal_kind=None)
    shutdown = Payload(
        "application/vnd.codex-session-gc.closed-work-apply+json",
        1,
        b'{"owned_launches_closed":true}',
    )
    saved = replace(attempt, units=(unit,), shutdown=shutdown)
    assert project_receipt_effects(saved)["units"][0]["evidence_reason"] == "not_dispatched"
    wrong = replace(saved, start=replace(saved.start, evidence=_payload(b"unknown")))
    assert project_receipt_effects(wrong)["units"][0]["completeness"] == "unavailable"
    wrong = replace(saved, shutdown=_payload(b"unknown"))
    assert project_receipt_effects(wrong)["units"][0]["completeness"] == "unavailable"


def test_reviewed_volume_uses_bound_manifest_and_saved_profile(tmp_path: Path) -> None:
    attempt, unit, helper = _effects_case(tmp_path)
    targets = json.loads(unit.request.data)["targets"]
    selected = [target["thread_id"] for target in targets]
    content = {
        "operation_id": str(attempt.start.attempt_id.value),
        "home_identity": [1, 2],
        "roots": selected,
        "selected": selected,
        "cutoff": None,
        "profile_sha256": attempt.start.profile.digest.hex(),
        "schema_version": schema_version(helper),
        "guardian_associations": [],
        "physical_manifest": targets,
        "digest": "",
    }
    declaration = Declaration.model_validate(content)
    content["digest"] = declaration.calculated_digest()
    context = json.loads(attempt.start.evidence.data)
    context["operation_declaration"] = content
    payload = Payload(attempt.start.evidence.content_type, 1, json.dumps(context).encode())
    saved = replace(attempt, start=replace(attempt.start, evidence=payload, process=payload))
    expected = sum(
        rollout["physical_identity"]["size"] for target in targets for rollout in target["rollouts"]
    )
    assert project_receipt_effects(saved)["reviewed_logical_bytes"] == expected
    for changes in (
        {"operation_id": str(uuid4())},
        {"profile_sha256": "bad"},
        {"schema_version": 0},
    ):
        edited = {**content, **changes}
        edited["digest"] = Declaration.model_validate(edited).calculated_digest()
        context["operation_declaration"] = edited
        payload = Payload(payload.content_type, 1, json.dumps(context).encode())
        bad = replace(attempt, start=replace(attempt.start, evidence=payload, process=payload))
        assert project_receipt_effects(bad)["reviewed_logical_bytes"] is None
    context["source_identity"] = "bad"
    payload = Payload(payload.content_type, 1, json.dumps(context).encode())
    bad = replace(attempt, start=replace(attempt.start, evidence=payload, process=payload))
    assert project_receipt_effects(bad)["reviewed_logical_bytes"] is None


def test_effects_use_caller_outcome_including_skipped_roots(tmp_path: Path) -> None:
    attempt, unit, _ = _effects_case(tmp_path)
    shutdown = Payload(
        "application/vnd.codex-session-gc.closed-work-apply+json",
        1,
        b'{"owned_launches_closed":true}',
    )
    saved = replace(
        attempt, units=(replace(unit, request=None, terminal_kind=None),), shutdown=shutdown
    )
    assert (
        project_receipt_effects(saved, outcome=TerminalKind.PARTIAL)["recommended_action"] == "none"
    )


def test_effects_preserve_independent_native_and_resolution_evidence(tmp_path: Path) -> None:
    attempt, unit, helper = _effects_case(tmp_path)
    request = json.loads(unit.request.data)
    paths = [item["rollout_path"] for target in request["targets"] for item in target["rollouts"]]
    native = Payload(
        "application/vnd.codex-session-gc.native-outcome+json",
        1,
        json.dumps(
            {
                "status": "unknown",
                "removed_rollouts": paths,
                "request_sha256": list(unit.request.digest),
            }
        ).encode(),
    )
    resolution = Payload(
        "application/vnd.codex-session-gc.closed-work-final-state+json",
        1,
        json.dumps(
            {
                "state": "CONFIRMED_FINAL_STATE",
                "request_sha256": unit.request.digest.hex(),
                "profile_sha256": attempt.start.profile.digest.hex(),
                "helper_sha256": helper.binary_sha256.hex(),
                "original_terminal": "unknown",
            }
        ).encode(),
    )
    valid = replace(
        unit, terminal=native, resolution=resolution, resolution_kind=TerminalKind.CONFIRMED
    )
    malformed = replace(valid, resolution=Payload("invalid/type", 1, b"{}"))
    result = project_receipt_effects(replace(attempt, units=(malformed,)))
    assert result["units"][0]["recorded_removed_rollout_count"] == len(paths)
    assert result["recommended_action"] == "inspect_receipt"
    result = project_receipt_effects(replace(attempt, units=(valid, malformed)))
    assert result["effects"]["unavailable_unit_count"] == len(result["units"])
    bad_native = replace(valid, terminal=Payload(native.content_type, 1, b"{}"))
    result = project_receipt_effects(replace(attempt, units=(bad_native,)))
    assert result["units"][0]["observed_final_state"] == "CONFIRMED_FINAL_STATE"
    assert result["recommended_action"] == "inspect_receipt"


def test_effects_parse_attempt_context_once_independent_of_unit_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt, unit, _ = _effects_case(tmp_path)
    original = vars(receipt_effects)["_context"]
    calls = []

    def counted(value: object) -> object:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(receipt_effects, "_context", counted)
    for count in (2, 100):
        calls.clear()
        project_receipt_effects(replace(attempt, units=(unit,) * count))
        assert len(calls) == 1
