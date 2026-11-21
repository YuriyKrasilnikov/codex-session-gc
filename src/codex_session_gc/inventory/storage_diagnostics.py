# Copyright (c) 2026
# ruff: noqa: C901, PLR0912, PLR0915, SLF001, EM101, TRY003

"""Detached diagnostic subjects and SQL relationship witnesses."""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict, deque
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from codex_session_gc.domain import RolloutId, ThreadId
from codex_session_gc.inventory.diagnostic_sql import (
    DEFAULT_DIAGNOSTIC_LIMITS,
    DiagnosticLimits,
    SqlFacts,
    observe_sql,
)
from codex_session_gc.inventory.storage import (
    PAGE_LIMIT,
    StorageSnapshot,
    _check_cancelled,
    _verify_root,
)
from codex_session_gc.inventory.storage_walk import StorageScanError

if TYPE_CHECKING:
    from collections.abc import Callable

Direction = Literal["incoming", "outgoing", "all"]


@dataclass(frozen=True, slots=True)
class Subject:
    """One physical row, SQL thread, or identifiable dangling endpoint."""

    subject_id: str
    kind: str
    thread_id: str | None
    rollout_id: str | None
    object_id: str | None
    original_association: str | None
    sql_owner: str
    current_pointer: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Evidence:
    """A directed witness or derived relationship between subjects."""

    source: str | None
    target: str | None
    kind: str
    origin: str
    reason: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticObservation:
    """An immutable result; all page operations are detached from I/O."""

    diagnostic_id: str
    storage_snapshot_id: str
    _summary: dict[str, Any]
    _subjects: tuple[Subject, ...]
    _evidence: tuple[Evidence, ...]
    _ambiguous_rollouts: frozenset[str] = frozenset()

    def summary(self) -> dict[str, Any]:
        """Return coverage and independent source metrics."""
        return {
            **deepcopy(self._summary),
            "diagnostic_id": self.diagnostic_id,
            "storage_snapshot_id": self.storage_snapshot_id,
        }

    def subjects(
        self, *, reason: str = "all", offset: int = 0, limit: int = PAGE_LIMIT
    ) -> dict[str, Any]:
        """Filter subjects before applying pagination."""
        _page_args(offset, limit)
        if not isinstance(reason, str) or not reason:
            raise ValueError("Invalid diagnostic reason.")
        values = tuple(
            s
            for s in self._subjects
            if reason == "all" or (reason == "problems" and bool(s.reasons)) or reason in s.reasons
        )
        return _page(self.diagnostic_id, values, offset, limit)

    def lookup(
        self,
        *,
        thread_id: ThreadId | None = None,
        rollout_id: RolloutId | None = None,
        offset: int = 0,
        limit: int = PAGE_LIMIT,
    ) -> dict[str, Any]:
        """Resolve a typed identity to observed candidates without further I/O."""
        _page_args(offset, limit)
        if (
            (thread_id is None) == (rollout_id is None)
            or (thread_id is not None and type(thread_id) is not ThreadId)
            or (rollout_id is not None and type(rollout_id) is not RolloutId)
        ):
            raise ValueError("Select exactly one typed diagnostic identity.")
        if thread_id is not None:
            values = tuple(s for s in self._subjects if s.thread_id == str(thread_id.value))
            selected = next((s for s in values if s.kind in ("thread", "endpoint")), None)
            if selected is None and len(values) == 1:
                selected = values[0]
        else:
            if rollout_id is None:
                raise ValueError("Select exactly one typed diagnostic identity.")
            values = tuple(s for s in self._subjects if s.rollout_id == str(rollout_id.value))
            selected = (
                values[0]
                if len(values) == 1 and str(rollout_id.value) not in self._ambiguous_rollouts
                else None
            )
        if selected is not None:
            result = self.explain(selected.subject_id, offset=offset, limit=limit)
            result["status"] = "observed"
        else:
            result = _page(self.diagnostic_id, values, offset, limit)
            known_ambiguous = (
                rollout_id is not None and str(rollout_id.value) in self._ambiguous_rollouts
            )
            result["status"] = "ambiguous" if values or known_ambiguous else "subject_not_observed"
        result["coverage"] = dict(self._summary["coverage"])
        result["original_association_observation"] = deepcopy(
            self._summary["original_association_observation"]
        )
        return result

    def explain(
        self,
        subject_id: str,
        *,
        direction: Direction = "all",
        offset: int = 0,
        limit: int = PAGE_LIMIT,
    ) -> dict[str, Any]:
        """Return one subject and a filtered page of its directed witnesses."""
        _page_args(offset, limit)
        if type(subject_id) is not str or direction not in ("incoming", "outgoing", "all"):
            raise ValueError("Invalid diagnostic explanation arguments.")
        subject = next(
            (s for s in self._subjects if subject_id in (s.subject_id, s.object_id)),
            None,
        )
        if subject is None:
            raise ValueError("Unknown diagnostic subject.")
        values = tuple(
            e
            for e in self._evidence
            if (direction in ("outgoing", "all") and e.source == subject.subject_id)
            or (direction in ("incoming", "all") and e.target == subject.subject_id)
        )
        result = _page(self.diagnostic_id, values, offset, limit)
        requested = {
            key
            for item in result["items"]
            for key in (item["source"], item["target"])
            if key is not None
        }
        labels = {
            item.subject_id: {
                "kind": item.kind,
                "thread_id": item.thread_id,
                "rollout_id": item.rollout_id,
                "object_id": item.object_id,
            }
            for item in self._subjects
            if item.subject_id in requested
        }
        for item in result["items"]:
            item["source_subject"] = labels.get(item["source"])
            item["target_subject"] = labels.get(item["target"])
        result["subject"] = asdict(subject)
        result["direction"] = direction
        result["original_association_observation"] = deepcopy(
            self._summary["original_association_observation"]
        )
        return result


