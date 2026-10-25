# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004, ANN401

from __future__ import annotations

import errno
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from codex_session_gc.domain import ArchiveState, ThreadId
from codex_session_gc.inventory import catalogue, metadata_walk
from tests.current_storage_fixture import _A, _B, _C
from tests.current_storage_fixture import _catalogue_home as _home


def test_metadata_grouping_sizes_and_source_preservation(tmp_path: Path) -> None:
    database = _home(tmp_path)
    before = {p: (p.read_bytes(), p.stat()) for p in tmp_path.rglob("*") if p.is_file()}
    result = catalogue.read_session_catalogue(tmp_path)
    assert not result.diagnostics
    rows = {str(row.id.value): row for row in result.entries}
    assert rows[_A].root_id == ThreadId.parse(_A)
    assert rows[_B].root_id == ThreadId.parse(_A)
    assert rows[_B].parent_id == ThreadId.parse(_A)
    assert rows[_B].archive_state is ArchiveState.ARCHIVED
    assert rows[_A].children == (ThreadId.parse(_B),)
    assert rows[_B].agent_status == "active"
    assert rows[_C].root_id == ThreadId.parse(_C)
    assert rows[_C].parent_id is None
    assert all(row.title == "Visible title" and row.cwd == "/project" for row in rows.values())
    assert all(row.stored_bytes == len(b"not history JSON") for row in rows.values())
    activity = rows[_A].last_activity
    assert activity is not None
    assert activity.timestamp() == 1234567890
    assert database.exists()
    for path, (data, previous) in before.items():
        current = path.stat()
        assert path.read_bytes() == data
        assert (current.st_size, current.st_mtime_ns, current.st_ctime_ns) == (
            previous.st_size,
            previous.st_mtime_ns,
            previous.st_ctime_ns,
        )


@pytest.mark.parametrize("path_kind", ["missing", "outside", "symlink", "directory"])
def test_unavailable_rollout_is_visible_without_history_reads(
    tmp_path: Path, path_kind: str
) -> None:
    database = _home(tmp_path)
    path = tmp_path / "sessions" / "missing.jsonl"
    if path_kind == "outside":
        path = tmp_path.parent / "outside.jsonl"
    elif path_kind == "symlink":
        path.symlink_to(tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{_A}.jsonl")
    elif path_kind == "directory":
        path.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (str(path), _A))
    row = next(
        row for row in catalogue.read_session_catalogue(tmp_path).entries if str(row.id.value) == _A
    )
    assert row.stored_bytes is None
    assert row.issue is not None


@pytest.mark.parametrize("kind", ["cycle", "dangling", "status"])
def test_invalid_topology_never_invents_selectable_root(tmp_path: Path, kind: str) -> None:
    database = _home(tmp_path)
    with sqlite3.connect(database) as connection:
        if kind == "cycle":
            connection.execute("INSERT INTO thread_spawn_edges VALUES (?,?,'closed')", (_B, _A))
        elif kind == "dangling":
            connection.execute("DELETE FROM threads WHERE id=?", (_A,))
        else:
            connection.execute("UPDATE thread_spawn_edges SET status='unknown'")
    result = catalogue.read_session_catalogue(tmp_path)
    row = next(row for row in result.entries if str(row.id.value) == _B)
    assert row.root_id is None
    assert row.issue == "invalid_spawn_topology"
    assert result.diagnostics


def _chain_entries(count: int) -> list[catalogue.SessionEntry]:
    return [
        catalogue.SessionEntry(
            ThreadId(UUID(int=number)),
            "",
            "",
            None,
            ArchiveState.NOT_ARCHIVED,
            None,
            None,
            (),
            0,
            None,
            None,
        )
        for number in range(1, count + 1)
    ]


