# Copyright (c) 2026
# ruff: noqa: EM101, TRY003, S608, SLF001

"""Observe only saved selected effects after an uncertain one-shot delivery.

Confirmation describes a bounded, non-atomic final-state observation. It neither
attributes deletion nor certifies perpetual absence, and never resends a unit.
Only the owned recovery journal is opened as a writable source database.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from codex_session_gc.domain import GuardianAssociation, ThreadId
from codex_session_gc.execution import closed_work_request as collector
from codex_session_gc.execution.closed_work_apply import DispatchLimits
from codex_session_gc.execution.confirmed_declaration import Declaration, saved_declaration
from codex_session_gc.execution.parent_relation import (
    GuardianParent,
    parse_parent_relation,
)
from codex_session_gc.execution.physical_manifest import decode_targets, history_cache_keys
from codex_session_gc.execution.process_identity import ChildIdentity, Presence, child_presence
from codex_session_gc.execution.selection_provenance import reviewed_associations
from codex_session_gc.recovery import (
    AttemptId,
    AttemptStatus,
    LaunchStatus,
    Payload,
    ReceiptRecoveryRequiredError,
    RecoveryError,
    TerminalKind,
    UnitId,
    UnitStatus,
    inspect,
    record_launch_absence,
    record_lifecycle_resolution,
    record_reconciliation_evidence,
    recover,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_REQUEST_VERSION = 6


class FinalState(StrEnum):
    """Result labels separate from the helper's original delivery outcome."""

    CONFIRMED = "CONFIRMED_FINAL_STATE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """One bounded observation and its honest journal persistence status."""

    state: FinalState
    evidence: Payload
    recorded: bool
    original_terminal: TerminalKind | None


@dataclass(frozen=True, slots=True)
class _Request:
    home: Path
    sqlite_home: Path
    root: str
    selected: tuple[str, ...]
    paths: tuple[Path, ...]
    raw_paths: tuple[Path, ...]
    history_keys: tuple[str, ...]
    original_databases: frozenset[collector.DatabaseKind]


def reconcile(
    receipt: Path,
    attempt_id: AttemptId,
    unit_id: UnitId,
    *,
    helper: collector.QualifiedHelperContract,
    limits: collector.CollectionLimits,
) -> ReconciliationResult:
    """Inspect one saved intent, observe owned-child absence, then selected effects.

    Unknown observations are append-only evidence, never mutation authorization.
    Existing native PARTIAL/UNKNOWN delivery records and exact intent stay intact.
    No helper is started; no later unit is dispatched or automatically continued.
    """
    state = FinalState.UNKNOWN
    original = None
    request = None
    unit = None
    detail = "unavailable"
    budget = collector.CollectionBudget(limits, time.monotonic() + limits.deadline_seconds)
    try:
        collector._absolute(receipt)
        _receipt_extent(receipt, budget)
        attempt, unit = _saved(receipt, attempt_id, unit_id)
        original = unit.terminal_kind
        request = unit.request
        if request is None or not unit.unresolved or attempt.start.profile != helper.evidence:
            raise collector.PreparationRefused("No unresolved intent bound to this helper profile.")
        parsed = _request(request, receipt, attempt_id, unit_id, limits)
        _declaration(attempt, unit, request, helper, limits)
        _absent_launches(attempt)
        _observe(parsed, helper, budget)
        _receipt_extent(receipt, budget)
        refreshed, _ = _saved(receipt, attempt_id, unit_id)
        if refreshed.launches != attempt.launches:
            raise collector.PreparationRefused(
                "The owned launch ledger changed during observation."
            )
        _absent_launches(refreshed)
        absence = Payload(
            "application/vnd.codex-session-gc.closed-work-owned-absence+json",
            1,
            json.dumps(
                {
                    "request_sha256": request.digest.hex(),
                    "atomic": False,
                    "observed": "registered_owned_children_absent",
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        )
        _record_absence(receipt, attempt_id, absence)
        state = FinalState.CONFIRMED
        detail = "selected_effects_absent"
    except OSError, ValueError, TypeError, sqlite3.Error, RecoveryError:
        detail = "unavailable_or_selected_effects_present"
    evidence = Payload(
        "application/vnd.codex-session-gc.closed-work-final-state+json",
        1,
        json.dumps(
            {
                "state": state.value,
                "detail": detail,
                "request_sha256": request.digest.hex() if request else None,
                "helper_sha256": helper.binary_sha256.hex(),
                "profile_sha256": helper.evidence.digest.hex(),
                "original_terminal": original.value if original else None,
                "atomic": False,
                "attribution": False,
                "perpetual_absence": False,
                "captured_bytes": budget.used,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    )
    recorded = False
    if unit is not None and request is not None and unit.unresolved:
        try:
            record_reconciliation_evidence(
                receipt,
                attempt_id,
                unit_id,
                request.digest,
                TerminalKind.CONFIRMED if state is FinalState.CONFIRMED else TerminalKind.UNKNOWN,
                evidence,
            )
            recorded = True
        except OSError, RecoveryError, ValueError, sqlite3.Error:
            # Observation evidence remains valid, but recovery did not become durable.
            recorded = False
    return ReconciliationResult(state, evidence, recorded, original)


def _confirmed_scope(declaration: Declaration, context: dict[str, object]) -> None:
    """Bind the optional MCP inputs to the original per-root saved plan."""
    if declaration.cutoff != context["cutoff"]:
        raise ValueError("The confirmed cutoff differs from the saved apply context.")
    confirmed = {binding.association for binding in declaration.guardian_associations}
    scopes = context["scopes"]
    if not isinstance(scopes, list):
        raise TypeError("The saved operation scopes are unavailable.")
    for scope in scopes:
        if (
            not isinstance(scope, dict)
            or scope.get("root") not in declaration.roots
            or not isinstance(scope.get("requested_threads"), list)
            or not set(scope["requested_threads"]).issubset(declaration.selected)
        ):
            raise ValueError("The saved unit exceeds the confirmed exact selection.")
        if not set(reviewed_associations(scope.get("guardian_associations"))).issubset(confirmed):
            raise ValueError("The saved Guardian association differs from confirmation.")


def _declaration(  # noqa: C901, PLR0912
    attempt: AttemptStatus,
    unit: UnitStatus,
    request: Payload,
    helper: collector.QualifiedHelperContract,
    limits: collector.CollectionLimits,
) -> None:
    """Bind existing apply declarations before observing a process or work home."""
    start = attempt.start
    payloads = (start.evidence, start.process, start.plan, unit.unit.evidence)
    for payload in payloads:
        if (
            payload.content_type != "application/vnd.codex-session-gc.closed-work-apply+json"
            or payload.version != 1
            or len(payload.data) > limits.maximum_request_bytes
        ):
            raise ValueError("The saved apply declaration format is unavailable.")
    if start.evidence != start.process:
        raise ValueError("The saved process and observation context disagree.")
    context = _object(start.evidence.data)
    fields = {
        "codex_home",
        "sqlite_home",
        "journal",
        "helper_path",
        "helper_sha256",
        "source_identity",
        "provider",
        "cutoff",
        "collection_limits",
        "dispatch_limits",
        "scopes",
    }
    if set(context) not in (fields, fields | {"operation_declaration"}):
        raise ValueError("The saved apply context is incomplete.")
    if "operation_declaration" in context:
        declaration = saved_declaration(
            context["operation_declaration"], start.attempt_id.value, helper
        )
        _confirmed_scope(declaration, context)

    raw = _object(request.data)
    if "operation_declaration" in context:
        confirmed_targets = {target.thread_id: target for target in declaration.physical_manifest}
        for target in decode_targets(raw["targets"]):
            if confirmed_targets.get(target.thread_id) != target:
                raise ValueError("Saved physical effects differ from the confirmed manifest.")
    if any(
        context[key] != raw[key]
        for key in ("codex_home", "sqlite_home", "journal", "provider", "cutoff")
    ):
        raise ValueError("Saved storage context disagrees with the request.")
    if (
        context["helper_sha256"] != helper.binary_sha256.hex()
        or context["source_identity"] != helper.source_identity
    ):
        raise ValueError("The saved helper context disagrees with its profile.")
    collector._absolute(Path(_text(context["helper_path"])))
    for key, value_type in (
        ("collection_limits", collector.CollectionLimits),
        ("dispatch_limits", DispatchLimits),
    ):
        values = context[key]
        if not isinstance(values, dict) or set(values) != set(value_type.__dataclass_fields__):
            raise ValueError("The saved execution bounds are unavailable.")
        value_type(**values)
    scopes = context["scopes"]
    if not isinstance(scopes, list) or len(scopes) != len(attempt.units):
        raise ValueError("The saved plan and unit declaration count disagree.")
    if start.plan != _apply_payload(scopes):
        raise ValueError("The saved plan differs from its apply context.")
    occupied: set[str] = set()
    for stored, scope in zip(attempt.units, scopes, strict=True):
        if not isinstance(scope, dict) or set(scope) != {
            "root",
            "ownership_scope",
            "requested_threads",
            "deletion_scope",
            "guardian_associations",
        }:
            raise ValueError("The saved work declaration is incomplete.")
        owners = _ids(scope["ownership_scope"])
        selected = _ids(scope["deletion_scope"])
        requested = _ids(scope["requested_threads"])
        if (
            scope["root"] not in owners
            or not set(selected).issubset(requested)
            or not set(requested).issubset(owners)
            or occupied.intersection(owners)
        ):
            raise ValueError("The saved work declarations are inconsistent.")
        _saved_associations(scope, raw if stored.unit.unit_id == unit.unit.unit_id else None)
        occupied.update(owners)
        if stored.unit.evidence != _apply_payload(scope):
            raise ValueError("A saved unit differs from its declared work.")
        if stored.unit.unit_id == unit.unit.unit_id:
            ownership = cast("dict[str, object]", raw["ownership_scope"])
            if (
                scope["root"] != ownership["root_thread_id"]
                or scope["ownership_scope"] != ownership["thread_ids"]
                or scope["deletion_scope"] != raw["deletion_scope"]
            ):
                raise ValueError("The intent exceeds its saved work declaration.")


def _saved_associations(scope: dict[str, object], raw: dict[str, object] | None) -> None:
    """Validate selected associations without inferring them from a live namespace."""
    associations = reviewed_associations(scope["guardian_associations"])
    selected = _ids(scope["deletion_scope"])
    if any(str(item.guardian_thread_id.value) not in selected for item in associations):
        raise ValueError("The saved Guardian association exceeds selected work.")
    if raw is None:
        return
    targets = raw["targets"]
    if not isinstance(targets, list):
        raise TypeError("Saved target relations are unavailable.")
    expected = []
    for target in targets:
        if not isinstance(target, dict):
            raise TypeError("Saved target relation is not an object.")
        relation = parse_parent_relation(target["parent_relation"])
        if isinstance(relation, GuardianParent):
            expected.append(
                GuardianAssociation(
                    ThreadId.parse(target["thread_id"]), relation.associated_parent_thread_id
                )
            )
    if tuple(sorted(expected, key=lambda item: item.guardian_thread_id)) != associations:
        raise ValueError("Saved Guardian associations disagree with target relations.")


def _apply_payload(value: object) -> Payload:
    """Encode the existing apply producer's immutable declaration representation."""
    return Payload(
        "application/vnd.codex-session-gc.closed-work-apply+json",
        1,
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
    )


