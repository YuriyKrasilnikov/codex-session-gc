# Copyright (c) 2026
# ruff: noqa: EM101, TRY003, TRY301, S608, PLR0913, PLR0917

"""Prepare finite v6 native requests from direct read transactions, never execution authority.

The caller supplies a qualified helper/build contract and explicit resource
policy. Read transactions are separate non-atomic observations. Native execution
independently revalidates identities, schema, scope and effects under ownership.
Original SQLite databases are opened read-only.
No whole-home stability is required.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from codex_session_gc.domain import (
    GraphSnapshot,
    PhysicalInventory,
    PhysicalRepresentation,
    RolloutId,
    RolloutMetadata,
    SelectedPhysicalManifest,
    ThreadId,
    UtcCutoff,
)
from codex_session_gc.execution.bounded_header import (
    HeaderReadError,
    HeaderReadLimits,
    read_bounded_header,
)
from codex_session_gc.execution.parent_relation import (
    GuardianParent,
    parent_relation,
    relation_payload,
    validate_relation_source,
)
from codex_session_gc.execution.physical_manifest import (
    WireTarget,
    decode_targets,
    encode_rollout,
    history_cache_keys,
)
from codex_session_gc.inventory.observation import RolloutObservation
from codex_session_gc.planning.closed_work import ClosedWorkSelection, select_closed_work
from codex_session_gc.recovery import AttemptId, Payload, UnitId
from codex_session_gc.sqlite_read import DirectReadError, read_transaction

if TYPE_CHECKING:
    from collections.abc import Iterator


CONTENT_TYPE = "application/vnd.codex-session-gc.legacy-native-request+json"


class PreparationRefused(ValueError):  # noqa: N818
    """The captured facts cannot form an exact finite native request."""


class DatabaseKind(StrEnum):
    """The native helper's complete database namespace, not a policy registry."""

    STATE = "state_5.sqlite"
    LOGS = "logs_2.sqlite"
    GOALS = "goals_1.sqlite"
    MEMORY = "memories_1.sqlite"
    MEMORY_V2 = "memories_v2_1.sqlite"
    QUEUE = "queue_1.sqlite"
    HISTORY = "thread_history_1.sqlite"
    BOARD = "agent_message_board_1.sqlite"


type SchemaRow = tuple[str, str, str, str | None]
type MigrationRow = tuple[int, bytes, bool]
_SCHEMA_COLUMNS = 4
_MIGRATION_COLUMNS = 3


@dataclass(frozen=True, slots=True)
class DatabaseContract:
    """Exact DDL and migration identities supplied by accepted build evidence."""

    kind: DatabaseKind
    schema: tuple[SchemaRow, ...]
    migrations: tuple[MigrationRow, ...]

    def __post_init__(self) -> None:
        """Require immutable native schema and migration tuples."""
        if type(self.kind) is not DatabaseKind:
            raise TypeError("Database kinds must be typed native identifiers.")
        if type(self.schema) is not tuple or type(self.migrations) is not tuple:
            raise TypeError("Schema descriptors must be immutable tuples.")
        for row in self.schema:
            if (
                type(row) is not tuple
                or len(row) != _SCHEMA_COLUMNS
                or any(type(value) is not str for value in row[:3])
                or not (row[3] is None or type(row[3]) is str)
            ):
                raise TypeError("DDL identities require immutable native schema tuples.")
        for migration in self.migrations:
            if (
                type(migration) is not tuple
                or len(migration) != _MIGRATION_COLUMNS
                or type(migration[0]) is not int
                or type(migration[1]) is not bytes
                or type(migration[2]) is not bool
            ):
                raise TypeError("Migration identities require integer, bytes and bool tuples.")
        if self.kind is DatabaseKind.BOARD and self.migrations:
            raise ValueError("The native board has no framework migrations.")