def _variable_bytes(value: object) -> int:
    """Charge JSON-encoded variable values separately from SQL input bytes."""
    if isinstance(value, str):
        return len(json.dumps(value, ensure_ascii=True).encode("ascii"))
    if isinstance(value, dict):
        return sum(_variable_bytes(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return sum(_variable_bytes(item) for item in value)
    return 0


def _page_args(offset: int, limit: int) -> None:
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= PAGE_LIMIT
    ):
        raise ValueError("Invalid diagnostic page arguments.")


def _page(diagnostic_id: str, values: tuple[Any, ...], offset: int, limit: int) -> dict[str, Any]:
    end = offset + limit
    return {
        "diagnostic_id": diagnostic_id,
        "total_matching": len(values),
        "offset": offset,
        "limit": limit,
        "next_offset": end if end < len(values) else None,
        "items": [asdict(value) for value in values[offset:end]],
    }


def _cycles(
    nodes: set[str], links: list[tuple[str, str]], cancelled: Callable[[], bool]
) -> set[str]:
    """Iterative Kosaraju traversal, including self-loops."""
    forward: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    for source, target in links:
        forward[source].append(target)
        reverse[target].append(source)
    seen: set[str] = set()
    order: list[str] = []
    for node in nodes:
        _check_cancelled(cancelled)
        if node in seen:
            continue
        seen.add(node)
        stack = [(node, iter(forward[node]))]
        while stack:
            _check_cancelled(cancelled)
            current, children = stack[-1]
            child = next(children, None)
            if child is None:
                order.append(current)
                stack.pop()
            elif child not in seen:
                seen.add(child)
                stack.append((child, iter(forward[child])))
    seen.clear()
    cyclic: set[str] = set()
    loops = {source for source, target in links if source == target}
    for node in reversed(order):
        if node in seen:
            continue
        seen.add(node)
        component = []
        todo = [node]
        while todo:
            _check_cancelled(cancelled)
            current = todo.pop()
            component.append(current)
            for parent in reverse[current]:
                if parent not in seen:
                    seen.add(parent)
                    todo.append(parent)
        if len(component) > 1 or node in loops:
            cyclic.update(component)
    return cyclic


def _pointer(home: Path, parts: tuple[str, ...] | None, raw: str | None) -> str:
    if parts is None or raw is None:
        return "unknown"
    path = Path(raw)
    if "\0" in raw or not path.is_absolute() or ".." in path.parts or str(path) != raw:
        return "unknown"
    if not path.is_relative_to(home):
        return "unknown"
    observed = home.joinpath(*parts)
    alternate = (
        observed.with_suffix("")
        if observed.name.endswith(".jsonl.zst")
        else observed.with_name(observed.name + ".zst")
    )
    return "current" if path in (observed, alternate) else "different"


def _project(
    home: Path,
    snapshot: StorageSnapshot,
    sql: SqlFacts,
    limits: DiagnosticLimits,
    cancelled: Callable[[], bool],
) -> DiagnosticObservation:
    diagnostic_id = f"diag/{uuid4()}"
    thread_rows = {row.thread_id: row for row in sql.threads}
    duplicate_threads = {
        key for key, count in Counter(row.thread_id for row in sql.threads).items() if count > 1
    }
    endpoints = {
        item for edge in sql.edges for item in (edge.parent, edge.child) if item is not None
    }
    endpoints.update(row.attachment for row in sql.threads if row.attachment is not None)
    endpoints.update(
        row.filename_thread_id for row in snapshot._rows if row.filename_thread_id is not None
    )
    physical_rows = snapshot._rows[: limits.max_subjects]
    all_identifiers = sorted(set(thread_rows) | endpoints)
    identifiers = all_identifiers[: max(0, limits.max_subjects - len(physical_rows))]
    subjects_complete = len(physical_rows) == len(snapshot._rows) and len(identifiers) == len(
        all_identifiers
    )
    keys = {
        identifier: f"{diagnostic_id}/{'thread' if identifier in thread_rows else 'endpoint'}/"
        f"{index}"
        for index, identifier in enumerate(identifiers, 1)
    }
    reasons: dict[str, set[str]] = defaultdict(set)
    evidence: list[Evidence] = []
    evidence_complete = True
    evidence_omitted = 0
    evidence_cap_reached = False

    def add(  # noqa: PLR0913, PLR0917
        source: str | None,
        target: str | None,
        kind: str,
        origin: str,
        reason: str | None = None,
        status: str | None = None,
    ) -> None:
        nonlocal evidence_complete, evidence_omitted, evidence_cap_reached
        if len(evidence) < limits.max_evidence:
            evidence.append(Evidence(source, target, kind, origin, reason, status))
        else:
            evidence_complete = False
            evidence_omitted += 1
            evidence_cap_reached = True

    for identifier in duplicate_threads & keys.keys():
        reasons[identifier].add("duplicate_owner")
    spawn: list[tuple[str, str]] = []
    attachment: list[tuple[str, str]] = []
    edge_counts = Counter((edge.parent, edge.child) for edge in sql.edges)
    parents: dict[str, set[str]] = defaultdict(set)
    for edge in sql.edges:
        _check_cancelled(cancelled)
        parent, child = edge.parent, edge.child
        if (parent is None and child is None) or any(
            value is not None and value not in keys for value in (parent, child)
        ):
            evidence_complete = False
            evidence_omitted += 1
            continue
        source = keys[parent] if parent is not None else None
        target = keys[child] if child is not None else None
        reason = None if edge.status is not None else "invalid_status"
        if parent is not None and child is not None:
            spawn.append((parent, child))
            parents[child].add(parent)
            if edge.status is None:
                reasons[child].add("invalid_status")
            if edge_counts[(parent, child)] > 1:
                reasons[child].add("duplicate_edge")
        elif parent is not None:
            reason = "invalid_child"
            reasons[parent].update(("invalid_endpoint", reason))
        elif child is not None:
            reason = "invalid_parent"
            reasons[child].update(("invalid_endpoint", reason))
        add(source, target, "spawn", "sql", reason, edge.status)
        if sql.owners_coverage == "complete":
            for endpoint_id, label in ((parent, "missing_parent"), (child, "missing_child")):
                if endpoint_id is not None and endpoint_id not in thread_rows:
                    reasons[endpoint_id].add(label)
                    add(source, target, "dangling_endpoint", "sql", label)
    for child, values in parents.items():
        if len(values) > 1:
            reasons[child].add("multiple_parents")
    for row in sql.threads:
        _check_cancelled(cancelled)
        if row.thread_id not in keys:
            continue
        if row.invalid_attachment:
            reasons[row.thread_id].add("invalid_attachment")
        if row.attachment is not None:
            if row.attachment not in keys:
                evidence_complete = False
                evidence_omitted += 1
                continue
            attachment.append((row.attachment, row.thread_id))
            add(keys[row.attachment], keys[row.thread_id], "attachment", "sql")
            if sql.owners_coverage == "complete" and row.attachment not in thread_rows:
                reasons[row.attachment].add("missing_parent")
                add(
                    keys[row.attachment],
                    keys[row.thread_id],
                    "dangling_endpoint",
                    "sql",
                    "missing_parent",
                )
    for kind, links in (("spawn", spawn), ("attachment", attachment)):
        cyclic = _cycles(set(identifiers), links, cancelled)
        for identifier in cyclic:
            reasons[identifier].add(f"{kind}_cycle")
        children: dict[str, list[str]] = defaultdict(list)
        for parent, child in links:
            children[parent].append(child)
        blocked = set(cyclic) | {
            identifier for identifier in identifiers if "missing_parent" in reasons[identifier]
        }
        queue = deque(blocked)
        visited = set(blocked)
        while queue:
            _check_cancelled(cancelled)
            parent = queue.popleft()
            for child in children[parent]:
                if child in cyclic:
                    continue
                add(keys[child], keys[parent], "blocked_by", "derived", kind)
                reasons[child].add("blocked_by")
                if child not in visited:
                    visited.add(child)
                    queue.append(child)
    subjects: list[Subject] = []
    physical_owners = {row.filename_thread_id for row in snapshot._rows}
    for identifier in identifiers:
        kind = "thread" if identifier in thread_rows else "endpoint"
        if (
            kind == "thread"
            and identifier not in physical_owners
            and snapshot._summary.history_coverage == "complete"
        ):
            reasons[identifier].add("sql_only")
        subjects.append(
            Subject(
                keys[identifier],
                kind,
                identifier,
                None,
                None,
                None,
                "exists"
                if kind == "thread"
                else ("missing" if sql.owners_coverage == "complete" else "unknown"),
                "not_applicable",
                tuple(sorted(reasons[identifier])),
            )
        )
    for index, physical in enumerate(physical_rows):
        _check_cancelled(cancelled)
        owner = physical.filename_thread_id
        fact = thread_rows.get(owner) if owner else None
        owner_state = (
            "exists"
            if fact
            else ("missing" if owner and sql.owners_coverage == "complete" else "unknown")
        )
        parts = (
            snapshot._diagnostic_paths[index] if index < len(snapshot._diagnostic_paths) else None
        )
        pointer = (
            _pointer(home, parts, fact.pointer)
            if fact and owner not in duplicate_threads
            else "unknown"
        )
        local_reasons = set()
        if (physical.association == "matched" and owner_state == "missing") or (
            physical.association == "missing" and owner_state == "exists"
        ):
            local_reasons.add("sql_observations_differ")
        if physical.association == "unparsed":
            local_reasons.add("unparsed_name")
        if physical.association == "ambiguous":
            local_reasons.add("ambiguous_name")
        if owner_state == "missing":
            local_reasons.add("missing_owner")
        if fact is not None and fact.pointer is None:
            local_reasons.add("invalid_pointer")
        if pointer == "different":
            local_reasons.add("historical_rollout")
        if parts is None:
            local_reasons.add("name_unavailable")
        subject_id = f"{diagnostic_id}/physical/{index + 1}"
        subjects.append(
            Subject(
                subject_id,
                "physical",
                owner,
                physical.rollout_id,
                physical.object_id,
                physical.association,
                owner_state,
                pointer,
                tuple(sorted(local_reasons)),
            )
        )
        if owner in keys:
            add(subject_id, keys[owner], "filename_owner", "filesystem")
    evidence = sorted(
        set(evidence),
        key=lambda e: (
            e.source or "",
            e.target or "",
            e.kind,
            e.origin,
            e.reason or "",
            e.status or "",
        ),
    )
    budget_remaining = limits.max_bytes
    serialized_limit_reached = False
    retained_subjects = []
    serialized_subject_start = None
    for index, subject in enumerate(subjects):
        _check_cancelled(cancelled)
        cost = _variable_bytes(asdict(subject))
        if cost > budget_remaining:
            serialized_limit_reached = True
            subjects_complete = False
            serialized_subject_start = index
            break
        budget_remaining -= cost
        retained_subjects.append(subject)
    allowed = {subject.subject_id for subject in retained_subjects}
    retained_evidence = []
    serialized_evidence_start = None
    for index, item in enumerate(evidence):
        _check_cancelled(cancelled)
        if (item.source is not None and item.source not in allowed) or (
            item.target is not None and item.target not in allowed
        ):
            evidence_complete = False
            evidence_omitted += 1
            continue
        cost = _variable_bytes(asdict(item))
        if cost > budget_remaining:
            serialized_limit_reached = True
            evidence_complete = False
            evidence_omitted += len(evidence) - index
            serialized_evidence_start = index
            break
        budget_remaining -= cost
        retained_evidence.append(item)
    candidate_ids = {row.rollout_id for row in physical_rows if row.rollout_id is not None}
    rollout_counts = Counter(
        row.rollout_id for row in snapshot._rows if row.rollout_id in candidate_ids
    )
    ambiguous_rollouts = frozenset(
        row.rollout_id
        for row in physical_rows
        if row.rollout_id is not None
        and (row.association == "ambiguous" or rollout_counts[row.rollout_id] > 1)
    )
    subjects_omitted = (
        len(snapshot._rows)
        - len(physical_rows)
        + len(all_identifiers)
        - len(identifiers)
        + len(subjects)
        - len(retained_subjects)
    )
    subjects_total = len(subjects)
    evidence_total = len(evidence)
    subjects = retained_subjects
    evidence = retained_evidence
    summary = {
        "coverage": {
            "names": snapshot._summary.history_coverage,
            "full_names": (
                "complete"
                if snapshot._summary.history_coverage == "complete"
                and len(snapshot._diagnostic_paths) == len(snapshot._rows)
                and all(parts is not None for parts in snapshot._diagnostic_paths)
                else "partial"
            ),
            "owners": sql.owners_coverage,
            "pointers": sql.pointers_coverage,
            "edges": sql.edges_coverage,
            "spawn_status": sql.status_coverage,
            "attachments": (
                sql.owners_coverage
                if not any(row.invalid_attachment for row in sql.threads)
                else "partial"
            ),
            "evidence": "complete" if evidence_complete and subjects_complete else "partial",
            "subjects": "complete" if subjects_complete else "partial",
            "providers": "not_requested",
        },
        "counts": {
            "physical_names": snapshot._history_names_observed,
            "physical_rows": len(snapshot._rows),
            "physical_subjects": sum(s.kind == "physical" for s in subjects),
            "sql_owners": len(sql.threads),
            "sql_pointers": sum(row.pointer is not None for row in sql.threads),
            "sql_edges": len(sql.edges),
            "evidence": len(evidence),
            "invalid_ids": sql.invalid_ids,
            "invalid_pointers": sql.invalid_pointers,
            "invalid_endpoints": sql.invalid_endpoints,
        },
        "sql_started_at": sql.started_at,
        "sql_finished_at": sql.finished_at,
        "fs_started_at": snapshot._summary.fs_started_at,
        "fs_finished_at": snapshot._summary.fs_finished_at,
        "original_sql_started_at": snapshot._summary.sql_started_at,
        "original_sql_finished_at": snapshot._summary.sql_finished_at,
        "sql_reason": sql.reason,
        "original_association_observation": {
            "started_at": snapshot._summary.sql_started_at,
            "finished_at": snapshot._summary.sql_finished_at,
            "coverage": snapshot._summary.sql_coverage,
        },
        "truncation": {
            "range_convention": "half_open; ordinals_one_based; serialized_indices_zero_based",
            "subjects_omitted": subjects_omitted,
            "evidence_omitted_at_least": evidence_omitted,
            "serialized_variable_bytes": limits.max_bytes - budget_remaining,
            "serialized_variable_limit_reached": serialized_limit_reached,
            "subject_limit_reached": len(physical_rows) + len(identifiers)
            < len(snapshot._rows) + len(all_identifiers),
            "evidence_limit_reached": evidence_cap_reached,
            "physical_ordinals_omitted": [len(physical_rows) + 1, len(snapshot._rows) + 1],
            "logical_ordinals_omitted": [len(identifiers) + 1, len(all_identifiers) + 1],
            "serialized_subject_suffix": (
                None
                if serialized_subject_start is None
                else [serialized_subject_start, subjects_total]
            ),
            "serialized_evidence_suffix": (
                None
                if serialized_evidence_start is None
                else [serialized_evidence_start, evidence_total]
            ),
        },
    }
    return DiagnosticObservation(
        diagnostic_id,
        snapshot.storage_snapshot_id,
        summary,
        tuple(subjects),
        tuple(evidence),
        ambiguous_rollouts,
    )


def observe_diagnostics(
    home: Path,
    snapshot: StorageSnapshot,
    *,
    cancelled: Callable[[], bool] | None = None,
    limits: DiagnosticLimits = DEFAULT_DIAGNOSTIC_LIMITS,
) -> DiagnosticObservation:
    """Observe SQL separately and bind all results to the supplied storage generation."""
    requested_stop = cancelled if cancelled is not None else lambda: False
    deadline = time.monotonic() + 180

    def stop() -> bool:
        if time.monotonic() >= deadline:
            raise StorageScanError("Storage diagnostic deadline exceeded.")
        return requested_stop()

    _check_cancelled(stop)
    home = Path(os.path.abspath(os.fspath(home)))  # noqa: PTH100
    _verify_root(home, snapshot._summary.home_identity)
    base_scan = snapshot._sql_scan
    if base_scan is None:
        sql = SqlFacts(reason="sql_snapshot_unavailable")
    else:
        scan = replace(base_scan, home=home)
        sql = observe_sql(scan, cancelled=stop, limits=limits)
    _verify_root(home, snapshot._summary.home_identity)
    return _project(home, snapshot, sql, limits, stop)