def _receipt_extent(receipt: Path, budget: collector.CollectionBudget) -> None:
    """Bound sampled receipt/sidecar extents before the known-format journal reader.

    These sampled extents are not copied-byte or kernel-I/O accounting. SQLite
    and procfs observations remain cooperative, with no kernel deadline promise.
    """
    total = 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        budget.check()
        try:
            descriptor = collector._open(Path(f"{receipt}{suffix}"))
        except FileNotFoundError:
            if suffix:
                continue
            raise
        try:
            size = os.fstat(descriptor).st_size
            if size > budget.limits.maximum_database_bytes:
                raise collector.PreparationRefused("A receipt component exceeds its sampled bound.")
            total += size
        finally:
            os.close(descriptor)
    if total > budget.limits.maximum_total_bytes:
        raise collector.PreparationRefused("Receipt extents exceed the caller observation bound.")


def _saved(
    receipt: Path, attempt_id: AttemptId, unit_id: UnitId
) -> tuple[AttemptStatus, UnitStatus]:
    try:
        attempts = inspect(receipt)
    except ReceiptRecoveryRequiredError:
        recover(receipt)  # The journal validates its own known format before writable recovery.
        attempts = inspect(receipt)
    matches = [item for item in attempts if item.start.attempt_id == attempt_id]
    if len(matches) != 1:
        raise collector.PreparationRefused("The saved attempt is absent or ambiguous.")
    units = [item for item in matches[0].units if item.unit.unit_id == unit_id]
    if len(units) != 1:
        raise collector.PreparationRefused("The saved unit is absent or ambiguous.")
    return matches[0], units[0]