@dataclass(frozen=True, slots=True)
class QualifiedHelperContract:
    """Caller-qualified binary/source evidence with the native storage contract.

    Construction does not qualify an artifact or certify closure. No loader,
    runtime policy registry or app-server maintenance assumptions are implied.
    """

    binary_sha256: bytes
    source_identity: str
    evidence: Payload
    databases: tuple[DatabaseContract, ...]

    def __post_init__(self) -> None:
        """Require immutable exact descriptors for the complete finite namespace."""
        if type(self.binary_sha256) is not bytes or len(self.binary_sha256) != sha256().digest_size:
            raise ValueError("A qualified helper requires an exact SHA-256 digest.")
        if (
            type(self.source_identity) is not str
            or not self.source_identity
            or type(self.evidence) is not Payload
        ):
            raise ValueError("A qualified helper requires source and qualification evidence.")
        if type(self.databases) is not tuple or len(self.databases) != len(DatabaseKind):
            raise ValueError("The helper contract must describe every native database kind.")
        if any(
            type(item) is not DatabaseContract or type(item.kind) is not DatabaseKind
            for item in self.databases
        ):
            raise TypeError("Database descriptors require typed native kinds.")
        if {item.kind for item in self.databases} != set(DatabaseKind):
            raise ValueError("The helper database namespace is ambiguous.")


@dataclass(frozen=True, slots=True)
class HeaderObservation:
    """A bounded first line and sampled physical identity, never a body proof."""

    path: Path
    metadata: dict[str, object]
    decoded_header_sha256: bytes
    identity: tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class ClosedWorkFacts:
    """Validated source facts, independent of a whole maintenance plan."""

    snapshot: GraphSnapshot
    rollouts: tuple[RolloutObservation, ...]
    cutoff: UtcCutoff
    physical_inventory: PhysicalInventory
    headers: tuple[HeaderObservation, ...] = ()
    incoming_references_complete: bool = False

    def __post_init__(self) -> None:
        """Keep the finite fact boundary typed and immutable."""
        if (
            type(self.snapshot) is not GraphSnapshot
            or type(self.cutoff) is not UtcCutoff
            or type(self.physical_inventory) is not PhysicalInventory
            or type(self.rollouts) is not tuple
            or any(type(item) is not RolloutObservation for item in self.rollouts)
            or type(self.headers) is not tuple
            or any(type(item) is not HeaderObservation for item in self.headers)
            or type(self.incoming_references_complete) is not bool
        ):
            raise TypeError("Closed-work facts require a typed snapshot and rollout observations.")


@dataclass(frozen=True, slots=True)
class CollectionLimits:
    """Explicit caller resource policy; no automatic limit expansion occurs."""

    maximum_database_bytes: int
    maximum_rollout_bytes: int
    maximum_total_bytes: int
    maximum_header_bytes: int
    maximum_request_bytes: int
    deadline_seconds: float

    def __post_init__(self) -> None:
        """Reject unbounded, nonpositive or incorrectly typed resource inputs."""
        for value in (
            self.maximum_database_bytes,
            self.maximum_rollout_bytes,
            self.maximum_total_bytes,
            self.maximum_header_bytes,
            self.maximum_request_bytes,
        ):
            if type(value) is not int or value < 1:
                raise ValueError("Byte bounds must be positive integers.")
        if (
            type(self.deadline_seconds) not in (int, float)
            or not math.isfinite(self.deadline_seconds)
            or self.deadline_seconds <= 0
        ):
            raise ValueError("The cooperative deadline must be finite and positive.")


@dataclass(slots=True)
class CollectionBudget:
    """Cooperative captured-input and SQL-key accounting for one observation phase."""

    limits: CollectionLimits
    deadline: float
    used: int = 0
    key_bytes: int = 0

    def check(self, amount: int = 0) -> None:
        """Charge actual input bytes without expanding the caller deadline or ceiling."""
        if time.monotonic() >= self.deadline:
            raise PreparationRefused("The cooperative collection deadline expired.")
        self.used += amount
        if self.used > self.limits.maximum_total_bytes:
            raise PreparationRefused("The total captured-byte bound was exceeded.")

    def key(self, value: int) -> None:
        """Bound exact native row ID serialization independently of source extents."""
        self.check()
        if type(value) is not int or not -(2**63) <= value < 2**63:
            raise PreparationRefused("A physical SQL key is not a signed native rowid.")
        self.key_bytes += len(str(value)) + 1
        if self.key_bytes > self.limits.maximum_request_bytes:
            raise PreparationRefused("Physical keys exceed the request-byte bound.")


