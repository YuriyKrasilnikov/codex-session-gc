# Copyright (c) 2026
# ruff: noqa: D103, S608
"""Wholly synthetic data in exact qualified schemas; not native requalification."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
from typing import Any

from codex_session_gc.execution.closed_work_profile import load_closed_work_contract

ROOT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
STAMP = "2026-06-01T00:00:00Z"
MIGRATIONS = """CREATE TABLE _sqlx_migrations (
version BIGINT PRIMARY KEY, description TEXT NOT NULL,
installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
success BOOLEAN NOT NULL, checksum BLOB NOT NULL, execution_time BIGINT NOT NULL)"""


def make_home(home: Path) -> dict[str, Path]:
    home.mkdir()
    (home / "sessions").mkdir()
    (home / "thread-writer-locks").mkdir()
    contract = load_closed_work_contract()
    for database in contract.databases:
        with sqlite3.connect(home / database.kind.value) as connection:
            for kind, name, _, sql in database.schema:
                if kind == "table" and name != "sqlite_sequence" and sql:
                    connection.execute(sql)
            for kind, _, _, sql in database.schema:
                if kind != "table" and sql:
                    connection.execute(sql)
            if database.migrations:
                connection.execute(MIGRATIONS)
                connection.executemany(
                    "INSERT INTO _sqlx_migrations"
                    "(version,description,success,checksum,execution_time) "
                    "VALUES(?,'synthetic fixture',?,?,0)",
                    [
                        (version, success, checksum)
                        for version, checksum, success in database.migrations
                    ],
                )
    paths = {}
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        for identifier in (ROOT, CHILD, OTHER):
            source: str | dict[str, Any] = "cli"
            if identifier == CHILD:
                source = {"subagent": {"thread_spawn": {"parent_thread_id": ROOT, "depth": 1}}}
            path = home / "sessions" / f"rollout-2026-06-01T00-00-00-{identifier}.jsonl"
            meta = {
                "id": identifier,
                "session_id": identifier,
                "timestamp": STAMP,
                "cwd": str(home),
                "source": source,
                "model_provider": "openai",
                "history_mode": "legacy",
                "originator": "synthetic-mcp-fixture",
                "cli_version": "0.162.1",
            }
            if identifier == CHILD:
                meta["parent_thread_id"] = ROOT
            path.write_text(
                json.dumps(
                    {"timestamp": STAMP, "ordinal": 0, "type": "session_meta", "payload": meta}
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": STAMP,
                        "ordinal": 1,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "synthetic history"}],
                        },
                    }
                )
                + "\n"
            )
            paths[identifier] = path
            connection.execute(
                "INSERT INTO threads(id,rollout_path,created_at,updated_at,source,model_provider,"
                "cwd,title,sandbox_policy,approval_mode,created_at_ms,updated_at_ms,history_mode) "
                "VALUES(?,?,1780272000,1780272000,?,'openai',?,'synthetic','{}','never',"
                "1780272000000,1780272000000,'legacy')",
                (
                    identifier,
                    str(path),
                    source if isinstance(source, str) else json.dumps(source),
                    str(home),
                ),
            )
        connection.execute("INSERT INTO thread_spawn_edges VALUES(?,?,'closed')", (ROOT, CHILD))
    return paths


def logical_rows(home: Path) -> dict[str, dict[str, list[tuple[Any, ...]]]]:
    result = {}
    for path in sorted(home.glob("*.sqlite")):
        with sqlite3.connect(path) as connection:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
                )
            ]
            result[path.name] = {
                name: sorted(connection.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
                for name in tables
            }
    return result


def retained_provider(path: Path, target: Path) -> None:
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header["payload"].update(
        history_mode="paginated",
        history_base={
            "thread_id": CHILD,
            "end_ordinal_exclusive": 2,
            "end_byte_offset": target.stat().st_size,
        },
    )
    path.write_text(json.dumps(header) + "\n" + "\n".join(lines[1:]) + "\n")


def make_guardian_home(home: Path) -> dict[str, Path]:
    """Give the selected Guardian a historical parent and opaque owner-scoped rows."""
    paths = make_home(home)
    source = {"subagent": {"other": "guardian"}}
    lines = paths[CHILD].read_text().splitlines()
    header = json.loads(lines[0])
    header["payload"].update(source=source, parent_thread_id=ROOT)
    paths[CHILD].write_text(json.dumps(header) + "\n" + "\n".join(lines[1:]) + "\n")
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute("UPDATE threads SET source=? WHERE id=?", (json.dumps(source), CHILD))
        connection.execute("DELETE FROM thread_spawn_edges WHERE child_thread_id=?", (CHILD,))
        for identifier in (ROOT, CHILD, OTHER):
            connection.execute(
                "INSERT INTO guardian_review_feedback VALUES(?,?,?)",
                ("feedback:" + identifier, identifier, b"\x00\xffopaque:" + identifier.encode()),
            )
            connection.execute(
                "INSERT INTO thread_read_receipts VALUES(?,?,?,?)",
                (identifier, "unread:" + identifier, "revision:" + identifier, None),
            )
    return paths
