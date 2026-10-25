# Copyright (c) 2026

"""Diagnostic observations on disposable storage, never artifact qualification."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from shlex import quote
from typing import TYPE_CHECKING

import pytest

from codex_session_gc import diagnostics
from codex_session_gc.cli.main import main
from codex_session_gc.execution.closed_work_profile import (
    ContractRefused,
    load_closed_work_contract,
)
from codex_session_gc.execution.closed_work_request import (
    DatabaseKind,
    validate_database_schema,
)
from codex_session_gc.sqlite_read import read_transaction

if TYPE_CHECKING:
    from pathlib import Path


def _source(home: Path) -> dict[Path, tuple[bytes, int, int, int]]:
    """Snapshot only disposable test files for the application-effects oracle."""
    result = {}
    for path in home.rglob("*"):
        if path.is_file():
            metadata = path.stat()
            result[path] = (
                path.read_bytes(),
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
    return result


def _equal(actual: object, expected: object) -> None:
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, observed {actual!r}")


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Use real schema fixtures and a synthetic digest for diagnostics-only tests."""
    home = tmp_path / "home"
    home.mkdir()
    for database in load_closed_work_contract().databases:
        with sqlite3.connect(home / database.kind.value) as connection:
            for row in (
                *[row for row in database.schema if row[0] == "table"],
                *[row for row in database.schema if row[0] != "table"],
            ):
                if row[3] is not None:
                    connection.execute(row[3])
            if database.kind is not DatabaseKind.BOARD:
                connection.execute("CREATE TABLE _sqlx_migrations(version,checksum,success)")
                connection.executemany(
                    "INSERT INTO _sqlx_migrations VALUES(?,?,?)", database.migrations
                )
    (home / "sessions").mkdir()
    (home / "sessions/history.jsonl").write_bytes(b"synthetic history, never parsed")
    helper = tmp_path / "helper"
    helper.write_text(f"#!/bin/sh\ntouch {quote(str(tmp_path / 'should-never-execute'))}\n")
    helper.chmod(0o755)
    contract = replace(
        load_closed_work_contract(), binary_sha256=sha256(helper.read_bytes()).digest()
    )
    monkeypatch.setattr(diagnostics, "load_closed_work_contract", lambda: contract)
    return home, helper


def _database(report: diagnostics.DoctorReport, kind: DatabaseKind) -> diagnostics.Observation:
    return next(item for item in report.databases if item.name == kind.value)


def test_matching_observations_do_not_execute_or_change_storage(
    environment: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    """Expose profile/helper identity while preserving source files and namespace."""
    home, helper = environment
    before = _source(home)
    report = diagnostics.inspect_compatibility(home, helper)
    _equal(report.exit_code, 0)
    _equal(len(report.databases), len(DatabaseKind))
    _equal({item.status for item in report.databases}, {diagnostics.Status.MATCHED})
    _equal(report.helper.sha256, sha256(helper.read_bytes()).hexdigest())
    _equal(report.profile.sha256, sha256(load_closed_work_contract().evidence.data).hexdigest())
    _equal(_source(home), before)
    _equal(sorted(path.name for path in tmp_path.iterdir()), ["helper", "home"])
    _equal("not a global snapshot or permission to delete" in report.boundary, expected=True)


@pytest.mark.parametrize("kind", [kind for kind in DatabaseKind if kind is not DatabaseKind.STATE])
def test_absent_optional_database_is_observed_without_creation(
    environment: tuple[Path, Path],
    kind: DatabaseKind,
) -> None:
    """Missing companion storage stays absent instead of becoming an empty database."""
    home, helper = environment
    path = home / kind.value
    path.unlink(missing_ok=True)
    report = diagnostics.inspect_compatibility(home, helper)
    _equal(_database(report, kind).status, diagnostics.Status.ABSENT_OPTIONAL)
    _equal(report.exit_code, 0)
    _equal(path.exists(), expected=False)


def test_missing_authoritative_state_is_not_optional(environment: tuple[Path, Path]) -> None:
    """Report the missing authoritative database as known incompatibility."""
    home, helper = environment
    (home / DatabaseKind.STATE.value).unlink()
    report = diagnostics.inspect_compatibility(home, helper)
    _equal(_database(report, DatabaseKind.STATE).status, diagnostics.Status.MISSING_REQUIRED)
    _equal(report.exit_code, 1)


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TRIGGER unexpected AFTER DELETE ON threads BEGIN SELECT 1; END",
        "UPDATE _sqlx_migrations SET checksum=X'00' WHERE version=61",
        "UPDATE _sqlx_migrations SET success=0 WHERE version=61",
    ],
)
def test_exact_schema_and_migration_changes_are_mismatches(
    environment: tuple[Path, Path],
    sql: str,
) -> None:
    """Use the execution comparator for triggers, checksums and migration success."""
    home, helper = environment
    with sqlite3.connect(home / DatabaseKind.STATE.value) as connection:
        connection.execute(sql)
    before = _source(home)
    report = diagnostics.inspect_compatibility(home, helper)
    _equal(_database(report, DatabaseKind.STATE).status, diagnostics.Status.MISMATCH)
    _equal(report.exit_code, 1)
    _equal(_source(home), before)