# Fixed SQL incidence, matching the native contract. No identifier comes from a
# caller value; IDs are always bound parameters.
_TABLES: dict[DatabaseKind, dict[str, str | None]] = {
    DatabaseKind.STATE: {
        "threads": "id",
        "thread_dynamic_tools": "thread_id",
        "thread_attachments": "thread_id",
        "guardian_review_feedback": "thread_id",
        "thread_read_receipts": "thread_id",
        "thread_spawn_edges": None,
    },
    DatabaseKind.LOGS: {"logs": "thread_id"},
    DatabaseKind.GOALS: {
        "thread_goals": "thread_id",
        "thread_goal_continuation_deferrals": "thread_id",
    },
    DatabaseKind.MEMORY: {"stage1_outputs": "thread_id", "jobs": "job_key"},
    DatabaseKind.MEMORY_V2: {"stage1_outputs": "thread_id", "jobs": "job_key"},
    DatabaseKind.HISTORY: {
        "thread_items": "thread_id",
        "thread_realtime_items": "thread_id",
        "thread_turns": "thread_id",
        "thread_history_projection_state": "thread_id",
    },
    DatabaseKind.QUEUE: {"queued_items": "thread_id", "queued_thread_revisions": "thread_id"},
    DatabaseKind.BOARD: {
        "board_templates": "board",
        "subscriptions": "board",
        "subscription_opt_outs": "board",
        "posts": "board",
        "channels": "board",
        "deleted_boards": "board",
    },
}


