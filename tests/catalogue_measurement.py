# Copyright (c) 2026
# ruff: noqa: S101, PLR2004, S603, S608, PLC0415

"""Bounded synthetic catalogue measurement; never an execution qualification.

The constructor oracle precedes catalogue capture. Fixtures remain task-owned
until explicit cleanup after matched comparisons. The smoke path always cleans
up in finally. Initial catalogue observations are cache-uncontrolled, never cold.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import hashlib
import io
import itertools
import json
import os
import resource
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from compression.zstd import compress
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from codex_session_gc.inventory.catalogue import SessionCatalogue
    from codex_session_gc.inventory.query import CatalogueGroup

FORMAT = "catalogue-workload-v1"
MAX_ALLOCATED = 512 * 1024**2
MAX_FILES = 100_000
PROJECT = Path(__file__).resolve().parents[1]


def _uuid(number: int) -> str:
    return str(UUID(int=number))


def _physical_path(owner: str, rollout: str, index: int, *, archive: bool) -> Path:
    day = index % 28 + 1
    ids = owner if owner == rollout else f"{owner}_{rollout}"
    return (
        Path("archived_sessions" if archive else "sessions")
        / "2026/06"
        / f"{day:02}"
        / f"rollout-2026-06-{day:02}T00-00-00-{ids}.jsonl"
    )


def _profile() -> dict[str, Any]:
    return json.loads((PROJECT / "tests/fixtures/inventory/native-state-profile.json").read_text())


def _sql_value(value: object) -> object:
    if isinstance(value, dict):
        from base64 import b64decode

        return b64decode(value["blobBase64"])
    return value


def _admission(parent: Path, count: int) -> dict[str, int]:
    if not 1 <= count <= 16_000:
        message = "Only bounded synthetic workloads through 16000 rows are admitted."
        raise ValueError(message)
    # Count actual constructor branches, including aliases and missing current files.
    physical = sum(
        1
        + int(i % 5 == 0)
        - int(i > 0 and i % 109 == 0)
        + int(i > 0 and i % 109 != 0 and i % 101 == 0)
        + int(i > 0 and i % 137 == 0)
        for i in range(count)
    )
    unit = max(4096, os.statvfs(parent).f_frsize)
    return {"files": physical + 3, "allocated_bytes": (physical + 64) * unit + count * 4096 + 65536}


def create_workloads(
    parents: tuple[Path, ...],
    counts: tuple[int, ...] = (4000, 8000, 16000),
) -> tuple[Path, ...]:
    """Admit the entire matched workload set before creating its first directory."""
    plan = [(parent, count, _admission(parent, count)) for parent in parents for count in counts]
    existing = [
        fixture_usage(root)
        for parent in set(parents)
        for root in parent.glob(".codex-session-gc-benchmark-*")
        if root.is_dir()
    ]
    if (
        sum(bound["allocated_bytes"] for _, _, bound in plan)
        + sum(value["allocated_bytes"] for value in existing)
        > MAX_ALLOCATED
        or sum(bound["files"] for _, _, bound in plan) + sum(value["files"] for value in existing)
        > MAX_FILES
    ):
        message = "The complete workload set exceeds the aggregate resource admission."
        raise ValueError(message)
    roots: list[Path] = []
    try:
        for parent, count, bound in plan:
            root = create_fixture(parent, count)
            roots.append(root)
            usage = fixture_usage(root)
            if any(usage[key] > bound[key] for key in ("files", "allocated_bytes")):
                message = "Actual fixture usage exceeds its pre-admitted reservation."
                raise ValueError(message)  # noqa: TRY301
            usages = [fixture_usage(item) for item in roots]
            if any(
                sum(item[key] for item in usages) > limit
                for key, limit in (("files", MAX_FILES), ("allocated_bytes", MAX_ALLOCATED))
            ):
                message = "Actual aggregate fixture usage exceeds admission."
                raise ValueError(message)  # noqa: TRY301
        return tuple(roots)
    except BaseException:
        for root in roots:
            cleanup(root)
        raise


def _input_snapshot(root: Path) -> dict[str, Any]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.name == "identity.json":
            continue
        metadata = path.stat(follow_symlinks=False)
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            message = "Fixture identity contains an unsupported object."
            raise ValueError(message)
        value = {
            key: getattr(metadata, key)
            for key in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        }
        if stat.S_ISREG(metadata.st_mode):
            value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        result[str(path.relative_to(root))] = value
    return result


def fixture_identity(root: Path) -> str:
    """Identify the constructor-owned snapshot for a matched comparison."""
    return hashlib.sha256((root / "identity.json").read_bytes()).hexdigest()


def verify_input(root: Path, expected_identity: str) -> None:
    """Check constructor identity outside measured phases; atime is intentionally absent."""
    if fixture_identity(root) != expected_identity:
        message = "Constructor input identity changed."
        raise ValueError(message)
    expected = json.loads((root / "identity.json").read_text())
    if _input_snapshot(root) != expected:
        message = "Fixture contents, metadata, SQL or namespace changed."
        raise ValueError(message)


def create_fixture(parent: Path, count: int) -> Path:  # noqa: C901, PLR0912, PLR0915
    """Construct independent expected membership before any catalogue observation."""
    if not 1 <= count <= 16_000:
        message = "Only bounded synthetic workloads through 16000 rows are admitted."
        raise ValueError(message)
    bound = _admission(parent, count)
    existing = [
        fixture_usage(root)
        for root in parent.glob(".codex-session-gc-benchmark-*")
        if root.is_dir()
    ]
    if (
        bound["allocated_bytes"] + sum(item["allocated_bytes"] for item in existing) > MAX_ALLOCATED
        or bound["files"] + sum(item["files"] for item in existing) > MAX_FILES
    ):
        message = "Fixture reservation exceeds resource admission."
        raise ValueError(message)
    root = Path(tempfile.mkdtemp(prefix=".codex-session-gc-benchmark-", dir=parent))
    try:
        profile = _profile()
        columns = profile["threads"]["columns"]
        template = [_sql_value(value) for value in profile["threads"]["rows"][0]]
        objects: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        edges = []
        first_initial: Path | None = None
        for index in range(count):
            owner = _uuid(index + 1)
            parent_id = _uuid(index - index % 5 + 1) if index % 5 in {1, 2} else None
            source: object = (
                {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}}
                if parent_id
                else {"subagent": {"other": "guardian"}}
                if index % 5 == 3
                else "cli"
            )
            if parent_id:
                edges.append((parent_id, owner, "closed"))
            reverted = index % 5 == 0
            current_id = _uuid(count + index + 1) if reverted else owner
            relative = _physical_path(owner, current_id, index, archive=index % 7 == 0)
            mode = "legacy" if index % 2 == 0 else "paginated"
            metadata: dict[str, Any] = {
                "id": owner,
                "source": source,
                "model_provider": "openai",
                "history_mode": mode,
            }
            if mode == "paginated":
                metadata["history_base"] = {
                    "thread_id": _uuid(1),
                    "end_ordinal_exclusive": 1,
                    "end_byte_offset": 17,
                }
            if index % 5 == 3:
                metadata["parent_thread_id"] = _uuid(index - index % 5 + 1)
            payload = (
                json.dumps({"type": "session_meta", "payload": metadata})
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "synthetic stored history " * (1 + index % 17),
                        },
                    }
                )
                + "\n"
            ).encode()
            representation = "zstd" if index % 3 == 0 else "plain"
            actual = (
                relative.with_name(relative.name + ".zst") if representation == "zstd" else relative
            )
            data = compress(payload) if representation == "zstd" else payload
            path = root / actual
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            objects.append(
                {
                    "owner": owner,
                    "rollout": current_id,
                    "path": str(actual),
                    "representation": representation,
                    "bytes": len(data),
                    "token": str(actual),
                }
            )
            if reverted:
                old = _physical_path(owner, owner, index, archive=index % 10 != 0)
                (root / old).parent.mkdir(parents=True, exist_ok=True)
                (root / old).write_bytes(payload)
                objects.append(
                    {
                        "owner": owner,
                        "rollout": owner,
                        "path": str(old),
                        "representation": "plain",
                        "bytes": len(payload),
                        "token": str(old),
                    }
                )
                if index == 0:
                    first_initial = old
            missing = index > 0 and index % 109 == 0
            if missing:
                path.unlink()
                objects.pop(-2 if reverted else -1)
            elif index > 0 and index % 101 == 0:
                alias = (
                    relative
                    if representation == "zstd"
                    else relative.with_name(relative.name + ".zst")
                )
                alternate = payload if representation == "zstd" else compress(payload)
                (root / alias).write_bytes(alternate)
                objects.append(
                    {
                        "owner": owner,
                        "rollout": current_id,
                        "path": str(alias),
                        "representation": "plain" if representation == "zstd" else "zstd",
                        "bytes": len(alternate),
                        "token": str(alias),
                    }
                )
            if index > 0 and index % 137 == 0:
                if first_initial is None:
                    message = "The constructed alias source must precede its consumers."
                    raise ValueError(message)  # noqa: TRY301
                alias = _physical_path(owner, _uuid(3 * count + index + 1), index, archive=False)
                (root / alias).parent.mkdir(parents=True, exist_ok=True)
                os.link(root / first_initial, root / alias)
                original = next(item for item in objects if item["path"] == str(first_initial))
                objects.append(
                    {
                        "owner": owner,
                        "rollout": _uuid(3 * count + index + 1),
                        "path": str(alias),
                        "representation": "plain",
                        "bytes": original["bytes"],
                        "token": original["token"],
                    }
                )
            rows.append(
                {
                    "id": owner,
                    "parent": parent_id,
                    "root": parent_id or owner,
                    "current": str(relative),
                    "current_id": current_id,
                    "source": source,
                    "archive": index % 7 == 0,
                    "mode": mode,
                    "missing": missing,
                }
            )
        by_id = Counter(item["rollout"] for item in objects)
        by_token = Counter(item["token"] for item in objects)
        excluded = {
            item["path"]
            for item in objects
            if by_id[item["rollout"]] > 1 or by_token[item["token"]] > 1
        }
        ambiguous_owners = {item["owner"] for item in objects if item["path"] in excluded}
        attributed_by_owner: dict[str, list[dict[str, Any]]] = {}
        for item in objects:
            if item["path"] not in excluded:
                attributed_by_owner.setdefault(item["owner"], []).append(item)
        for row in rows:
            attributed = attributed_by_owner.get(row["id"], [])
            row["objects"] = attributed
            row["known"] = sum(item["bytes"] for item in attributed)
            row["exact"] = None if row["missing"] or row["id"] in ambiguous_owners else row["known"]
        with sqlite3.connect(root / "state_5.sqlite") as connection:
            for item in profile["objects"]:
                if item["name"] in {"threads", "thread_spawn_edges"}:
                    connection.execute(item["sql"])
            values = []
            for row in rows:
                value = template.copy()
                for key, field in {
                    "id": row["id"],
                    "title": "Synthetic work "
                    + row["id"]
                    + "x" * (300 if int(UUID(row["id"])) % 53 == 0 else 0),
                    "cwd": "/synthetic/project",
                    "rollout_path": str(root / row["current"]),
                    "source": row["source"]
                    if isinstance(row["source"], str)
                    else json.dumps(row["source"]),
                    "archived": int(row["archive"]),
                    "history_mode": row["mode"],
                    "updated_at_ms": 1780272000000,
                    "updated_at": 1780272000,
                }.items():
                    value[columns.index(key)] = field
                values.append(value)
            connection.executemany(
                f"INSERT INTO threads ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                values,
            )
            connection.executemany("INSERT INTO thread_spawn_edges VALUES (?,?,?)", edges)
        oracle = {
            "format": FORMAT,
            "logical_count": count,
            "physical_count": len(objects),
            "rows": rows,
        }
        (root / "oracle.json").write_text(json.dumps(oracle, sort_keys=True, separators=(",", ":")))
        (root / "identity.json").write_text(json.dumps(_input_snapshot(root), sort_keys=True))
        usage = fixture_usage(root)
        if usage["allocated_bytes"] > bound["allocated_bytes"] or usage["files"] > bound["files"]:
            message = "Constructed fixture exceeds its admitted resource bounds."
            raise ValueError(message)  # noqa: TRY301
        return root  # noqa: TRY300
    except BaseException:
        shutil.rmtree(root)
        raise


def fixture_usage(root: Path) -> dict[str, int]:
    """Observe actual allocation separately from logical extents after construction."""
    files = allocated = apparent = 0
    for directory, _, names in os.walk(root):
        allocated += Path(directory).stat().st_blocks * 512
        for name in names:
            value = (Path(directory) / name).stat(follow_symlinks=False)
            files += 1
            allocated += value.st_blocks * 512
            apparent += value.st_size
    return {"files": files, "allocated_bytes": allocated, "apparent_bytes": apparent}


def cleanup(root: Path) -> None:
    """Remove only a named task fixture after its current A/B consumer releases it."""
    if (
        not root.name.startswith(".codex-session-gc-benchmark-")
        or json.loads((root / "oracle.json").read_text())["format"] != FORMAT
    ):
        message = "Cleanup requires the exact task-owned constructor manifest."
        raise ValueError(message)
    shutil.rmtree(root)


def _rchar() -> tuple[int, int]:
    data = Path("/proc/self/io").read_bytes()
    return int(dict(line.split(b": ", 1) for line in data.splitlines())[b"rchar"]), len(data)


def _verify(
    root: Path,
    oracle: dict[str, Any],
    catalogue: SessionCatalogue,
    groups: tuple[CatalogueGroup, ...],
) -> None:
    expected = {row["id"]: row for row in oracle["rows"]}
    assert len(catalogue.entries) == len(expected)
    assert {str(entry.id.value) for entry in catalogue.entries} == set(expected)
    for entry in catalogue.entries:
        row = expected[str(entry.id.value)]
        assert entry.known_stored_bytes == row["known"]
        assert entry.stored_bytes == row["exact"]
        assert {str(item.path.relative_to(root)) for item in entry.physical_rollouts} == {
            item["path"] for item in row["objects"]
        }
    by_root: dict[str, list[dict[str, Any]]] = {}
    for row in expected.values():
        by_root.setdefault(row["root"], []).append(row)
    assert {str(group.root.id.value) for group in groups} == set(by_root)
    for group in groups:
        members = by_root[str(group.root.id.value)]
        known = sum(row["known"] for row in members)
        exact = None if any(row["exact"] is None for row in members) else known
        expected_ids = {row["id"] for row in members}
        assert {str(entry.id.value) for entry in group.entries} == expected_ids
        assert len(group.entries) == len(expected_ids)
        assert {str(identity.value) for identity in group.matching_ids} == expected_ids
        assert len(group.matching_ids) == len(expected_ids)
        own = expected[str(group.root.id.value)]
        agents = [row for row in members if row["id"] != own["id"]]
        agent_known = sum(row["known"] for row in agents)
        assert group.own_known_stored_bytes == own["known"]
        assert group.agent_known_stored_bytes == agent_known
        assert group.own_stored_bytes == own["exact"]
        assert group.agent_stored_bytes == (
            None if any(row["exact"] is None for row in agents) else agent_known
        )
        assert group.own_incomplete == int(own["exact"] is None)
        assert group.agent_incomplete == sum(row["exact"] is None for row in agents)
        assert group.total_known_stored_bytes == known
        assert group.total_stored_bytes == exact
        assert group.incomplete_history_count == sum(row["exact"] is None for row in members)


@contextmanager
def _api_probe() -> Iterator[Counter[str]]:
    """Count Python API calls without claiming to count kernel syscalls."""
    originals: dict[str, Callable[..., Any]] = {
        name: getattr(os, name) for name in ("stat", "lstat", "open", "read", "pread", "scandir")
    }
    calls: Counter[str] = Counter()
    original_open = builtins.open
    original_io_open = io.open

    def wrap(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def counted(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            calls[name] += 1
            if name in {"open", "builtins_open", "io_open"} and str(args[0]).endswith(
                (".jsonl", ".jsonl.zst")
            ):
                calls["history_content_open"] += 1
            if name == "open" and not args[1] & os.O_DIRECTORY:
                calls["non_directory_os_open"] += 1
            return function(*args, **kwargs)

        return counted

    try:
        for name, function in originals.items():
            setattr(os, name, wrap(name, function))
        builtins.open = wrap("builtins_open", original_open)
        io.open = wrap("io_open", original_io_open)
        yield calls
    finally:
        for name, function in originals.items():
            setattr(os, name, function)
        builtins.open = original_open
        io.open = original_io_open


def worker(root: Path, expected_identity: str) -> dict[str, Any]:
    """Measure one cache-uncontrolled fresh interpreter against its constructor oracle."""
    from codex_session_gc.inventory.catalogue import read_session_catalogue
    from codex_session_gc.inventory.query import CatalogueIndex, CatalogueQuery

    if not expected_identity:
        message = "Observation requires the enclosing controller's pinned input identity."
        raise ValueError(message)
    oracle = json.loads((root / "oracle.json").read_text())
    before_fd = sum(1 for _ in Path("/proc/self/fd").iterdir())
    before_rchar, meter_read = _rchar()
    before_cpu, before_wall = time.process_time(), time.perf_counter()
    with _api_probe() as calls:
        catalogue = read_session_catalogue(root)
        capture_wall, capture_cpu = time.perf_counter(), time.process_time()
        index = CatalogueIndex(catalogue)
        index_wall = time.perf_counter()
        groups = index.query(CatalogueQuery())
        complete_wall = time.perf_counter()
        first = tuple(itertools.islice((entry for group in groups for entry in group.entries), 50))
        first_wall = time.perf_counter()
        capture_calls = dict(calls)
        calls.clear()
        filter_before_rchar, filter_meter = _rchar()
        calls.clear()
        filtered = index.query(CatalogueQuery(text="synthetic", min_bytes=0, sort="own"))
        filter_calls = dict(calls)
        filter_after_rchar, _ = _rchar()
        _verify(root, oracle, catalogue, groups)
        assert first
        assert filtered
        _verify(root, oracle, catalogue, filtered)
        assert not filter_calls
        assert filter_after_rchar - filter_before_rchar - filter_meter == 0
        assert capture_calls.get("history_content_open", 0) == 0
    after_rchar, _ = _rchar()
    after_fd = sum(1 for _ in Path("/proc/self/fd").iterdir())
    assert after_fd == before_fd
    measured_cpu = time.process_time() - before_cpu
    return {
        "format": FORMAT,
        "surface": "detached catalogue worker; tuple extraction, not UI readiness",
        "oracle_pass": True,
        "input_phase_validated": False,
        "input_identity": expected_identity,
        "api_count_scope": "wrapped Python APIs; excludes DirEntry methods and kernel work",
        "fd_scope": "settled before/after counts, not peak descriptor usage",
        "logical_count": oracle["logical_count"],
        "physical_count": oracle["physical_count"],
        "capture_wall_seconds": capture_wall - before_wall,
        "capture_cpu_seconds": capture_cpu - before_cpu,
        "index_seconds": index_wall - capture_wall,
        "complete_totals_seconds": complete_wall - before_wall,
        "first_useful_rows_seconds": first_wall - before_wall,
        "measured_cpu_seconds": measured_cpu,
        "rchar_including_oracle_validation": after_rchar - before_rchar - meter_read,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "python_api_calls": capture_calls,
        "detached_query_storage_api_calls": filter_calls,
        "detached_query_rchar": filter_after_rchar - filter_before_rchar - filter_meter,
        "fd_before": before_fd,
        "fd_after": after_fd,
        "first_rows": len(first),
        "cache_state": "capture after controller opening identity reads; OS caches uncontrolled",
    }


def ui_worker(root: Path, expected_identity: str) -> dict[str, Any]:  # noqa: PLR0915
    """Observe actual fresh Textual table readiness, not capture-worker process latency."""
    if not expected_identity:
        message = "Observation requires the enclosing controller's pinned input identity."
        raise ValueError(message)
    entered = time.perf_counter()
    from datetime import UTC, datetime

    from textual.widgets import DataTable, Static

    from codex_session_gc.cli import textual_console as ui
    from codex_session_gc.domain import UtcCutoff
    from codex_session_gc.inventory.query import CatalogueQuery

    imported = time.perf_counter()
    oracle = json.loads((root / "oracle.json").read_text())
    expected_groups = len({row["root"] for row in oracle["rows"]})
    reader = ui.read_session_catalogue
    capture: dict[str, float] = {}

    def timed_reader(home: Path) -> SessionCatalogue:
        started, cpu = time.perf_counter(), time.process_time()
        result = reader(home)
        capture.update(
            wall_seconds=time.perf_counter() - started, cpu_seconds=time.process_time() - cpu
        )
        return result

    ui.read_session_catalogue = timed_reader
    before_fd = sum(1 for _ in Path("/proc/self/fd").iterdir())
    before_rchar, meter = _rchar()
    started, cpu = time.perf_counter(), time.process_time()
    observed: dict[str, Any] = {}

    async def scenario(calls: Counter[str]) -> None:
        app = ui.SessionApp(
            root,
            UtcCutoff(datetime(2030, 1, 1, tzinfo=UTC)),
            Path("/measurement-not-dispatched"),
            root.parent / "measurement-unused-receipts",
        )
        async with app.run_test(size=(120, 40)) as pilot:
            table = app.query_one(DataTable)
            deadline = time.monotonic() + 600
            while not table.row_count:
                if (
                    time.monotonic() >= deadline
                    or "unavailable" in str(app.query_one("#notice", Static).render()).lower()
                ):
                    message = "The actual Textual table did not become useful."
                    raise RuntimeError(message)
                await pilot.pause(0.01)
            await pilot.pause()
            first_visible = time.perf_counter()
            assert table.is_on_screen
            assert "Synthetic work" in str(table.get_cell_at((0, 0)))
            observed["first_useful_rows_seconds"] = first_visible - started
            observed["ui_entry_to_first_useful_seconds"] = first_visible - entered
            while table.row_count != expected_groups:
                if time.monotonic() >= deadline:
                    message = "The actual Textual work rows did not complete."
                    raise RuntimeError(message)
                await pilot.pause(0.01)
            await pilot.pause()
            observed["complete_totals_seconds"] = time.perf_counter() - started
            assert f"{oracle['logical_count']:,} histories" in str(
                app.query_one("#heading", Static).render()
            )
            assert app.catalogue is not None
            assert app.index is not None
            _verify(root, oracle, app.catalogue, app.groups)
            observed["visible_work_rows"] = table.row_count
            observed["python_api_calls"] = dict(calls)
            before, meter_bytes = _rchar()
            calls.clear()
            filtered = app.index.query(CatalogueQuery(text="synthetic", min_bytes=0, sort="own"))
            query_calls = dict(calls)
            after, _ = _rchar()
            assert filtered
            _verify(root, oracle, app.catalogue, filtered)
            assert not query_calls
            assert after - before - meter_bytes == 0
            observed["detached_query_storage_api_calls"] = query_calls
            observed["detached_query_rchar"] = after - before - meter_bytes
            assert observed["python_api_calls"].get("history_content_open", 0) == 0

    try:
        with _api_probe() as calls:
            asyncio.run(scenario(calls))
    finally:
        ui.read_session_catalogue = reader
    after_rchar, _ = _rchar()
    after_fd = sum(1 for _ in Path("/proc/self/fd").iterdir())
    assert before_fd == after_fd
    measured_cpu = time.process_time() - cpu
    return {
        "format": FORMAT,
        "surface": "actual headless Textual App/Pilot; readiness observed after polling",
        "oracle_pass": True,
        "input_phase_validated": False,
        "input_identity": expected_identity,
        "api_count_scope": "wrapped Python APIs; excludes DirEntry methods and kernel work",
        "fd_scope": "settled before/after counts, not peak descriptor usage",
        "logical_count": oracle["logical_count"],
        "physical_count": oracle["physical_count"],
        "ui_import_seconds": imported - entered,
        "capture": capture,
        "measured_cpu_seconds_including_validation_and_teardown": measured_cpu,
        "rchar_including_validation_and_teardown": after_rchar - before_rchar - meter,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "fd_before": before_fd,
        "fd_after": after_fd,
        "cache_state": "fresh process; opening identity reads; uncontrolled OS caches",
        **observed,
    }


def measure(
    root: Path, repetitions: int, *, ui: bool = True, expected_identity: str | None = None
) -> list[dict[str, Any]]:
    """Run identical existing fixtures; subprocess output remains bounded structured JSON."""
    if not 1 <= repetitions <= 7:
        message = "Measurement requires one through seven explicitly bounded repetitions."
        raise ValueError(message)
    if expected_identity is None:
        message = "Matched measurement requires the constructor input identity."
        raise ValueError(message)
    # Fixtures have one owner and no concurrent writers; observers read only.
    # Persistent changes invalidate the entire phase, including earlier samples.
    verify_input(root, expected_identity)
    results = []
    try:
        for index in range(repetitions):
            started = time.perf_counter()
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "ui-observation" if ui else "capture-observation",
                    str(root),
                    "--identity",
                    expected_identity,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=PROJECT,
            )
            process_wall = time.perf_counter() - started
            if result.returncode:
                message = f"Measurement worker refused: {result.stderr[:4000]}"
                raise RuntimeError(message)
            value = json.loads(result.stdout)
            assert value["input_identity"] == expected_identity
            assert value["oracle_pass"]
            value["worker_process_wall_seconds"] = process_wall
            value["observation"] = (
                "first observed fixture capture after opening identity verification"
                if index == 0
                else "later warm-observation opportunity; no cache control"
            )
            results.append(value)
    finally:
        verify_input(root, expected_identity)
    for value in results:
        value["input_phase_validated"] = True
        value["input_validation_scope"] = (
            "one complete identity snapshot before/after the entire phase; "
            "exclusive task-owned input, no concurrent writers, read-only observers; atime excluded"
        )
    return results


def smoke(parent: Path) -> dict[str, Any]:
    """One tiny eight-row source/metric smoke with unconditional task cleanup."""
    root = create_fixture(parent, 8)
    try:
        return {
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "fixture_usage": fixture_usage(root),
            "measurements": measure(root, 1, expected_identity=fixture_identity(root)),
        }
    finally:
        cleanup(root)


def main() -> None:
    """Expose bounded smoke or individual lifecycle phases for coordinated measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "smoke",
            "worker",
            "ui-worker",
            "capture-observation",
            "ui-observation",
            "create",
            "create-set",
            "measure",
            "cleanup",
        ),
    )
    parser.add_argument("path", type=Path)
    parser.add_argument("--identity")
    parser.add_argument("--second-parent", type=Path)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=7)
    args = parser.parse_args()
    if args.action == "ui-observation":
        result: object = ui_worker(args.path, args.identity)
    elif args.action == "capture-observation":
        result = worker(args.path, args.identity)
    elif args.action in {"worker", "ui-worker"}:
        result = measure(
            args.path, 1, ui=args.action == "ui-worker", expected_identity=args.identity
        )
    elif args.action == "smoke":
        result = smoke(args.path)
    elif args.action == "create-set":
        if args.second_parent is None:
            message = "The complete workload set requires both backend parents."
            raise ValueError(message)
        roots = create_workloads((args.path, args.second_parent))
        result = [
            {"root": str(root), "identity": fixture_identity(root), "usage": fixture_usage(root)}
            for root in roots
        ]
    elif args.action == "create":
        root = create_fixture(args.path, args.count)
        result = {
            "root": str(root),
            "identity": fixture_identity(root),
            "usage": fixture_usage(root),
        }
    elif args.action == "measure":
        result = measure(args.path, args.repetitions, expected_identity=args.identity)
    else:
        cleanup(args.path)
        result = {"removed": str(args.path)}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