def test_integer_migration_checksum_is_a_structured_mismatch(
    environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject integer checksum storage without converting it into an allocation length."""
    home, helper = environment
    state_contract = next(
        item for item in load_closed_work_contract().databases if item.kind is DatabaseKind.STATE
    )
    with read_transaction(home / DatabaseKind.STATE.value) as connection:
        validate_database_schema(connection, state_contract)
    with closing(sqlite3.connect(home / DatabaseKind.STATE.value)) as connection:
        connection.execute(
            "UPDATE _sqlx_migrations SET checksum=? WHERE version=61",
            (2**63 - 1,),
        )
        connection.commit()
    before = _source(home)
    args = ["doctor", "--home", str(home), "--helper", str(helper), "--json"]
    _equal(main(args), 1)
    report = json.loads(capsys.readouterr().out)
    state = next(item for item in report["databases"] if item["name"] == DatabaseKind.STATE.value)
    _equal(state["status"], "mismatch")
    _equal("SQLite BLOB" in state["detail"], expected=True)
    _equal(_source(home), before)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("success", 2),
        ("success", "1"),
        ("success", 1.0),
        ("success", None),
        ("version", "61"),
        ("version", 61.0),
        ("version", None),
    ],
)
def test_migration_scalar_storage_types_are_structured_mismatches(
    environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: object,
) -> None:
    """Do not coerce SQL scalar values into native integer identities."""
    home, helper = environment
    with closing(sqlite3.connect(home / DatabaseKind.STATE.value)) as connection:
        statement = (
            "UPDATE _sqlx_migrations SET success=? WHERE version=61"
            if field == "success"
            else "UPDATE _sqlx_migrations SET version=? WHERE version=61"
        )
        connection.execute(statement, (value,))
        connection.commit()
    before = _source(home)
    _equal(main(["doctor", "--home", str(home), "--helper", str(helper), "--json"]), 1)
    report = json.loads(capsys.readouterr().out)
    state = next(item for item in report["databases"] if item["name"] == DatabaseKind.STATE.value)
    _equal(state["status"], "mismatch")
    _equal("SQLite INTEGER" in state["detail"], expected=True)
    _equal(_source(home), before)


@pytest.mark.parametrize("success", [0, 1])
def test_canonical_integer_success_projects_to_exact_contract_boolean(
    environment: tuple[Path, Path],
    success: int,
) -> None:
    """Preserve both canonical SQL success values when their exact contract agrees."""
    home, _ = environment
    state = next(
        item for item in load_closed_work_contract().databases if item.kind is DatabaseKind.STATE
    )
    with closing(sqlite3.connect(home / DatabaseKind.STATE.value)) as connection:
        connection.execute("UPDATE _sqlx_migrations SET success=?", (success,))
        connection.commit()
    expected = replace(
        state,
        migrations=tuple(
            (version, checksum, bool(success)) for version, checksum, _ in state.migrations
        ),
    )
    with read_transaction(home / DatabaseKind.STATE.value) as connection:
        validate_database_schema(connection, expected)


@pytest.mark.parametrize("problem", ["wrong-bytes", "not-executable", "missing", "symlink", "fifo"])
def test_helper_observations_do_not_launch_invalid_inputs(
    environment: tuple[Path, Path],
    problem: str,
) -> None:
    """Distinguish known artifact mismatches from unavailable helper observations."""
    home, helper = environment
    if problem == "wrong-bytes":
        helper.write_text("different")
    elif problem == "not-executable":
        helper.chmod(0o644)
    elif problem == "missing":
        helper.unlink()
    elif problem == "symlink":
        target = helper.with_name("target")
        helper.rename(target)
        helper.symlink_to(target)
    else:
        helper.unlink()
        os.mkfifo(helper)
    report = diagnostics.inspect_compatibility(home, helper)
    status = (
        diagnostics.Status.MISMATCH
        if problem in {"wrong-bytes", "not-executable", "fifo"}
        else diagnostics.Status.UNKNOWN
    )
    _equal(report.helper.status, status)
    _equal(report.exit_code, 1 if status is diagnostics.Status.MISMATCH else 3)


def test_unavailable_database_and_resource_bounds_remain_unknown(
    environment: tuple[Path, Path],
) -> None:
    """An unreadable observation is not evidence of an unsupported schema."""
    home, helper = environment
    path = home / DatabaseKind.STATE.value
    path.write_bytes(b"not sqlite")
    report = diagnostics.inspect_compatibility(home, helper)
    _equal(_database(report, DatabaseKind.STATE).status, diagnostics.Status.UNKNOWN)
    _equal(report.exit_code, 3)
    report = diagnostics.inspect_compatibility(home, helper, maximum_read_bytes=1)
    _equal(report.helper.status, diagnostics.Status.UNKNOWN)
    _equal(report.exit_code, 3)


def test_missing_helper_and_invalid_profile_are_explicit_unknowns(
    environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither omitted executable nor malformed installed evidence gives a match."""
    home, helper = environment
    _equal(diagnostics.inspect_compatibility(home, None).exit_code, 3)

    def invalid_profile() -> None:
        message = "Invalid installed profile"
        raise ContractRefused(message)

    monkeypatch.setattr(diagnostics, "load_closed_work_contract", invalid_profile)
    report = diagnostics.inspect_compatibility(home, helper)
    _equal(report.profile.status, diagnostics.Status.UNKNOWN)
    _equal(report.databases, ())


def test_cli_json_and_text_report_observations(
    environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both presentations explain the same read-only compatibility observations."""
    home, helper = environment
    args = ["doctor", "--home", str(home), "--helper", str(helper)]
    _equal(main([*args, "--json"]), 0)
    report = json.loads(capsys.readouterr().out)
    _equal(report["helper"]["status"], "matched")
    _equal(
        report["profile"]["sha256"], sha256(load_closed_work_contract().evidence.data).hexdigest()
    )
    _equal(main(args), 0)
    output = capsys.readouterr().out
    _equal("Expected SHA-256:" in output, expected=True)
    _equal("not a global snapshot or permission to delete" in output, expected=True)


@pytest.mark.parametrize("flags", [["--max-read-bytes", "0"], ["--timeout-seconds", "nan"]])
def test_invalid_cli_bounds_fail_before_observation(
    environment: tuple[Path, Path],
    flags: list[str],
) -> None:
    """Reject invalid resource inputs instead of silently choosing alternate bounds."""
    home, helper = environment
    with pytest.raises(SystemExit) as failure:
        main(["doctor", "--home", str(home), "--helper", str(helper), *flags])
    _equal(failure.value.code, 2)


@pytest.mark.parametrize("live_writer", [False, True])
def test_wal_reads_preserve_logical_data_with_normal_sqlite_coordination(
    environment: tuple[Path, Path],
    *,
    live_writer: bool,
) -> None:
    """Observe live WAL data without claiming that SQLite coordination is write-free."""
    home, helper = environment
    state = home / DatabaseKind.STATE.value
    with closing(sqlite3.connect(state)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE _sqlx_migrations SET success=0 WHERE version=61")
        writer.commit()
        before = writer.execute("SELECT * FROM _sqlx_migrations ORDER BY version").fetchall()
        if not live_writer:
            writer.close()
        report = diagnostics.inspect_compatibility(home, helper)
        _equal(_database(report, DatabaseKind.STATE).status, diagnostics.Status.MISMATCH)
        with closing(sqlite3.connect(state)) as reader:
            _equal(
                reader.execute("SELECT * FROM _sqlx_migrations ORDER BY version").fetchall(), before
            )
        _equal((home / "sessions/history.jsonl").read_bytes(), b"synthetic history, never parsed")
        _equal((helper.parent / "should-never-execute").exists(), expected=False)
        _equal(any(path.suffix == ".lock" for path in home.rglob("*")), expected=False)


def test_expired_observation_deadline_cannot_report_matches(
    environment: tuple[Path, Path],
) -> None:
    """Even small schema operations respect the overall observation deadline."""
    home, helper = environment
    report = diagnostics.inspect_compatibility(home, helper, timeout_seconds=1e-12)
    _equal(report.helper.status, diagnostics.Status.UNKNOWN)
    _equal({item.status for item in report.databases}, {diagnostics.Status.UNKNOWN})
    _equal(report.exit_code, 3)


def test_replacing_same_byte_helper_during_observation_is_unknown(
    environment: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching digest does not hide replacement of the observed executable path."""
    home, helper = environment
    original_stat = os.fstat
    replaced = False

    def stat_and_replace(descriptor: int) -> os.stat_result:
        nonlocal replaced
        metadata = original_stat(descriptor)
        if not replaced:
            replaced = True
            content = helper.read_bytes()
            helper.rename(helper.with_name("previous-helper"))
            helper.write_bytes(content)
            helper.chmod(0o755)
        return metadata

    monkeypatch.setattr(os, "fstat", stat_and_replace)
    report = diagnostics.inspect_compatibility(home, helper)
    _equal(report.helper.status, diagnostics.Status.UNKNOWN)
    _equal(report.exit_code, 3)


def test_symlink_database_is_unavailable_without_following_it(
    environment: tuple[Path, Path],
) -> None:
    """Unsafe observation paths do not become schema compatibility results."""
    home, helper = environment
    path = home / DatabaseKind.STATE.value
    target = home / "other-state.sqlite"
    path.rename(target)
    path.symlink_to(target)
    before = _source(home)
    report = diagnostics.inspect_compatibility(home, helper)
    _equal(_database(report, DatabaseKind.STATE).status, diagnostics.Status.UNKNOWN)
    _equal(_source(home), before)