def _absent_launches(attempt: AttemptStatus) -> None:
    if not attempt.launches:
        raise collector.PreparationRefused("No owned launch ledger was saved.")
    for launch in attempt.launches:
        if launch.status is LaunchStatus.PENDING:
            raise collector.PreparationRefused("An unfenced launcher can still accept a grant.")
        if launch.registration is None:
            # Fencing prevents admission but is not proof that the waiting PID was reaped.
            raise collector.PreparationRefused("Owned launcher absence is unavailable.")
        if child_presence(_identity(launch.registration.incarnation)) is not Presence.ABSENT:
            raise collector.PreparationRefused("The recorded owned child is not observed absent.")


def _identity(payload: Payload) -> ChildIdentity:
    if (
        payload.content_type != "application/codex-session-gc.child-identity+json"
        or payload.version != 1
    ):
        raise collector.PreparationRefused("The stored child identity format is unsupported.")
    value = _object(payload.data)
    if set(value) != set(ChildIdentity.__dataclass_fields__):
        raise collector.PreparationRefused("The stored child identity is incomplete.")
    integers = {"pid", "start_ticks", "uid"}
    if any(type(value[key]) is not int or cast("int", value[key]) < 0 for key in integers):
        raise collector.PreparationRefused("The stored child coordinates are invalid.")
    if cast("int", value["pid"]) < 1 or any(
        type(value[key]) is not str or not value[key] for key in value.keys() - integers
    ):
        raise collector.PreparationRefused("The stored child coordinates are invalid.")
    return ChildIdentity(
        pid=cast("int", value["pid"]),
        start_ticks=cast("int", value["start_ticks"]),
        uid=cast("int", value["uid"]),
        marker=_text(value["marker"]),
        boot_id=_text(value["boot_id"]),
        pid_namespace=_text(value["pid_namespace"]),
        mount_namespace=_text(value["mount_namespace"]),
        proc_mount=_text(value["proc_mount"]),
    )


