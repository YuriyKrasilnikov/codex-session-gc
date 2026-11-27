# Copyright (c) 2026

"""Read-only projection of recorded physical effects from receipt evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from codex_session_gc.execution.closed_work_profile import CONTRACT_CONTENT_TYPE, _decode_contract
from codex_session_gc.execution.closed_work_reconcile import FinalState
from codex_session_gc.execution.closed_work_request import CONTENT_TYPE
from codex_session_gc.execution.confirmed_declaration import Declaration, schema_version
from codex_session_gc.execution.operation_status import lifecycle_settled, project_operation_status
from codex_session_gc.execution.physical_manifest import decode_targets
from codex_session_gc.recovery import TerminalKind

REQUEST_VERSION = 7
SHA256_HEX_LENGTH = 64

if TYPE_CHECKING:
    from codex_session_gc.execution.closed_work_request import QualifiedHelperContract
    from codex_session_gc.execution.physical_manifest import WireTarget
    from codex_session_gc.recovery import AttemptStatus, Payload, UnitStatus


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(key)
        result[key] = value
    return result


def _json(payload: Payload) -> object:
    return json.loads(payload.data, object_pairs_hook=_object, parse_constant=_bad_constant)


def _bad_constant(value: str) -> None:
    raise ValueError(value)


def _files(targets: tuple[WireTarget, ...]) -> dict[str, tuple[tuple[int, int], int]]:
    files = {}
    for target in targets:
        for item in target.rollouts:
            stat = item.physical_identity
            if item.rollout_path in files:
                raise ValueError(item.rollout_path)
            files[item.rollout_path] = ((stat.dev, stat.ino), stat.size)
    return files


def _context(attempt: AttemptStatus) -> tuple[dict[str, Any], QualifiedHelperContract]:
    start = attempt.start
    _require(
        condition=start.profile.content_type == CONTRACT_CONTENT_TYPE and start.profile.version == 1
    )
    helper = _decode_contract(start.profile.data)
    _require(
        condition=start.evidence.content_type
        == "application/vnd.codex-session-gc.closed-work-apply+json"
        and start.evidence.version == 1
        and start.process == start.evidence
    )
    context = _json(start.evidence)
    if not isinstance(context, dict):
        raise TypeError
    _require(
        condition=context.get("helper_sha256") == helper.binary_sha256.hex()
        and context.get("source_identity") == helper.source_identity
    )
    return context, helper


def _reviewed(
    attempt: AttemptStatus, context: tuple[dict[str, Any], QualifiedHelperContract] | None
) -> int | None:
    if context is None:
        return None
    try:
        evidence, helper = context
        declaration = Declaration.model_validate(evidence.get("operation_declaration"))
        _require(
            condition=bool(declaration.selected)
            and declaration.operation_id == str(attempt.start.attempt_id.value)
            and declaration.digest == declaration.calculated_digest()
            and declaration.profile_sha256 == attempt.start.profile.digest.hex()
            and declaration.schema_version == schema_version(helper)
        )
        files = _files(declaration.physical_manifest)
        identities = dict(files.values())
        _require(condition=len(identities) == len(files))
        return sum(identities.values())
    except KeyError, TypeError, ValueError, RecursionError:
        return None


def _resolution(
    attempt: AttemptStatus, unit: UnitStatus, helper: QualifiedHelperContract
) -> str | None:
    if unit.resolution is None:
        return None
    payload = unit.resolution
    _require(
        condition=payload.content_type
        == "application/vnd.codex-session-gc.closed-work-final-state+json"
        and payload.version == 1
        and unit.request is not None
    )
    value = _json(payload)
    if not isinstance(value, dict) or unit.request is None:
        raise TypeError
    state = FinalState(value["state"])
    _require(
        condition=value.get("request_sha256") == unit.request.digest.hex()
        and value.get("profile_sha256") == attempt.start.profile.digest.hex()
        and value.get("helper_sha256") == helper.binary_sha256.hex()
        and value.get("original_terminal")
        == (unit.terminal_kind.value if unit.terminal_kind else None)
    )
    expected = TerminalKind.CONFIRMED if state is FinalState.CONFIRMED else TerminalKind.UNKNOWN
    _require(condition=unit.resolution_kind == expected)
    return state.value


def _settled_undispatched(attempt: AttemptStatus) -> bool:
    if not lifecycle_settled(attempt):
        return False
    payload = attempt.lifecycle if attempt.launches else attempt.shutdown
    if payload is None or payload.version != 1:
        return False
    value = _json(payload)
    if not isinstance(value, dict):
        return False
    return (
        payload.content_type == "application/vnd.codex-session-gc.closed-work-apply+json"
        and value.get("owned_launches_closed") is True
    ) or (
        payload.content_type == "application/vnd.codex-session-gc.closed-work-owned-absence+json"
        and value.get("observed") == "registered_owned_children_absent"
        and value.get("atomic") is False
    )


def _unit(
    attempt: AttemptStatus,
    unit: UnitStatus,
    helper: QualifiedHelperContract | None,
    *,
    settled: bool,
) -> tuple[dict[str, Any], set[tuple[object, ...]], bool]:
    row: dict[str, Any] = {
        "recorded_removed_rollout_count": None,
        "recorded_removed_logical_bytes": None,
        "completeness": "unavailable",
        "evidence_reason": "missing_native_result",
        "original_outcome": unit.terminal_kind.value if unit.terminal_kind else None,
        "observed_final_state": None,
    }
    ids: set[tuple[object, ...]] = set()
    if helper is None:
        row["evidence_reason"] = "invalid_or_unsupported_evidence"
        return row, ids, True
    if unit.request is None:
        try:
            scope = unit.unit.evidence
            _require(
                condition=scope.content_type
                == "application/vnd.codex-session-gc.closed-work-apply+json"
                and scope.version == 1
                and isinstance(_json(scope), dict)
            )
            if settled:
                row.update(
                    recorded_removed_rollout_count=0,
                    recorded_removed_logical_bytes=0,
                    completeness="complete",
                    evidence_reason="not_dispatched",
                )
            elif lifecycle_settled(attempt):
                row["evidence_reason"] = "invalid_or_unsupported_evidence"
        except KeyError, TypeError, ValueError, RecursionError:
            row["evidence_reason"] = "invalid_or_unsupported_evidence"
        return row, ids, unit.resolution is not None
    try:
        files = _request_files(attempt, unit)
        ids = {("identity", *identity) for identity, _ in files.values()} | {
            ("path", path) for path in files
        }
    except KeyError, TypeError, ValueError, RecursionError:
        row["evidence_reason"] = "invalid_or_unsupported_evidence"
        return row, ids, unit.resolution is not None
    resolution_invalid = False
    try:
        row["observed_final_state"] = _resolution(attempt, unit, helper)
    except KeyError, TypeError, ValueError, RecursionError:
        resolution_invalid = True
    try:
        _apply_native(row, unit, files)
    except KeyError, TypeError, ValueError, RecursionError:
        row["evidence_reason"] = "invalid_or_unsupported_evidence"
    return row, ids, resolution_invalid


def _apply_native(
    row: dict[str, Any], unit: UnitStatus, files: dict[str, tuple[tuple[int, int], int]]
) -> None:
    terminal = unit.terminal
    if terminal is None:
        return
    if (
        terminal.content_type == "application/codex-session-gc.one-shot-observation+json"
        and terminal.version == 1
    ):
        _transport(terminal)
        return
    paths = _native_paths(unit, files)
    row.update(
        recorded_removed_rollout_count=len(paths),
        recorded_removed_logical_bytes=sum(files[p][1] for p in paths),
        completeness="complete"
        if unit.terminal_kind in {TerminalKind.CONFIRMED, TerminalKind.REFUSED}
        else "partial",
        evidence_reason="native_result",
    )


def _require(*, condition: bool) -> None:
    if not condition:
        raise ValueError


def _request_files(
    attempt: AttemptStatus, unit: UnitStatus
) -> dict[str, tuple[tuple[int, int], int]]:
    request = unit.request
    if request is None:
        raise ValueError
    _require(condition=request.content_type == CONTENT_TYPE and request.version == 1)
    value = _json(request)
    _require(condition=isinstance(value, dict))
    if not isinstance(value, dict):
        raise TypeError
    _require(
        condition=type(value.get("version")) is int and value.get("version") == REQUEST_VERSION
    )
    _require(
        condition=value.get("attempt_id") == str(attempt.start.attempt_id.value)
        and value.get("unit_id") == str(unit.unit.unit_id.value)
    )
    targets = decode_targets(value["targets"])
    selected = value.get("deletion_scope")
    _require(
        condition=bool(targets)
        and isinstance(selected, list)
        and [target.thread_id for target in targets] == selected
    )
    files = _files(targets)
    _require(condition=len({identity for identity, _ in files.values()}) == len(files))
    return files


def _transport(payload: Payload) -> None:
    value = _json(payload)
    if not isinstance(value, dict):
        raise TypeError
    _require(condition=set(value) == {"detail", "returncode", "stdout_sha256", "stderr_sha256"})
    _require(
        condition=isinstance(value["detail"], str)
        and (value["returncode"] is None or type(value["returncode"]) is int)
    )
    for name in ("stdout_sha256", "stderr_sha256"):
        digest = value[name]
        _require(condition=isinstance(digest, str) and len(digest) == SHA256_HEX_LENGTH)
        if not isinstance(digest, str):
            raise TypeError
        _require(condition=all(char in "0123456789abcdef" for char in digest))


def _native_paths(unit: UnitStatus, files: dict[str, tuple[tuple[int, int], int]]) -> list[str]:
    if unit.request is None or unit.terminal is None:
        raise ValueError
    terminal = unit.terminal
    _require(
        condition=terminal.content_type == "application/vnd.codex-session-gc.native-outcome+json"
        and terminal.version == 1
    )
    result = _json(terminal)
    _require(condition=isinstance(result, dict))
    if not isinstance(result, dict):
        raise TypeError
    digest = result.get("request_sha256")
    if not isinstance(digest, list):
        raise TypeError
    _require(condition=all(type(item) is int for item in digest))
    _require(condition=digest == list(unit.request.digest))
    status = TerminalKind(result["status"])
    paths = result["removed_rollouts"]
    _require(condition=status == unit.terminal_kind and isinstance(paths, list))
    if not isinstance(paths, list):
        raise TypeError
    _require(condition=all(type(path) is str for path in paths))
    _require(condition=len(set(paths)) == len(paths) and set(paths) <= files.keys())
    _require(condition=status is not TerminalKind.CONFIRMED or set(paths) == files.keys())
    _require(condition=status is not TerminalKind.REFUSED or not paths)
    return paths


def project_receipt_effects(
    attempt: AttemptStatus, *, outcome: TerminalKind | None = None
) -> dict[str, Any]:
    """Summarize only effects attributed by valid, request-bound native evidence."""
    context = None
    settled = False
    try:
        context = _context(attempt)
        settled = _settled_undispatched(attempt)
    except KeyError, TypeError, ValueError, RecursionError:
        pass
    helper = context[1] if context is not None else None
    projected = [_unit(attempt, unit, helper, settled=settled) for unit in attempt.units]
    rows = [row for row, _, _ in projected]
    identities = [ids for _, ids, _ in projected]
    owners: dict[tuple[object, ...], int] = {}
    ambiguous: set[int] = set()
    for index, ids in enumerate(identities):
        for identity in ids:
            if identity in owners:
                ambiguous.update((index, owners[identity]))
            else:
                owners[identity] = index
    for index in ambiguous:
        rows[index].update(
            recorded_removed_rollout_count=None,
            recorded_removed_logical_bytes=None,
            completeness="unavailable",
            evidence_reason="invalid_or_unsupported_evidence",
        )
    partial = sum(row["completeness"] == "partial" for row in rows)
    unavailable = sum(row["completeness"] == "unavailable" for row in rows)
    completeness = (
        "unavailable"
        if not rows or unavailable == len(rows)
        else "partial"
        if partial or unavailable
        else "complete"
    )
    if outcome is None:
        outcome = project_operation_status(attempt).outcome
    invalid = any(
        row["evidence_reason"] == "invalid_or_unsupported_evidence" for row in rows
    ) or any(resolution_invalid for _, _, resolution_invalid in projected)
    unresolved = (
        outcome is TerminalKind.UNKNOWN
        or any(unit.unresolved for unit in attempt.units)
        or not lifecycle_settled(attempt)
    )
    action = "inspect_receipt" if invalid else "reconcile" if unresolved else "none"
    if not rows:
        action = "inspect_receipt"
    return {
        "units": rows,
        "effects": {
            "recorded_removed_rollout_count": sum(
                row["recorded_removed_rollout_count"] or 0 for row in rows
            )
            if unavailable < len(rows)
            else None,
            "recorded_removed_logical_bytes": sum(
                row["recorded_removed_logical_bytes"] or 0 for row in rows
            )
            if unavailable < len(rows)
            else None,
            "partial_unit_count": partial,
            "unavailable_unit_count": unavailable,
            "completeness": completeness,
        },
        "reviewed_logical_bytes": _reviewed(attempt, context),
        "observed_at": datetime.now(UTC).isoformat(),
        "recommended_action": action,
    }
