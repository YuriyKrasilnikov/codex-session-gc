# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, SLF001

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.execution import closed_work_profile as loader
from codex_session_gc.execution.closed_work_request import CONTENT_TYPE, DatabaseKind

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _document() -> dict[str, object]:
    """Synthetic decoder data, explicitly not helper/schema qualification."""
    return {
        "version": 1,
        "wire_version": 6,
        "request_content_type": CONTENT_TYPE,
        "binary_sha256": list(bytes(range(32))),
        "source_identity": "synthetic-unqualified-loader-test",
        "qualification_evidence": {
            "content_type": "application/test-synthetic-evidence",
            "version": 1,
            "data_base64": base64.b64encode(b"NOT qualification").decode(),
        },
        "databases": [
            {
                "kind": kind.value,
                "schema": [["table", "synthetic", "synthetic", "CREATE TABLE synthetic(x)"]],
                "migrations": [] if kind is DatabaseKind.BOARD else [[1, [1, 2, 3], True]],
            }
            for kind in DatabaseKind
        ],
    }


def test_decode_exact_typed_contract_and_bind_all_resource_bytes() -> None:
    document = _document()
    current_version = 6
    assert document["wire_version"] == current_version
    raw = json.dumps(document).encode()
    contract = loader._decode_contract(raw)
    assert contract.binary_sha256 == bytes(range(32))
    assert contract.source_identity == "synthetic-unqualified-loader-test"
    assert {entry.kind for entry in contract.databases} == set(DatabaseKind)
    assert contract.databases[0].schema == (
        ("table", "synthetic", "synthetic", "CREATE TABLE synthetic(x)"),
    )
    assert contract.databases[0].migrations == ((1, b"\x01\x02\x03", True),)
    assert contract.evidence.content_type == loader.CONTRACT_CONTENT_TYPE
    assert contract.evidence.data == raw
    assert contract.evidence.digest == sha256(raw).digest()
    changed = loader._decode_contract(raw + b"\n")
    assert changed.evidence.digest != contract.evidence.digest


def test_public_loader_uses_only_fixed_package_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resource(package: str) -> Path:
        assert package == "codex_session_gc.execution"
        return tmp_path

    monkeypatch.setattr(loader, "files", resource)
    with pytest.raises(loader.ContractRefused):
        loader.load_closed_work_contract()
    raw = json.dumps(_document()).encode()
    (tmp_path / "closed_work_contract.json").write_bytes(raw)
    assert loader.load_closed_work_contract().evidence.data == raw
    with pytest.raises(TypeError):
        loader.load_closed_work_contract(tmp_path)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=True),
        lambda d: d.update(version=2),
        lambda d: d.update(wire_version=2),
        lambda d: d.update(wire_version=True),
        lambda d: d.update(request_content_type="application/json"),
        lambda d: d.update(binary_sha256=[0] * 31),
        lambda d: d.update(binary_sha256=[True] * 32),
        lambda d: d.update(binary_sha256=[256] * 32),
        lambda d: d.update(source_identity=""),
        lambda d: d.update(databases=[]),
        lambda d: d.update(extra="unknown"),
        lambda d: d["qualification_evidence"].update(version=True),
        lambda d: d["qualification_evidence"].update(data_base64="!invalid"),
        lambda d: d["qualification_evidence"].update(data_base64=""),
        lambda d: d["databases"][0].update(kind="unknown.sqlite"),
        lambda d: d["databases"][0].update(schema=[]),
        lambda d: d["databases"][0].update(schema=[["table", "x", "x"]]),
        lambda d: d["databases"][0].update(schema=[["other", "x", "x", None]]),
        lambda d: d["databases"][0].update(migrations=[[True, [1], True]]),
        lambda d: d["databases"][0].update(migrations=[[1, [], True]]),
        lambda d: d["databases"][0].update(migrations=[[1, [1], 1]]),
        lambda d: d["databases"][-1].update(migrations=[[1, [1], True]]),
        lambda d: d["databases"][1].update(kind=d["databases"][0]["kind"]),
        lambda d: d["databases"][0].update(migrations=[[1, [1], True], [1, [2], True]]),
        lambda d: d["databases"][0]["schema"].append(d["databases"][0]["schema"][0]),
    ],
)
def test_refuse_unsupported_or_malformed_contract(mutate: Callable) -> None:
    document = _document()
    mutate(document)
    with pytest.raises(loader.ContractRefused):
        loader._decode_contract(json.dumps(document).encode())


@pytest.mark.parametrize("raw", [b"{}", b"[]", b"null", b"{", b"\xff", b"NaN"])
def test_refuse_invalid_document(raw: bytes) -> None:
    with pytest.raises(loader.ContractRefused):
        loader._decode_contract(raw)


def test_refuse_duplicate_json_fields_at_any_depth() -> None:
    raw = json.dumps(_document()).encode()
    duplicate = raw.replace(b'"version": 1', b'"version": 1, "version": 1', 1)
    with pytest.raises(loader.ContractRefused):
        loader._decode_contract(duplicate)
    nested = raw.replace(b'"content_type":', b'"version": 1, "content_type":', 1)
    with pytest.raises(loader.ContractRefused):
        loader._decode_contract(nested)


def test_fresh_observation_requires_exact_current_profile() -> None:
    current = loader._decode_contract(json.dumps(_document()).encode())
    assert loader.reconciliation_contract(current.evidence, current) == current
    with pytest.raises(loader.ContractRefused):
        loader.reconciliation_contract(
            replace(current.evidence, data=current.evidence.data + b"\n"), current
        )


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 7])
def test_retired_and_unknown_request_versions_are_refused(version: int) -> None:
    document = _document()
    document["wire_version"] = version
    with pytest.raises(loader.ContractRefused):
        loader._decode_contract(json.dumps(document).encode())


def test_missing_current_resource_never_selects_another_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = json.dumps(_document()).encode()
    (tmp_path / "another_contract.json").write_bytes(other)
    monkeypatch.setattr(loader, "files", lambda _package: tmp_path)
    with pytest.raises(loader.ContractRefused):
        loader.load_closed_work_contract()


def test_official_board_channel_upgrade_keeps_one_exact_schema_identity() -> None:
    # Official rust-v0.164.0 message-board/src/local/pool.rs, SHA-256
    # cdc57c450d4adc2cb9de03af51f08290bf474887ff4ee4842430aaeaff71f81d.
    # This checks SQLite DDL preservation, not helper artifact qualification.
    old_channels = (
        "CREATE TABLE channels (\n"
        " board TEXT NOT NULL, name TEXT NOT NULL, name_search TEXT NOT NULL, "
        "created_at TEXT NOT NULL, timestamp INTEGER NOT NULL, author TEXT NOT NULL,\n"
        " PRIMARY KEY(board,name)\n)"
    )
    fresh_channels = old_channels.replace(
        "author TEXT NOT NULL,\n", "author TEXT NOT NULL, description TEXT,\n"
    )
    template = (
        "CREATE TABLE board_templates (board TEXT PRIMARY KEY NOT NULL, template TEXT NOT NULL)"
    )
    schemas = []
    for upgraded in (False, True):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(old_channels if upgraded else fresh_channels)
            connection.execute(template)
            if upgraded:
                connection.execute("ALTER TABLE channels ADD COLUMN description TEXT")
            schemas.append(
                connection.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                ).fetchall()
            )
        finally:
            connection.close()
    assert schemas[0] == schemas[1]
    assert schemas[0] == [
        ("table", "board_templates", "board_templates", template),
        ("table", "channels", "channels", fresh_channels),
    ]
