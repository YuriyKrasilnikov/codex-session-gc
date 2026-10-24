# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Decode the one bundled, evidence-bearing helper storage contract.

Loading never qualifies a new artifact or reads an executable. The bundled
resource must be produced from accepted build and schema evidence; synthetic
loader tests do not supply that qualification.
"""

from __future__ import annotations

import base64
import json
from importlib.resources import files

from codex_session_gc.execution.closed_work_request import (
    CONTENT_TYPE,
    DatabaseContract,
    DatabaseKind,
    QualifiedHelperContract,
)
from codex_session_gc.recovery import Payload

WIRE_VERSION = 6
CONTRACT_CONTENT_TYPE = "application/vnd.codex-session-gc.closed-work-contract+json"


class ContractRefused(ValueError):  # noqa: N818
    """The static resource is absent or does not describe the supported contract."""


def load_closed_work_contract() -> QualifiedHelperContract:
    """Load only the installed package resource, with no manifest-path override."""
    try:
        raw = files("codex_session_gc.execution").joinpath("closed_work_contract.json").read_bytes()
    except OSError as error:
        raise ContractRefused("The bundled closed-work contract is unavailable.") from error
    return _decode_contract(raw)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContractRefused("Contract JSON contains a duplicate field.")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise ContractRefused("Non-JSON constants are not supported.")


def _fields(value: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractRefused("Contract fields do not match the supported format.")
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ContractRefused("Contract text identities must be nonempty strings.")
    return value


def _bytes(value: object, *, digest: bool = False) -> bytes:
    if (
        type(value) is not list
        or not value
        or any(type(item) is not int or not 0 <= item < 2**8 for item in value)
        or (digest and len(value) != 32)  # noqa: PLR2004
    ):
        raise ContractRefused("Contract byte identities are invalid.")
    return bytes(value)


def _database(value: object) -> DatabaseContract:
    entry = _fields(value, {"kind", "schema", "migrations"})
    kind = DatabaseKind(_text(entry["kind"]))
    schema = entry["schema"]
    migrations = entry["migrations"]
    if type(schema) is not list or not schema or type(migrations) is not list:
        raise ContractRefused("Database schema and migrations must be finite row arrays.")
    schema_rows = []
    schema_names = set()
    for row in schema:
        if (
            type(row) is not list
            or len(row) != 4  # noqa: PLR2004
            or row[0] not in ("table", "index", "view", "trigger")
            or any(type(item) is not str or not item for item in row[:3])
            or not (row[3] is None or (type(row[3]) is str and row[3]))
        ):
            raise ContractRefused("Invalid SQLite schema identity row.")
        name = (row[0], row[1])
        if name in schema_names:
            raise ContractRefused("Duplicate schema identity.")
        schema_names.add(name)
        schema_rows.append((row[0], row[1], row[2], row[3]))
    migration_rows = []
    versions = set()
    for row in migrations:
        if (
            type(row) is not list
            or len(row) != 3  # noqa: PLR2004
            or type(row[0]) is not int
            or not 0 < row[0] < 2**63
            or type(row[2]) is not bool
        ):
            raise ContractRefused("Invalid migration identity row.")
        if row[0] in versions:
            raise ContractRefused("Duplicate migration version.")
        versions.add(row[0])
        migration_rows.append((row[0], _bytes(row[1]), row[2]))
    return DatabaseContract(kind, tuple(schema_rows), tuple(migration_rows))


def _decode_contract(raw: bytes) -> QualifiedHelperContract:
    """Decode exact static bytes; returned evidence binds the entire document."""
    try:
        if type(raw) is not bytes:
            raise ContractRefused("Contract input must be immutable bytes.")
        value = _fields(
            json.loads(raw, object_pairs_hook=_object, parse_constant=_constant),
            {
                "version",
                "wire_version",
                "request_content_type",
                "binary_sha256",
                "source_identity",
                "qualification_evidence",
                "databases",
            },
        )
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or type(value["wire_version"]) is not int
            or value["wire_version"] != WIRE_VERSION
            or value["request_content_type"] != CONTENT_TYPE
        ):
            raise ContractRefused("Unsupported static contract or request format.")
        evidence = _fields(
            value["qualification_evidence"], {"content_type", "version", "data_base64"}
        )
        if type(evidence["version"]) is not int or evidence["version"] < 1:
            raise ContractRefused("Invalid qualification evidence version.")
        decoded = base64.b64decode(_text(evidence["data_base64"]), validate=True)
        if not decoded:
            raise ContractRefused("Qualification evidence must not be empty.")
        Payload(_text(evidence["content_type"]), evidence["version"], decoded)
        databases = value["databases"]
        if type(databases) is not list or len(databases) != len(DatabaseKind):
            raise ContractRefused("The complete eight-database namespace is required.")
        return QualifiedHelperContract(
            _bytes(value["binary_sha256"], digest=True),
            _text(value["source_identity"]),
            Payload(CONTRACT_CONTENT_TYPE, 1, raw),
            tuple(_database(entry) for entry in databases),
        )
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        if isinstance(error, ContractRefused):
            raise
        raise ContractRefused("The bundled closed-work contract is malformed.") from error


def reconciliation_contract(
    profile: Payload, current: QualifiedHelperContract
) -> QualifiedHelperContract:
    """Require the exact current contract before any fresh storage observation."""
    if profile == current.evidence:
        return current
    raise ContractRefused("Fresh observation requires the current qualified receipt profile.")