def prepare_request(  # noqa: C901, PLR0912, PLR0915
    observation: ClosedWorkFacts,
    selection: ClosedWorkSelection,
    *,
    cutoff: UtcCutoff,
    codex_home: Path,
    sqlite_home: Path,
    journal: Path,
    attempt_id: AttemptId,
    unit_id: UnitId,
    helper: QualifiedHelperContract,
    default_provider: str,
    limits: CollectionLimits,
) -> Payload:
    """Return immutable request bytes for one selected work, without dispatch.

    Missing optional databases are observed as absent and omitted, not represented
    by empty bounds. A missing board never creates storage or implies future-write
    exclusion. A multi-root caller prepares separate sequential native units.
    """
    if type(default_provider) is not str or not default_provider:
        raise PreparationRefused("An explicit nonempty store-default provider is required.")
    _absolute(codex_home)
    _absolute(sqlite_home)
    _absolute(journal)
    if type(observation) is not ClosedWorkFacts:
        raise TypeError("Preparation requires finite closed-work facts.")
    snapshot = observation.snapshot
    if snapshot is None or cutoff != observation.cutoff or len(selection.roots) != 1:
        raise PreparationRefused("One observed work root and its exact cutoff are required.")
    refreshed = select_closed_work(
        snapshot,
        cutoff,
        selection.roots,
        selected_threads=selection.requested_threads,
        physical_inventory=observation.physical_inventory,
    )
    if refreshed != selection or selection.errors or not selection.conditional_candidates:
        raise PreparationRefused("Selection is stale, refused or empty.")
    root = next(iter(selection.roots))
    selected = tuple(str(item.value) for item in sorted(selection.conditional_candidates))
    selected_ids = frozenset(selected)
    budget = CollectionBudget(limits, time.monotonic() + limits.deadline_seconds)
    paths = {item.kind: sqlite_home / item.kind.value for item in helper.databases}
    namespace = {kind: _sample(path) for kind, path in paths.items()}
    if namespace[DatabaseKind.STATE] is None:
        raise PreparationRefused("The authoritative state database is absent.")
    targets: list[dict[str, object]] = []
    bounds: list[dict[str, object]] = []
    captured_rollouts: dict[Path, tuple[int, int, int, int, int]] = {}
    guardian_guards: set[str] = set()
    try:
        for contract in sorted(helper.databases, key=lambda item: item.kind.value):
            source = paths[contract.kind]
            if namespace[contract.kind] is None:
                continue
            with _read_transaction(source, budget) as connection:
                validate_database_schema(connection, contract)
                bounds.append(
                    _bound(
                        connection,
                        contract.kind,
                        source,
                        tuple(
                            sorted(
                                set(selected)
                                | {
                                    str(item.ownership.rollout_id.value)
                                    for item in observation.physical_inventory.rollouts
                                    if str(item.ownership.thread_id.value) in selected_ids
                                }
                            )
                        )
                        if contract.kind is DatabaseKind.HISTORY
                        else selected,
                        str(root.value),
                        budget,
                    )
                )
                if contract.kind is DatabaseKind.STATE:
                    connection.row_factory = sqlite3.Row
                    targets, captured_rollouts = prepare_targets(
                        connection, observation, selection, codex_home, budget
                    )
                    guardian_guards, guardian_identities = _guardian_guards(
                        connection, selected, codex_home, budget, observation.physical_inventory
                    )
                    captured_rollouts.update(guardian_identities)
            budget.check()
        if any(_sample(path) != namespace[kind] for kind, path in paths.items()):
            raise PreparationRefused("The native database set or physical identity changed.")
        for path, identity in captured_rollouts.items():
            descriptor = _open(path)
            try:
                if _identity(os.fstat(descriptor)) != identity:
                    raise PreparationRefused(
                        "Selected history identity changed after header capture."
                    )
            finally:
                os.close(descriptor)
        guards = {
            str(codex_home / ".tmp/rollout-maintenance.lock"),
            str(codex_home / "thread-writer-locks/.coordination.lock"),
        }
        guards.update(
            str(codex_home / f"thread-writer-locks/{item.value}.lock")
            for item in selection.ownership_scope
        )
        guards.update(guardian_guards)
        raw = json.dumps(
            {
                "version": 6,
                "journal": str(journal),
                "attempt_id": str(attempt_id.value),
                "unit_id": str(unit_id.value),
                "codex_home": str(codex_home),
                "sqlite_home": str(sqlite_home),
                "provider": default_provider,
                "cutoff": cutoff.timestamp.isoformat(),
                "ownership_scope": {
                    "root_thread_id": str(root.value),
                    "thread_ids": sorted(str(item.value) for item in selection.ownership_scope),
                },
                "deletion_scope": list(selected),
                "targets": targets,
                "databases": bounds,
                "guard_paths": sorted(guards),
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(raw) > limits.maximum_request_bytes:
            raise PreparationRefused("The request-byte bound was exceeded.")
        budget.check()
        return Payload(CONTENT_TYPE, 1, raw)
    except PreparationRefused:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as error:
        raise PreparationRefused(str(error)) from error


def validate_database_schema(connection: sqlite3.Connection, contract: DatabaseContract) -> None:
    """Compare exact DDL and migration identities on a caller-owned connection."""
    exception = "" if contract.kind is DatabaseKind.BOARD else " AND name!='_sqlx_migrations'"
    rows = tuple(
        connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            + exception
            + " ORDER BY type,name"
        )
    )
    if rows != contract.schema:
        raise PreparationRefused("The database schema is outside the qualified contract.")
    if contract.kind is not DatabaseKind.BOARD:
        migration_rows: list[MigrationRow] = []
        for version, checksum, success in connection.execute(
            "SELECT version,checksum,success FROM _sqlx_migrations ORDER BY version"
        ):
            if type(version) is not int:
                raise PreparationRefused("Migration versions must be SQLite INTEGER values.")
            if type(success) is not int or success not in (0, 1):
                raise PreparationRefused("Migration success must be SQLite INTEGER 0 or 1.")
            if type(checksum) is not bytes:
                raise PreparationRefused("Migration checksums must be SQLite BLOB values.")
            migration_rows.append((version, checksum, bool(success)))
        if tuple(migration_rows) != contract.migrations:
            raise PreparationRefused("The migration identities are unqualified.")


def _bound(
    connection: sqlite3.Connection,
    kind: DatabaseKind,
    path: Path,
    selected: tuple[str, ...],
    root: str,
    budget: CollectionBudget,
) -> dict[str, object]:
    rows = {}
    marks = ",".join("?" for _ in selected)
    params: tuple[str, ...]
    for table, column in _TABLES[kind].items():
        if kind is DatabaseKind.BOARD:
            clause, params = ("board=?", (root,)) if root in selected else ("0", ())
        elif column is None:
            clause = f"parent_thread_id IN ({marks}) OR child_thread_id IN ({marks})"
            params = selected + selected
        else:
            clause, params = f"{column} IN ({marks})", selected
            if table == "jobs":
                clause += " AND kind='memory_stage1'"
        keys = []
        for (key,) in connection.execute(
            f"SELECT rowid FROM {table} WHERE {clause} ORDER BY rowid", params
        ):
            budget.key(key)
            keys.append(key)
        rows[table] = keys
    if kind is DatabaseKind.QUEUE:
        sequence = []
        for (key,) in connection.execute(
            "SELECT rowid FROM sqlite_sequence WHERE name='queued_thread_revisions' ORDER BY rowid"
        ):
            budget.key(key)
            sequence.append(key)
        rows["sqlite_sequence"] = sequence
    return {
        "path": str(path),
        "rows": rows,
        "queue_bookkeeping": kind is DatabaseKind.QUEUE,
        "board_tombstone": root if kind is DatabaseKind.BOARD and root in selected else None,
    }


def prepare_targets(  # noqa: C901
    connection: sqlite3.Connection,
    observation: ClosedWorkFacts,
    selection: ClosedWorkSelection,
    home: Path,
    budget: CollectionBudget,
) -> tuple[list[dict[str, object]], dict[Path, tuple[int, int, int, int, int]]]:
    """Bind current SQL facts and all already-observed selected-owner physical objects."""
    manifest = SelectedPhysicalManifest(
        tuple(sorted(selection.conditional_candidates)), observation.physical_inventory
    )
    grouped: dict[ThreadId, list[RolloutMetadata]] = {
        owner: [] for owner in manifest.selected_threads
    }
    for item in manifest.physical_rollouts:
        grouped[item.ownership.thread_id].append(item)
    pointers = {
        pointer.ownership.thread_id: pointer
        for pointer in observation.physical_inventory.current_pointers
    }
    targets = []
    captured = {}
    observed_activity = {
        record.id: record.last_activity.timestamp for record in observation.snapshot.threads
    }
    observed_rollouts: dict[tuple[ThreadId | None, RolloutId | None], RolloutObservation] = {}
    for observed_rollout in observation.rollouts:
        observed_rollouts.setdefault(
            (observed_rollout.thread_id, observed_rollout.rollout_id), observed_rollout
        )
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for owner in manifest.selected_threads:
        pointer = pointers[owner]
        row = connection.execute(
            "SELECT rollout_path,source,agent_role,model_provider,updated_at_ms,archived,"
            "history_mode "
            "FROM threads WHERE id=?",
            (str(owner.value),),
        ).fetchone()
        if row is None or Path(row["rollout_path"]) != pointer.path:
            raise PreparationRefused("Current SQL pointer changed since physical observation.")
        observed = observed_activity[owner]
        if observed is None:
            raise PreparationRefused("The selected observed activity is unknown.")
        elapsed = observed - epoch
        observed_ms = (
            elapsed.days * 86400000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000
        )
        if type(row["updated_at_ms"]) is not int or row["updated_at_ms"] != observed_ms:
            raise PreparationRefused("Selected activity changed since observation.")
        items = sorted(grouped[owner], key=lambda item: item.ownership.rollout_id)
        headers = _physical_headers(items, budget)
        captured.update({header.path: header.identity for header in headers.values()})
        current_header = headers.get(pointer.ownership.rollout_id)
        if current_header is None:
            raise PreparationRefused("The selected current physical rollout is missing.")
        metadata = current_header.metadata
        source = _session_source(row["source"])
        if (
            metadata.get("source") != source
            or metadata.get("model_provider") != row["model_provider"]
            or metadata.get("agent_role") != row["agent_role"]
            or metadata.get("history_mode", "legacy") != row["history_mode"]
        ):
            raise PreparationRefused("Current physical provenance disagrees with SQL.")
        current_observation = observed_rollouts.get((owner, pointer.ownership.rollout_id))
        if current_observation is None:
            raise PreparationRefused("The selected current rollout observation is missing.")
        target = {
            "thread_id": str(owner.value),
            "current_rollout_path": str(pointer.path),
            "current_rollout_id": str(pointer.ownership.rollout_id.value),
            "source": source,
            "agent_role": row["agent_role"],
            "provider": row["model_provider"],
            "updated_at_ms": row["updated_at_ms"],
            "archived": bool(row["archived"]),
            "parent_relation": _selected_relation(
                current_observation, selection, owner, source, metadata
            ),
            "rollouts": [encode_rollout(item) for item in items],
        }
        parsed = WireTarget.model_validate(target)
        if parsed.updated_at_ms >= int(observation.cutoff.timestamp.timestamp() * 1000):
            raise PreparationRefused("Current selected SQL activity is not before the cutoff.")
        expected_root = home / ("archived_sessions" if parsed.archived else "sessions")
        if not current_header.path.is_relative_to(expected_root):
            raise PreparationRefused("Current archive namespace disagrees with SQL.")
        targets.append(parsed.model_dump(mode="json"))
    decoded = decode_targets(targets)
    selected = set(history_cache_keys(decoded))
    retained = {
        str(pointer.ownership.thread_id.value)
        for pointer in observation.physical_inventory.current_pointers
        if pointer.ownership.thread_id not in grouped
    }
    retained.update(
        str(item.ownership.rollout_id.value)
        for item in observation.physical_inventory.rollouts
        if item.ownership.thread_id not in grouped
    )
    if selected & retained:
        raise PreparationRefused("A selected history-cache key overlaps retained ownership.")
    return targets, captured


def _physical_headers(
    items: list[RolloutMetadata], budget: CollectionBudget
) -> dict[RolloutId, HeaderObservation]:
    """Recheck only exact selected physical objects, without choosing alternate paths."""
    headers = {}
    for item in items:
        physical = item.physical
        if physical is None:
            raise PreparationRefused("A selected physical identity is unknown.")
        alias = (
            physical.path.with_suffix("")
            if physical.representation is PhysicalRepresentation.ZSTD
            else physical.path.with_name(physical.path.name + ".zst")
        )
        try:
            alias.lstat()
        except FileNotFoundError:
            pass
        else:
            raise PreparationRefused("A competing physical representation appeared.")
        header = observe_header(physical.path, budget)
        if (
            header.identity
            != (
                physical.stat.dev,
                physical.stat.ino,
                physical.stat.stored_bytes,
                physical.stat.mtime_ns,
                physical.stat.ctime_ns,
            )
            or header.decoded_header_sha256 != physical.decoded_header_sha256
        ):
            raise PreparationRefused("The reviewed physical manifest changed.")
        headers[item.ownership.rollout_id] = header
    return headers


def _selected_relation(
    observation: RolloutObservation,
    selection: ClosedWorkSelection,
    identifier: ThreadId,
    source: object,
    metadata: dict[str, object],
) -> dict[str, object]:
    """Keep selected Guardian association identical across facts and fresh native input."""
    relation = parent_relation(observation)
    validate_relation_source(relation, source)
    if isinstance(relation, GuardianParent) and (
        metadata.get("parent_thread_id") != str(relation.associated_parent_thread_id.value)
        or not any(
            association.guardian_thread_id == identifier
            and association.associated_parent_thread_id == relation.associated_parent_thread_id
            for association in selection.guardian_associations
        )
    ):
        raise PreparationRefused("Selected Guardian association changed since review.")
    return relation_payload(relation)


def _guardian_guards(
    connection: sqlite3.Connection,
    selected: tuple[str, ...],
    home: Path,
    budget: CollectionBudget,
    inventory: PhysicalInventory,
) -> tuple[set[str], dict[Path, tuple[int, int, int, int, int]]]:
    """Pin retained Guardian physical writer paths without extending deletion scope."""
    guards: set[str] = set()
    identities: dict[Path, tuple[int, int, int, int, int]] = {}
    guardian_source = {"subagent": {"other": "guardian"}}
    selected_ids = frozenset(selected)
    pointers = {pointer.ownership.thread_id: pointer for pointer in inventory.current_pointers}
    physicals = {item.ownership: item for item in inventory.rollouts}
    candidates = connection.execute(
        "SELECT id,rollout_path,source,model_provider,agent_role,archived,history_mode "
        "FROM threads WHERE CASE WHEN json_valid(source) "
        "THEN json_extract(source,'$.subagent.other') END='guardian'"
    )
    for row in candidates:
        budget.check()
        identifier = ThreadId.parse(row["id"])
        if type(row["archived"]) is not int or row["archived"] not in (0, 1):
            raise PreparationRefused("Guardian archive metadata is unavailable.")
        raw_path = Path(row["rollout_path"])
        pointer = pointers.get(identifier)
        current = physicals.get(pointer.ownership) if pointer is not None else None
        if pointer is None or current is None or current.physical is None:
            raise PreparationRefused("Retained Guardian current physical identity is unresolved.")
        path = current.physical.path
        if raw_path != pointer.path or not path.is_relative_to(
            home / ("archived_sessions" if row["archived"] else "sessions")
        ):
            raise PreparationRefused("Retained Guardian current pointer changed.")
        header = observe_header(
            path, budget, enforce_extent_limit=str(identifier.value) in selected_ids
        )
        metadata = header.metadata
        if metadata.get("parent_thread_id") not in selected_ids:
            continue
        if (
            row["history_mode"] not in {"legacy", "paginated"}
            or metadata.get("history_mode", "legacy") != row["history_mode"]
            or header.decoded_header_sha256 != current.physical.decoded_header_sha256
            or metadata.get("id") != str(identifier.value)
            or _session_source(row["source"]) != guardian_source
            or metadata.get("source") != guardian_source
            or metadata.get("model_provider") != row["model_provider"]
            or not isinstance(row["model_provider"], str)
            or not row["model_provider"]
            or metadata.get("agent_role") != row["agent_role"]
            or not (row["agent_role"] is None or isinstance(row["agent_role"], str))
            or type(row["archived"]) is not int
            or row["archived"] not in (0, 1)
            or connection.execute(
                "SELECT 1 FROM thread_spawn_edges WHERE child_thread_id=? LIMIT 1",
                (str(identifier.value),),
            ).fetchone()
            is not None
        ):
            raise PreparationRefused(
                "Associated guardian metadata is outside its retained profile."
            )
        guards.add(str(home / f"thread-writer-locks/{identifier.value}.lock"))
        identities[path] = header.identity
    return guards, identities


def _session_source(raw: object) -> str | dict[str, object]:
    """Decode native enum_to_string SQL: scalar tags or structured JSON."""
    scalars = {"cli", "vscode", "exec", "mcp", "unknown"}
    if type(raw) is not str:
        raise PreparationRefused("The native SQL source is not text.")
    if raw in scalars:
        return raw
    value = json.loads(raw)
    if isinstance(value, dict) or (isinstance(value, str) and value in scalars):
        return value
    raise PreparationRefused("The SQL source is not an admitted native source encoding.")


def _validate_target_path(path: Path, home: Path, identifier: ThreadId, *, archived: bool) -> None:
    directory = home / ("archived_sessions" if archived else "sessions")
    if not path.is_relative_to(directory) or path.suffix != ".jsonl":
        raise PreparationRefused("The native target requires its archive-specific plain-file path.")
    match = re.fullmatch(
        r"rollout-([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2})-(.+)\.jsonl",
        path.name,
    )
    if match is None:
        raise PreparationRefused("The native target filename is not a canonical rollout name.")
    datetime.strptime(match[1], "%Y-%m-%dT%H-%M-%S").replace(tzinfo=UTC)
    original, separator, replacement = match[2].partition("_")
    ThreadId.parse(original)
    rollout = ThreadId.parse(replacement if separator else original)
    if rollout != identifier:
        raise PreparationRefused("Filename identity disagrees with the validated target owner.")
    if _sample(path.with_suffix(".jsonl.zst")) is not None:
        raise PreparationRefused("Compressed sibling objects exceed the plain-file effect bound.")


def _absolute(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise PreparationRefused("All storage and receipt paths must be explicit absolute paths.")


def _open(path: Path) -> int:
    _absolute(path)
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise PreparationRefused("Only regular native input files are supported.")
        return descriptor
    finally:
        os.close(directory)


def _sample(path: Path) -> tuple[int, int] | None:
    try:
        descriptor = _open(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PreparationRefused(str(error)) from error
    try:
        value = os.fstat(descriptor)
        return value.st_dev, value.st_ino
    finally:
        os.close(descriptor)


@contextmanager
def _read_transaction(source: Path, budget: CollectionBudget) -> Iterator[sqlite3.Connection]:
    """Read live WAL state without copies or immutable snapshots."""
    try:
        with read_transaction(
            source, maximum_bytes=budget.limits.maximum_database_bytes, deadline=budget.deadline
        ) as connection:
            yield connection
    except (DirectReadError, OSError) as error:
        raise PreparationRefused(str(error)) from error
    budget.check()


def is_empty_regular(path: Path) -> bool:
    """Observe definitely empty regular input without admitting a filename or ownership."""
    descriptor = _open(path)
    try:
        before = os.fstat(descriptor)
        if before.st_size != 0:
            return False
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after) or _sample(path) != (after.st_dev, after.st_ino):
            raise PreparationRefused("An empty namespace object changed during observation.")
        return True
    finally:
        os.close(descriptor)


def observe_header(
    path: Path,
    budget: CollectionBudget,
    *,
    require_stable_extent: bool = True,
    enforce_extent_limit: bool = True,
) -> HeaderObservation:
    """Capture only the bounded first line; body length does not affect read work."""
    descriptor = _open(path)
    try:
        before = os.fstat(descriptor)
        if enforce_extent_limit and before.st_size > budget.limits.maximum_rollout_bytes:
            raise PreparationRefused("The selected rollout exceeds its size bound.")
        representation = (
            PhysicalRepresentation.ZSTD
            if path.name.endswith(".jsonl.zst")
            else PhysicalRepresentation.PLAIN
        )
        try:
            captured = read_bounded_header(
                descriptor,
                representation,
                limits=HeaderReadLimits(
                    budget.limits.maximum_header_bytes,
                    budget.limits.maximum_total_bytes - budget.used,
                    require_stable_extent,
                ),
                charge=budget.check,
            )
        except HeaderReadError as error:
            raise PreparationRefused(str(error)) from error
        after = os.fstat(descriptor)
        if (require_stable_extent and _identity(before) != _identity(after)) or _sample(path) != (
            after.st_dev,
            after.st_ino,
        ):
            raise PreparationRefused("The selected physical history changed during header capture.")
        envelope = json.loads(captured.line)
        if (
            not isinstance(envelope, dict)
            or envelope.get("type") != "session_meta"
            or not isinstance(envelope.get("payload"), dict)
        ):
            raise PreparationRefused("A complete native metadata line is unavailable.")
        keys = (
            "id",
            "source",
            "parent_thread_id",
            "model_provider",
            "agent_role",
            "history_mode",
            "history_base",
            "forked_from_id",
            "forked_from_ordinal_exclusive",
        )
        metadata = {key: envelope["payload"][key] for key in keys if key in envelope["payload"]}
        return HeaderObservation(path, metadata, sha256(captured.line).digest(), _identity(after))
    finally:
        os.close(descriptor)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def review_targets(
    observation: ClosedWorkFacts,
    selection: ClosedWorkSelection,
    home: Path,
    limits: CollectionLimits,
) -> tuple[WireTarget, ...]:
    """Capture exact reviewed targets without journal creation or helper dispatch."""
    if not selection.conditional_candidates:
        return ()
    budget = CollectionBudget(limits, time.monotonic() + limits.deadline_seconds)
    with _read_transaction(home / DatabaseKind.STATE, budget) as connection:
        connection.row_factory = sqlite3.Row
        targets, _ = prepare_targets(connection, observation, selection, home, budget)
    return decode_targets(targets)