def _record_absence(receipt: Path, attempt_id: AttemptId, evidence: Payload) -> None:
    attempts = inspect(receipt)
    attempt = next((item for item in attempts if item.start.attempt_id == attempt_id), None)
    if attempt is None:
        raise collector.PreparationRefused("The saved attempt changed.")
    _absent_launches(attempt)
    for launch in attempt.launches:
        if launch.status is LaunchStatus.REGISTERED:
            record_launch_absence(
                receipt, attempt_id, launch.intent.launch_id, launch.intent.generation, evidence
            )
    if attempt.lifecycle_unresolved:
        record_lifecycle_resolution(
            receipt, attempt_id, attempt.launch_generation, TerminalKind.CONFIRMED, evidence
        )


def _object(raw: bytes) -> dict[str, object]:
    def pairs(items: Iterable[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate request keys.")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise TypeError("An intent must be one JSON object.")
    return value


def _ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(type(item) is not str for item in value):
        raise ValueError("Scope identifiers are unavailable.")
    values = tuple(str(ThreadId.parse(item).value) for item in value)
    if len(set(values)) != len(values) or list(values) != value:
        raise ValueError("Scope identifiers are not unique canonical IDs.")
    return values


def _request(  # noqa: C901, PLR0912, PLR0915
    payload: Payload,
    receipt: Path,
    attempt: AttemptId,
    unit: UnitId,
    limits: collector.CollectionLimits,
) -> _Request:
    if (
        payload.content_type != collector.CONTENT_TYPE
        or payload.version != 1
        or len(payload.data) > limits.maximum_request_bytes
    ):
        raise ValueError("Unsupported saved native request.")
    value = _object(payload.data)
    expected = {
        "version",
        "journal",
        "attempt_id",
        "unit_id",
        "codex_home",
        "sqlite_home",
        "provider",
        "cutoff",
        "ownership_scope",
        "deletion_scope",
        "targets",
        "databases",
        "guard_paths",
    }
    if (
        set(value) != expected
        or type(value["version"]) is not int
        or value["version"] != _REQUEST_VERSION
    ):
        raise ValueError("Unsupported saved native request fields.")
    if (
        value["journal"] != str(receipt)
        or value["attempt_id"] != str(attempt.value)
        or value["unit_id"] != str(unit.value)
    ):
        raise ValueError("Request identities disagree with its saved intent.")
    _text(value["provider"])
    cutoff = datetime.fromisoformat(_text(value["cutoff"]))
    if cutoff.tzinfo is None:
        raise ValueError("Cutoff must have an explicit timezone.")
    elapsed = cutoff.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    cutoff_ms = elapsed.days * 86400000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000
    home, sqlite_home = Path(_text(value["codex_home"])), Path(_text(value["sqlite_home"]))
    collector._absolute(home)
    collector._absolute(sqlite_home)
    ownership = value["ownership_scope"]
    if not isinstance(ownership, dict) or set(ownership) != {"root_thread_id", "thread_ids"}:
        raise ValueError("Ownership scope is unavailable.")
    owners = _ids(ownership["thread_ids"])
    root = ownership["root_thread_id"]
    selected = _ids(value["deletion_scope"])
    if type(root) is not str or root not in owners or not set(selected).issubset(owners):
        raise ValueError("Deletion exceeds the saved ownership scope.")
    targets = decode_targets(value["targets"])
    if tuple(target.thread_id for target in targets) != selected:
        raise ValueError("Exact logical target identities disagree with selection.")
    paths = []
    for target in targets:
        if target.updated_at_ms >= cutoff_ms:
            raise ValueError("The saved target activity is not before cutoff.")
        for physical in target.rollouts:
            path = Path(physical.rollout_path)
            if not any(
                path.is_relative_to(home / directory)
                for directory in ("sessions", "archived_sessions")
            ):
                raise ValueError("A saved physical path exceeds the managed namespace.")
            paths.append(path)
    databases = value["databases"]
    if not isinstance(databases, list):
        raise TypeError("Original database bounds are unavailable.")
    originals = set()
    for bound in databases:
        if not isinstance(bound, dict) or type(bound.get("path")) is not str:
            raise ValueError("A database bound is invalid.")
        if set(bound) != {"path", "rows", "queue_bookkeeping", "board_tombstone"}:
            raise ValueError("Unsupported database bound fields.")
        path = Path(bound["path"])
        kind = collector.DatabaseKind(path.name)
        if path != sqlite_home / kind.value or kind in originals:
            raise ValueError("A database bound exceeds the native namespace.")
        if bound.get("queue_bookkeeping") is not (
            kind is collector.DatabaseKind.QUEUE
        ) or bound.get("board_tombstone") != (
            root if kind is collector.DatabaseKind.BOARD and root in selected else None
        ):
            raise ValueError("A database effect declaration is inconsistent.")
        rows = bound["rows"]
        tables = set(collector._TABLES[kind])
        if kind is collector.DatabaseKind.QUEUE:
            tables.add("sqlite_sequence")
        if not isinstance(rows, dict) or set(rows) != tables:
            raise ValueError("Database row declarations exceed the profile.")
        for keys in rows.values():
            if not isinstance(keys, list) or any(
                type(key) is not int or not -(2**63) <= key < 2**63 for key in keys
            ):
                raise ValueError("Database row identities are invalid.")
            if keys != sorted(set(keys)):
                raise ValueError("Database row identities are ambiguous.")
        originals.add(kind)
    if collector.DatabaseKind.STATE not in originals or len(set(paths)) != len(paths):
        raise ValueError("The authoritative storage identity is unavailable.")
    guards = {
        str(home / ".tmp/rollout-maintenance.lock"),
        str(home / "thread-writer-locks/.coordination.lock"),
    }
    guards.update(str(home / f"thread-writer-locks/{identifier}.lock") for identifier in owners)
    declared = value["guard_paths"]
    if (
        not isinstance(declared, list)
        or any(type(path) is not str for path in declared)
        or declared != sorted(set(declared))
        or not guards.issubset(declared)
    ):
        raise ValueError("Required saved guards are missing or ambiguous.")
    supplemental = set(declared) - guards
    for text in supplemental:
        path = Path(text)
        identifier = str(ThreadId.parse(path.stem).value)
        if text != str(home / f"thread-writer-locks/{identifier}.lock"):
            raise ValueError("Supplemental guards require canonical thread-writer paths.")
    return _Request(
        home,
        sqlite_home,
        root,
        selected,
        tuple(paths),
        tuple(Path(target.current_rollout_path) for target in targets),
        history_cache_keys(targets),
        frozenset(originals),
    )


def _text(value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not value and not allow_empty):
        raise TypeError("An exact native text field is unavailable.")
    return value


def _filename(path: Path, identity: str) -> None:
    match = re.fullmatch(
        r"rollout-([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2})-(.+)\.jsonl",
        path.name,
    )
    if match is None:
        raise ValueError("The saved filename exceeds the native profile.")
    datetime.strptime(match[1], "%Y-%m-%dT%H-%M-%S").replace(tzinfo=UTC)
    original, separator, replacement = match[2].partition("_")
    ThreadId.parse(original)
    if str(ThreadId.parse(replacement if separator else original).value) != identity:
        raise ValueError("Filename identity disagrees with the selected target.")


def _observe(
    request: _Request, helper: collector.QualifiedHelperContract, budget: collector.CollectionBudget
) -> None:
    namespace = {
        item.kind: collector._sample(request.sqlite_home / item.kind.value)
        for item in helper.databases
    }
    if any(namespace[kind] is None for kind in request.original_databases):
        raise collector.PreparationRefused("A required original database disappeared.")
    _paths_absent(request.paths)
    for contract in helper.databases:
        if namespace[contract.kind] is None:
            continue
        source = request.sqlite_home / contract.kind.value
        with collector._read_transaction(source, budget) as connection:
            collector.validate_database_schema(connection, contract)
            _effects_absent(connection, contract.kind, request, budget)
    if any(
        collector._sample(request.sqlite_home / kind.value) != identity
        for kind, identity in namespace.items()
    ):
        raise collector.PreparationRefused("The observed database namespace changed.")
    _paths_absent(request.paths)
    budget.check()


def _paths_absent(paths: tuple[Path, ...]) -> None:
    for path in paths:
        if collector._sample(path) is not None:
            raise collector.PreparationRefused("Selected physical history is present.")


def _effects_absent(
    connection: sqlite3.Connection,
    kind: collector.DatabaseKind,
    request: _Request,
    budget: collector.CollectionBudget,
) -> None:
    effect_ids = (
        request.history_keys if kind is collector.DatabaseKind.HISTORY else request.selected
    )
    marks = ",".join("?" for _ in effect_ids)
    if kind is collector.DatabaseKind.STATE:
        paths = tuple(str(path) for path in (*request.paths, *request.raw_paths))
        path_marks = ",".join("?" for _ in paths)
        if connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM threads WHERE rollout_path IN ({path_marks}))", paths
        ).fetchone()[0]:
            raise collector.PreparationRefused("A known selected physical path remains catalogued.")
    for table, column in collector._TABLES[kind].items():
        budget.check()
        params = effect_ids
        if kind is collector.DatabaseKind.BOARD:
            if request.root not in request.selected:
                continue
            exists = connection.execute(
                f"SELECT EXISTS(SELECT 1 FROM {table} WHERE board=?)", (request.root,)
            ).fetchone()[0]
            tombstone = table == "deleted_boards"
            required_marker = kind in request.original_databases and tombstone
            if bool(exists) != required_marker:
                raise collector.PreparationRefused("Selected board final state is not observed.")
            continue
        if column is None:
            clause = f"parent_thread_id IN ({marks}) OR child_thread_id IN ({marks})"
            params = request.selected + request.selected
        else:
            clause = f"{column} IN ({marks})"
            if table == "jobs":
                clause += " AND kind='memory_stage1'"
        if connection.execute(
            f"SELECT EXISTS(SELECT 1 FROM {table} WHERE {clause})", params
        ).fetchone()[0]:
            raise collector.PreparationRefused("A selected logical effect remains present.")