def test_deep_catalogue_chain_has_linear_identity_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doubling a valid deep chain does not quadruple identity operations."""
    original_hash = ThreadId.__hash__
    calls = 0

    def counted_hash(value: ThreadId) -> int:
        nonlocal calls
        calls += 1
        return original_hash(value)

    monkeypatch.setattr(ThreadId, "__hash__", counted_hash)
    measurements = []
    for count in (1200, 2400):
        entries = _chain_entries(count)
        edges = tuple(
            (str(parent.id.value), str(child.id.value), "closed")
            for parent, child in pairwise(entries)
        )
        calls = 0
        diagnostics: list[str] = []
        observed = catalogue._topology(edges, entries, diagnostics)  # noqa: SLF001
        measurements.append(calls)
        assert not diagnostics
        assert all(entry.root_id == entries[0].id for entry in observed)
        assert len(observed) == count
    assert measurements[1] < measurements[0] * 3


def test_topology_reuses_roots_without_hiding_invalid_branches() -> None:
    entries = _chain_entries(9)
    relations = ((1, 2), (2, 3), (1, 4), (5, 6), (6, 5), (6, 7), (2, 8), (4, 8), (8, 9))
    edges = tuple(
        (str(entries[parent - 1].id.value), str(entries[child - 1].id.value), "closed")
        for parent, child in relations
    )
    diagnostics: list[str] = []
    result = {
        entry.id: entry
        for entry in catalogue._topology(edges, entries, diagnostics)  # noqa: SLF001
    }
    assert all(result[entries[i - 1].id].root_id == entries[0].id for i in (1, 2, 3, 4))
    assert all(result[entries[i - 1].id].root_id is None for i in (5, 6, 7, 8, 9))
    assert all(result[entries[i - 1].id].issue == "invalid_spawn_topology" for i in (5, 6, 7, 8, 9))


def test_repeated_malformed_children_have_linear_set_work() -> None:
    """Account for built-in set updates even when CPython reuses cached hashes."""
    measurements = []
    for count in (1200, 2400):
        entries = _chain_entries(count)
        edges = tuple((str(entries[0].id.value), "malformed", "closed") for _ in range(count))
        work = 0

        def measure(_frame: Any, event: str, function: Any) -> None:
            nonlocal work
            if event == "c_call":
                # Merging into an already full invalid set still visits its
                # members in C, even without any Python __hash__ invocation.
                target = getattr(function, "__self__", None)
                work += (
                    max(1, len(target))
                    if isinstance(target, set) and function.__name__ == "update"
                    else 1
                )

        diagnostics: list[str] = []
        previous = sys.getprofile()
        try:
            sys.setprofile(measure)
            observed = catalogue._topology(edges, entries, diagnostics)  # noqa: SLF001
        finally:
            sys.setprofile(previous)
        measurements.append(work)
        assert len(observed) == count
        assert all(entry.root_id is None for entry in observed)
        assert all(entry.issue == "invalid_spawn_topology" for entry in observed)
        assert diagnostics.count("invalid_spawn_child") == count
        assert diagnostics.count("invalid_spawn_topology") == count
    assert measurements[1] < measurements[0] * 3


def test_malformed_child_invalidates_all_work_without_losing_valid_relations() -> None:
    entries = _chain_entries(4)
    first, second, third, fourth = (entry.id for entry in entries)
    edges = (
        (str(first.value), str(second.value), "closed"),
        (str(third.value), "malformed", "closed"),
        (str(second.value), str(third.value), "active"),
        (str(fourth.value), "malformed", "open"),
    )
    diagnostics: list[str] = []
    result = {
        entry.id: entry
        for entry in catalogue._topology(edges, entries, diagnostics)  # noqa: SLF001
    }
    assert set(result) == {first, second, third, fourth}
    assert all(entry.root_id is None for entry in result.values())
    assert result[first].children == (second,)
    assert result[second].parent_id == first
    assert result[second].children == (third,)
    assert result[second].agent_status == "closed"
    assert result[third].parent_id == second
    assert result[third].agent_status == "active"
    assert diagnostics == ["invalid_spawn_child"] * 2 + ["invalid_spawn_topology"] * 4


@pytest.mark.parametrize("kind", ["missing", "multiple", "corrupt", "schema", "symlink"])
def test_invalid_state_returns_diagnostics(tmp_path: Path, kind: str) -> None:
    database = tmp_path / "state_5.sqlite"
    if kind == "multiple":
        database.touch()
        (tmp_path / "state_6.sqlite").touch()
    elif kind == "corrupt":
        database.write_bytes(b"not SQLite")
    elif kind == "schema":
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE threads(id TEXT)")
    elif kind == "symlink":
        database.symlink_to(tmp_path / "absent")
    result = catalogue.read_session_catalogue(tmp_path)
    assert not result.entries
    assert result.diagnostics


def test_transaction_closes_before_namespace_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _home(tmp_path)
    original = os.stat
    written = False
    injection = threading.Lock()

    def stat_after_write(
        path: object, *, dir_fd: int | None = None, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal written
        with injection:
            if dir_fd is not None and not written:
                with sqlite3.connect(database, timeout=0) as writer:
                    writer.execute("BEGIN EXCLUSIVE")
                    writer.execute("UPDATE threads SET title='Changed'")
                written = True
        return original(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", stat_after_write)
    result = catalogue.read_session_catalogue(tmp_path)
    assert written
    assert result.entries
    assert all(row.title == "Visible title" for row in result.entries)
    assert not result.source_changed
    assert not result.diagnostics


def test_seconds_fallback_and_recent_first(tmp_path: Path) -> None:
    database = _home(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE threads SET updated_at_ms=NULL,updated_at=2000000000 WHERE id=?", (_C,)
        )
    result = catalogue.read_session_catalogue(tmp_path)
    assert result.entries[0].id == ThreadId.parse(_C)
    activity = result.entries[0].last_activity
    assert activity is not None
    assert activity.timestamp() == 2000000000


def test_live_wal_and_sidecars_are_not_changed(tmp_path: Path) -> None:
    database = _home(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE threads SET title='In WAL'")
        connection.commit()
        files = [database, Path(f"{database}-wal")]
        before = {p: (p.read_bytes(), p.stat()) for p in files}
        result = catalogue.read_session_catalogue(tmp_path)
        assert result.entries
        assert all(row.title == "In WAL" for row in result.entries)
        for path, (data, metadata) in before.items():
            current = path.stat()
            assert path.read_bytes() == data
            assert (current.st_ino, current.st_size, current.st_mtime_ns) == (
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )


def test_ancestor_replacement_cannot_redirect_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _home(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    name = f"rollout-2026-06-01T00-00-00-{_A}.jsonl"
    (outside / name).write_bytes(b"x" * 999)
    original = os.stat
    replaced = False

    def replace_before_final_stat(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal replaced
        if str(path).endswith(name) and not replaced:
            replaced = True
            (home / "sessions").rename(home / "original-sessions")
            (home / "sessions").symlink_to(outside, target_is_directory=True)
        return original(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", replace_before_final_stat)
    result = catalogue.read_session_catalogue(home)
    row = next(row for row in result.entries if row.id == ThreadId.parse(_A))
    assert replaced
    assert row.stored_bytes == len(b"not history JSON")
    assert row.stored_bytes != 999


def test_grouped_sizes_reuse_directories_and_close_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _home(tmp_path)
    opened: list[int] = []
    original_open, original_dup = os.open, os.dup

    def track_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    def track_dup(descriptor: int) -> int:
        duplicate = original_dup(descriptor)
        opened.append(duplicate)
        return duplicate

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "dup", track_dup)
    assert catalogue.read_session_catalogue(tmp_path).entries
    initial_operations = len(opened)
    opened.clear()
    with sqlite3.connect(database) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(threads)")]
        template = list(connection.execute("SELECT * FROM threads LIMIT 1").fetchone())
        for number in range(400):
            identifier = str(UUID(int=number + 1))
            path = tmp_path / "sessions" / f"rollout-2026-06-01T00-00-00-{identifier}.jsonl"
            path.write_bytes(b"catalogue")
            values = template.copy()
            values[columns.index("id")] = identifier
            values[columns.index("rollout_path")] = str(path)
            marks = ",".join("?" for _ in columns)
            connection.execute(f"INSERT INTO threads VALUES ({marks})", values)  # noqa: S608
    result = catalogue.read_session_catalogue(tmp_path)
    assert len(result.entries) == 403
    assert sum(row.stored_bytes == len(b"catalogue") for row in result.entries) == 400
    # Growing one directory's file set must not repeat its directory-open work.
    assert len(opened) == initial_operations
    for descriptor in set(opened):
        with pytest.raises(OSError) as error:  # noqa: PT011
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


def test_directory_descriptors_close_when_observation_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path)
    original = os.stat
    directory_fd: int | None = None

    def interrupt_final_stat(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal directory_fd
        if dir_fd is not None:
            directory_fd = dir_fd
            raise KeyboardInterrupt
        return original(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", interrupt_final_stat)
    with pytest.raises(KeyboardInterrupt):
        catalogue.read_session_catalogue(tmp_path)
    assert directory_fd is not None
    with pytest.raises(OSError) as error:  # noqa: PT011
        os.fstat(directory_fd)
    assert error.value.errno == errno.EBADF


def test_actual_sources_do_not_infer_main_from_missing_parent(tmp_path: Path) -> None:
    database = _home(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM thread_spawn_edges")
        for identifier, source in (
            (_A, "cli"),
            (_B, '{"subagent":{"thread_spawn":{"parent_thread_id":"' + _A + '"}}}'),
            (_C, '{"subagent":{"other":"guardian"}}'),
        ):
            connection.execute("UPDATE threads SET source=? WHERE id=?", (source, identifier))
    result = catalogue.read_session_catalogue(tmp_path)
    rows = {str(row.id.value): row for row in result.entries}
    assert rows[_A].source is catalogue.SessionSource.MAIN
    assert rows[_B].source is catalogue.SessionSource.AGENT
    assert rows[_C].source is catalogue.SessionSource.GUARDIAN
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET source='unrecognized' WHERE id=?", (_A,))
    unknown = next(
        row for row in catalogue.read_session_catalogue(tmp_path).entries if str(row.id.value) == _A
    )
    assert unknown.source is catalogue.SessionSource.UNKNOWN


def test_irrelevant_sql_payload_and_sparse_bodies_are_not_materialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _home(tmp_path)
    before = catalogue.read_session_catalogue(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET first_user_message=zeroblob(2000000)")
    for path in (tmp_path / "sessions").iterdir():
        with path.open("r+b") as stream:
            stream.truncate(2**34)

    original_open = os.open

    def reject_history_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if str(path).endswith(".jsonl"):
            pytest.fail("Catalogue opened history content")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", reject_history_open)
    after = catalogue.read_session_catalogue(tmp_path)
    assert len(after.entries) == len(before.entries) == 3
    assert not after.diagnostics
    assert all(row.stored_bytes == 2**34 for row in after.entries)
    assert [row.title for row in after.entries] == [row.title for row in before.entries]


@pytest.mark.parametrize(
    "source",
    [
        '{"subagent":{"thread_spawn":{},"other":"guardian"}}',
        '{"subagent":{"thread_spawn":{}},"extra":true}',
        '{"subagent":{"other":"guardian"},"extra":true}',
    ],
)
def test_conflicting_source_variants_stay_unknown(tmp_path: Path, source: str) -> None:
    database = _home(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET source=? WHERE id=?", (source, _A))
    row = next(
        row for row in catalogue.read_session_catalogue(tmp_path).entries if str(row.id.value) == _A
    )
    assert row.source is catalogue.SessionSource.UNKNOWN


@pytest.mark.parametrize("chunk_size", [1, 4, 16])
def test_nested_metadata_work_has_one_global_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chunk_size: int
) -> None:
    _home(tmp_path)
    nested = tmp_path / "sessions" / "nested"
    nested.mkdir()
    for number in range(5):
        (nested / str(number)).touch()
    monkeypatch.setattr(metadata_walk, "WORKERS", 2)
    monkeypatch.setattr(metadata_walk, "CHUNK_SIZE", chunk_size)
    for number in range(2 * chunk_size + 1):
        (tmp_path / "sessions" / str(number)).touch()
    original_stat = os.stat
    gate = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active = peak = submitted = peak_submitted = calls = 0
    workers: set[threading.Thread] = set()

    class BoundedPool(ThreadPoolExecutor):
        def submit(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal submitted, peak_submitted
            with lock:
                submitted += 1
                peak_submitted = max(peak_submitted, submitted)
            future = super().submit(*args, **kwargs)

            def settled(_future: Any) -> None:
                nonlocal submitted
                with lock:
                    submitted -= 1

            future.add_done_callback(settled)
            return future

    def observed_stat(path: Any, **kwargs: Any) -> os.stat_result:
        nonlocal active, peak, calls
        if kwargs.get("dir_fd") is None:
            return original_stat(path, **kwargs)
        with lock:
            active += 1
            calls += 1
            ordinal = calls
            peak = max(peak, active)
            workers.add(threading.current_thread())
        try:
            if ordinal <= 2:
                gate.wait()
            assert kwargs["follow_symlinks"] is False
            return original_stat(path, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(metadata_walk, "ThreadPoolExecutor", BoundedPool)
    monkeypatch.setattr(os, "stat", observed_stat)
    result = catalogue.read_session_catalogue(tmp_path)
    assert result.physical_complete
    assert peak == peak_submitted == 2
    assert active == submitted == 0
    assert calls == 3 + 1 + 5 + (2 * chunk_size + 1)
    assert all(not worker.is_alive() for worker in workers)


@pytest.mark.parametrize("failure", ["submission", "worker"])
def test_metadata_failure_joins_borrowers_before_closing_parent(  # noqa: C901
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _home(tmp_path)
    monkeypatch.setattr(metadata_walk, "WORKERS", 2)
    monkeypatch.setattr(metadata_walk, "CHUNK_SIZE", 4)
    for number in range(5):
        (tmp_path / "sessions" / str(number)).touch()
    entered, release, failed = threading.Event(), threading.Event(), threading.Event()
    original_stat = os.stat
    lock = threading.Lock()
    loans: list[int] = []
    workers: set[threading.Thread] = set()
    errors: list[BaseException] = []

    class FailingPool(ThreadPoolExecutor):
        submissions = 0

        def submit(self, *args: Any, **kwargs: Any) -> Any:
            self.submissions += 1
            if failure == "submission" and self.submissions == 2:
                assert entered.wait(5)
                failed.set()
                message = "submission refused"
                raise RuntimeError(message)
            return super().submit(*args, **kwargs)

    def delayed_stat(path: Any, **kwargs: Any) -> os.stat_result:
        if kwargs.get("dir_fd") is None:
            return original_stat(path, **kwargs)
        with lock:
            loans.append(kwargs["dir_fd"])
            first = len(loans) == 1
            workers.add(threading.current_thread())
        if first:
            entered.set()
            assert release.wait(5)
        elif failure == "worker":
            assert entered.wait(5)
            failed.set()
            raise KeyboardInterrupt
        return original_stat(path, **kwargs)

    monkeypatch.setattr(metadata_walk, "ThreadPoolExecutor", FailingPool)
    monkeypatch.setattr(os, "stat", delayed_stat)

    def observe() -> None:
        try:
            catalogue.read_session_catalogue(tmp_path)
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    observer = threading.Thread(target=observe)
    observer.start()
    try:
        assert failed.wait(5)
        assert observer.is_alive()
        assert not errors
        assert loans
        assert os.fstat(loans[0])
    finally:
        release.set()
        observer.join(10)
    assert not observer.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError if failure == "submission" else KeyboardInterrupt)
    assert all(not worker.is_alive() for worker in workers)
    for descriptor in set(loans):
        with pytest.raises(OSError) as error:  # noqa: PT011
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("consume", [False, True])
def test_metadata_consumer_exception_closes_even_unstarted_directory(
    tmp_path: Path, *, consume: bool
) -> None:
    (tmp_path / "file").touch()
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    with metadata_walk.Walker() as walker:
        with pytest.raises(RuntimeError, match="consumer stopped"):  # noqa: PT012, SIM117
            with walker.files(descriptor, tmp_path, []) as files:
                if consume:
                    next(files)
                message = "consumer stopped"
                raise RuntimeError(message)
        with pytest.raises(OSError) as error:  # noqa: PT011
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


def test_reverse_metadata_completion_preserves_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path)
    monkeypatch.setattr(metadata_walk, "WORKERS", 2)
    monkeypatch.setattr(metadata_walk, "CHUNK_SIZE", 1)
    expected = catalogue.read_session_catalogue(tmp_path)
    with os.scandir(tmp_path / "sessions") as entries:
        first, second = [entry.name for entry in entries][:2]
    completed: list[str] = []
    later = threading.Event()
    original_stat = os.stat

    def reverse_stat(path: Any, **kwargs: Any) -> os.stat_result:
        if kwargs.get("dir_fd") is not None:
            if path == first:
                assert later.wait(5)
            value = original_stat(path, **kwargs)
            completed.append(str(path))
            if path == second:
                later.set()
            return value
        return original_stat(path, **kwargs)

    monkeypatch.setattr(os, "stat", reverse_stat)
    assert catalogue.read_session_catalogue(tmp_path) == expected
    assert completed[:2] == [second, first]


def test_scandir_failure_preserves_successfully_enumerated_known_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path)
    original_scandir = os.scandir
    session_inode = (tmp_path / "sessions").stat().st_ino
    prefix: list[str] = []

    class InterruptedEntries(AbstractContextManager[Any]):
        def __init__(self, entries: Any) -> None:
            self.entries = entries

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_args: object) -> None:
            self.entries.close()

        def __iter__(self) -> Any:
            return self

        def __next__(self) -> Any:
            if prefix:
                message = "enumeration stopped"
                raise OSError(message)
            entry = next(self.entries)
            prefix.append(entry.name)
            return entry

    def interrupted_scandir(path: Any) -> Any:
        entries = original_scandir(path)
        if isinstance(path, int) and os.fstat(path).st_ino == session_inode:
            return InterruptedEntries(entries)
        return entries

    monkeypatch.setattr(os, "scandir", interrupted_scandir)
    result = catalogue.read_session_catalogue(tmp_path)
    assert result.diagnostics == ("physical_namespace_unavailable",)
    assert not result.physical_complete
    assert all(entry.stored_bytes is None for entry in result.entries)
    assert sum(entry.known_stored_bytes for entry in result.entries) == len(b"not history JSON")
    assert [
        item.path.name for entry in result.entries for item in entry.physical_rollouts
    ] == prefix
