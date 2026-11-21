# Copyright (c) 2026

"""Physical inventory observations retain identity and close borrowed resources."""

from __future__ import annotations

import errno
import json
import os
import time
from compression import zstd
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event, local
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from codex_session_gc.domain import InvalidRecordError, ThreadId
from codex_session_gc.execution import bounded_header, closed_work_request, physical_inventory
from codex_session_gc.execution.closed_work_request import CollectionBudget, PreparationRefused
from codex_session_gc.execution.physical_inventory import _Entry, observe_inventory
from codex_session_gc.execution.resource_policy import collection_limits

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_WAIT = 10.0
_SECOND_SUBMISSION = 2


def _file(home: Path, owner: int = 2) -> Path:
    identifier = str(UUID(int=owner))
    path = home / "sessions" / "nested" / f"rollout-2026-06-01T00-00-00-{identifier}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": identifier, "history_mode": "legacy"}}
        )
        + "\n"
    )
    return path


def _budget() -> CollectionBudget:
    limits = collection_limits()
    return CollectionBudget(limits, time.monotonic() + limits.deadline_seconds)


def _fds() -> set[str]:
    return {item.name for item in Path("/proc/self/fd").iterdir()}


def _wait(event: Event) -> None:
    if not event.wait(_WAIT):
        pytest.fail("inventory barrier timed out")


def _ordered_files(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    *,
    after_first: Callable[[], None] | None = None,
) -> tuple[Path, ...]:
    paths = tuple(_file(home, owner) for owner in range(2, count + 2))

    def entries(_home: Path) -> Generator[_Entry]:
        descriptor = os.open(paths[0].parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for index, path in enumerate(paths):
                if index == 1 and after_first is not None:
                    after_first()
                yield _Entry(path, descriptor)
        finally:
            os.close(descriptor)

    monkeypatch.setattr(physical_inventory, "_files", entries)
    return paths


def _track_pool(
    monkeypatch: pytest.MonkeyPatch,
    after_submit: Callable[[int, Future[None]], None],
    *,
    before_submit: Callable[[int], None] | None = None,
) -> None:
    def create(max_workers: int) -> ThreadPoolExecutor:
        pool = ThreadPoolExecutor(max_workers=max_workers)
        submit = pool.submit
        attempts = 0

        def track(
            function: Callable[..., None], /, *args: object, **kwargs: object
        ) -> Future[None]:
            nonlocal attempts
            attempts += 1
            if before_submit is not None:
                before_submit(attempts)
            result = submit(function, *args, **kwargs)
            after_submit(attempts, result)
            return result

        monkeypatch.setattr(pool, "submit", track)
        return pool

    monkeypatch.setattr(physical_inventory, "ThreadPoolExecutor", create)


def _open_histories(paths: tuple[Path, ...]) -> list[Path]:
    targets = set(paths)
    observed = []
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            path = descriptor.readlink()
        except FileNotFoundError:
            continue
        if path in targets:
            observed.append(path)
    return observed


def _track_window(
    monkeypatch: pytest.MonkeyPatch,
    capacity: int,
    oldest_waiting: Event,
    oldest_release: Event,
    second_done: Event,
) -> None:
    def submitted(index: int, future: Future[None]) -> None:
        if index == 1:
            result = future.result

            def wait_for_oldest(timeout: float | None = None) -> None:
                oldest_waiting.set()
                result(timeout)

            monkeypatch.setattr(future, "result", wait_for_oldest)
        elif index == _SECOND_SUBMISSION:
            future.add_done_callback(lambda _future: second_done.set())

    def check_capacity(index: int) -> None:
        if index > capacity and not oldest_release.is_set():
            pytest.fail("window advanced before the oldest lookup settled")

    _track_pool(monkeypatch, submitted, before_submit=check_capacity)


def _track_shutdown(
    monkeypatch: pytest.MonkeyPatch, before_shutdown: Callable[[int], None]
) -> None:
    def create(max_workers: int) -> ThreadPoolExecutor:
        pool = ThreadPoolExecutor(max_workers=max_workers)
        shutdown = pool.shutdown
        attempts = 0

        def stop(wait: bool = True, *, cancel_futures: bool = False) -> None:  # noqa: FBT001, FBT002
            nonlocal attempts
            attempts += 1
            if not wait:
                pytest.fail("inventory asked the executor to abandon running checks")
            before_shutdown(attempts)
            shutdown(wait=wait, cancel_futures=cancel_futures)

        monkeypatch.setattr(pool, "shutdown", stop)
        return pool

    monkeypatch.setattr(physical_inventory, "ThreadPoolExecutor", create)


def test_inventory_refuses_parent_replacement_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement ancestry cannot pass the final current-path identity check."""
    path = _file(tmp_path)
    inode = path.stat().st_ino
    original = os.pread
    changed = False

    def rename_during_read(fd: int, size: int, offset: int) -> bytes:
        nonlocal changed
        data = original(fd, size, offset)
        if not changed and os.fstat(fd).st_ino == inode:
            changed = True
            moved = path.parent.with_name("moved")
            path.parent.rename(moved)
            path.parent.mkdir()
            path.write_bytes((moved / path.name).read_bytes())
        return data

    before = _fds()
    monkeypatch.setattr(bounded_header.os, "pread", rename_during_read)
    with pytest.raises(PreparationRefused, match="changed during header capture"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if not changed or _fds() != before:
        pytest.fail("replacement did not occur or descriptors remained open")


def test_inventory_filename_failure_closes_nested_descriptors(tmp_path: Path) -> None:
    """Invalid nonempty filenames cannot leak suspended traversal descriptors."""
    path = _file(tmp_path)
    path.rename(path.with_name("not-a-rollout.jsonl"))
    before = _fds()
    with pytest.raises(InvalidRecordError):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if _fds() != before:
        pytest.fail("an inventory descriptor remained open")


def test_inventory_selected_and_empty_current_refuse(tmp_path: Path) -> None:
    """An empty current object cannot satisfy an admitted physical identity."""
    path = _file(tmp_path)
    path.write_bytes(b"")
    identifier = ThreadId(UUID(int=2))
    with pytest.raises(PreparationRefused, match="physical history is empty"):
        observe_inventory(
            tmp_path,
            {str(identifier.value): {"rollout_path": str(path)}},
            frozenset({identifier}),
            _budget(),
        )


@pytest.mark.parametrize("selected", [False, True])
def test_inventory_append_respects_selected_extent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, selected: bool
) -> None:
    """Unrelated tail append is allowed; a selected extent change is refused."""
    path = _file(tmp_path)
    inode = path.stat().st_ino
    original = os.pread
    changed = False

    def append_during_read(fd: int, size: int, offset: int) -> bytes:
        nonlocal changed
        data = original(fd, size, offset)
        if not changed and os.fstat(fd).st_ino == inode:
            changed = True
            with path.open("ab") as stream:
                stream.write(b"new body\n")
        return data

    monkeypatch.setattr(bounded_header.os, "pread", append_during_read)
    owners = frozenset({ThreadId(UUID(int=2))}) if selected else frozenset()
    if selected:
        with pytest.raises(PreparationRefused, match="changed during metadata observation"):
            observe_inventory(tmp_path, {}, owners, _budget())
    else:
        inventory, _ = observe_inventory(tmp_path, {}, owners, _budget())
        if len(inventory.rollouts) != 1:
            pytest.fail("unrelated append lost the observed physical object")
    if not changed:
        pytest.fail("append did not occur during header capture")


def test_inventory_rejects_competing_representation(tmp_path: Path) -> None:
    """Competing plain and compressed files cannot claim one rollout identity."""
    path = _file(tmp_path)
    path.with_name(path.name + ".zst").write_bytes(zstd.compress(path.read_bytes()))
    with pytest.raises(PreparationRefused, match="duplicate or competing"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())


def test_inventory_budget_refuses_without_leaking_descriptors(tmp_path: Path) -> None:
    """The captured-input byte ceiling applies to borrowed file descriptors."""
    _file(tmp_path)
    limits = collection_limits(maximum_bytes=4)
    before = _fds()
    with pytest.raises(PreparationRefused):
        observe_inventory(
            tmp_path, {}, frozenset(), CollectionBudget(limits, time.monotonic() + 10)
        )
    if before != _fds():
        pytest.fail("a descriptor remained open after budget refusal")


def test_inventory_refuses_header_change_during_unrelated_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retained body append cannot conceal a simultaneous header rewrite."""
    path = _file(tmp_path)
    inode = path.stat().st_ino
    original = os.pread
    changed = False

    def rewrite_during_read(fd: int, size: int, offset: int) -> bytes:
        nonlocal changed
        data = original(fd, size, offset)
        if not changed and os.fstat(fd).st_ino == inode:
            changed = True
            path.write_bytes(data.replace(b"legacy", b"other!") + b"new body\n")
        return data

    monkeypatch.setattr(bounded_header.os, "pread", rewrite_during_read)
    with pytest.raises(PreparationRefused, match="metadata header changed"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if not changed:
        pytest.fail("header rewrite did not occur")


def test_inventory_accepts_single_compressed_header(tmp_path: Path) -> None:
    """A compressed physical object has the same owner and bounded observation."""
    path = _file(tmp_path)
    compressed = path.with_name(path.name + ".zst")
    compressed.write_bytes(zstd.compress(path.read_bytes()))
    path.unlink()
    inventory, _ = observe_inventory(tmp_path, {}, frozenset(), _budget())
    if len(inventory.rollouts) != 1 or inventory.rollouts[0].ownership.thread_id != ThreadId(
        UUID(int=2)
    ):
        pytest.fail("compressed rollout owner was not observed")


def test_inventory_refuses_symlink_and_closes_nested_descriptors(tmp_path: Path) -> None:
    """A symlink in the managed namespace is not admitted as a regular file."""
    path = _file(tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    before = _fds()
    with pytest.raises(PreparationRefused, match="unresolved alias"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if _fds() != before:
        pytest.fail("a descriptor remained open after symlink refusal")


def test_inventory_refuses_invalid_header_and_closes_descriptors(tmp_path: Path) -> None:
    """A malformed metadata envelope cannot leave file or traversal ownership open."""
    path = _file(tmp_path)
    path.write_text('{"type":"wrong"}\n')
    before = _fds()
    with pytest.raises(PreparationRefused, match="metadata line is unavailable"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if _fds() != before:
        pytest.fail("a descriptor remained open after malformed metadata")


def test_inventory_expired_deadline_closes_descriptors(tmp_path: Path) -> None:
    """An expired cooperative deadline cannot leak traversal ownership."""
    _file(tmp_path)
    before = _fds()
    with pytest.raises(PreparationRefused, match="deadline expired"):
        observe_inventory(tmp_path, {}, frozenset(), CollectionBudget(collection_limits(), 0))
    if _fds() != before:
        pytest.fail("a descriptor remained open after deadline refusal")


def test_inventory_refuses_ancestor_symlink_before_header_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No rollout bytes are read through an aliased namespace ancestor."""
    real = tmp_path / "real"
    _file(real)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    original = os.pread
    reads = 0

    def count_reads(fd: int, size: int, offset: int) -> bytes:
        nonlocal reads
        reads += 1
        return original(fd, size, offset)

    before = _fds()
    monkeypatch.setattr(bounded_header.os, "pread", count_reads)
    with pytest.raises(NotADirectoryError):
        observe_inventory(alias, {}, frozenset(), _budget())
    if reads or _fds() != before:
        pytest.fail("an aliased ancestor was read or a descriptor remained open")


def test_inventory_bounds_pending_checks_and_pins_their_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fifth lookup waits for the oldest even after a later one finishes."""
    capacity = 4
    paths = _ordered_files(tmp_path, monkeypatch, capacity + 1)
    started = {path: Event() for path in paths}
    release = {path: Event() for path in paths}
    waiting_for_oldest = Event()
    second_done = Event()
    original_sample = closed_work_request._sample  # noqa: SLF001

    def sample(path: Path) -> tuple[int, int] | None:
        started[path].set()
        _wait(release[path])
        return original_sample(path)

    _track_window(monkeypatch, capacity, waiting_for_oldest, release[paths[0]], second_done)
    monkeypatch.setattr(closed_work_request, "_sample", sample)
    before = _fds()
    with ThreadPoolExecutor(max_workers=1) as driver:
        observed = driver.submit(observe_inventory, tmp_path, {}, frozenset(), _budget())
        try:
            for path in paths[:capacity]:
                _wait(started[path])
            _wait(waiting_for_oldest)
            if sorted(_open_histories(paths)) != sorted(paths[:capacity]):
                pytest.fail("pending lookups did not own exactly four original leaves")
            release[paths[1]].set()
            _wait(second_done)
            if started[paths[-1]].is_set():
                pytest.fail("fifth lookup started before the oldest settled")
        finally:
            for event in release.values():
                event.set()
        inventory, _ = observed.result(_WAIT)
    if [rollout.ownership.thread_id.value for rollout in inventory.rollouts] != [
        UUID(int=owner) for owner in range(2, len(paths) + 2)
    ]:
        pytest.fail("completed lookups changed the enumeration order")
    if _fds() != before:
        pytest.fail("completed inventory leaked descriptors")


@pytest.mark.parametrize("later_producer_failure", [False, True])
def test_inventory_returns_the_earliest_refusal_after_out_of_order_lookups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    later_producer_failure: bool,
) -> None:
    """A faster later failure cannot override the first pathname mismatch."""
    second_done = Event()

    def read_ahead() -> None:
        if later_producer_failure:
            second_done.set()
            message = "a later inventory entry failed"
            raise PreparationRefused(message)

    first, second = _ordered_files(tmp_path, monkeypatch, 2, after_first=read_ahead)

    def sample(path: Path) -> None:
        if path == first:
            _wait(second_done)
            return
        if path != second:
            pytest.fail("unexpected lookup path")
        message = "a later pathname probe failed"
        raise PreparationRefused(message)

    def submitted(index: int, future: Future[None]) -> None:
        if index == _SECOND_SUBMISSION:
            future.add_done_callback(lambda _future: second_done.set())

    _track_pool(monkeypatch, submitted)
    monkeypatch.setattr(closed_work_request, "_sample", sample)
    before = _fds()
    with pytest.raises(PreparationRefused, match="changed during header capture"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if not second_done.is_set() or _fds() != before:
        pytest.fail("later lookup did not finish or inventory leaked descriptors")


@pytest.mark.parametrize(
    ("replacement", "header", "diagnostic"),
    [
        (True, '{"type":"wrong"}\n', "changed during header capture"),
        (False, '{"type":"wrong"}\n', "metadata line is unavailable"),
        (
            False,
            '{"type":"session_meta","payload":{"id":"different owner"}}\n',
            "Physical header owner disagrees with its canonical filename",
        ),
        (False, None, "deadline expired"),
    ],
)
def test_inventory_resolves_the_oldest_lookup_before_reporting_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    header: str | None,
    diagnostic: str,
    *,
    replacement: bool,
) -> None:
    """A controlled clock expires while the oldest pathname probe is in flight."""
    path = _file(tmp_path)
    if header is not None:
        path.write_text(header)
    expired = Event()
    started = Event()
    release = Event()
    original_sample = closed_work_request._sample  # noqa: SLF001
    monkeypatch.setattr(
        closed_work_request, "time", SimpleNamespace(monotonic=lambda: float(expired.is_set()))
    )

    def sample(path: Path) -> tuple[int, int] | None:
        started.set()
        _wait(release)
        return original_sample(path)

    monkeypatch.setattr(closed_work_request, "_sample", sample)
    before = _fds()
    with ThreadPoolExecutor(max_workers=1) as driver:
        budget = CollectionBudget(collection_limits(), 1.0)
        observed = driver.submit(observe_inventory, tmp_path, {}, frozenset(), budget)
        try:
            _wait(started)
            expired.set()
            if replacement:
                path.rename(path.with_name("former"))
                path.touch()
        finally:
            release.set()
        with pytest.raises(PreparationRefused, match=diagnostic):
            observed.result(_WAIT)
    if _fds() != before:
        pytest.fail("expired inventory leaked descriptors")


def test_inventory_closes_new_and_pending_leaves_after_submission_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing a submission releases that leaf and settles the older lookup."""
    _ordered_files(tmp_path, monkeypatch, 2)
    failed_submit = Event()
    completed = Event()
    original_sample = closed_work_request._sample  # noqa: SLF001

    def sample(path: Path) -> tuple[int, int] | None:
        _wait(failed_submit)
        result = original_sample(path)
        completed.set()
        return result

    def reject_second(index: int) -> None:
        if index == _SECOND_SUBMISSION:
            failed_submit.set()
            message = "cannot submit pathname check"
            raise RuntimeError(message)

    _track_pool(monkeypatch, lambda _index, _future: None, before_submit=reject_second)
    monkeypatch.setattr(closed_work_request, "_sample", sample)
    before = _fds()
    with pytest.raises(RuntimeError, match="cannot submit pathname check"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if not completed.is_set() or _fds() != before:
        pytest.fail("submission failure left an older lookup or a leaf open")


def test_inventory_never_checks_a_path_if_submission_is_interrupted_after_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker may start before submit returns, while admission is still pending."""
    _file(tmp_path)
    worker_started = Event()
    worker_finished = Event()
    probe_used = Event()

    def create(max_workers: int) -> ThreadPoolExecutor:
        pool = ThreadPoolExecutor(max_workers=max_workers)
        submit = pool.submit

        def interrupted(
            function: Callable[..., None], /, *args: object, **kwargs: object
        ) -> Future[None]:
            def launched() -> None:
                worker_started.set()
                try:
                    function(*args, **kwargs)
                finally:
                    worker_finished.set()

            submit(launched)
            _wait(worker_started)
            raise KeyboardInterrupt

        monkeypatch.setattr(pool, "submit", interrupted)
        return pool

    def sample(_path: Path) -> None:
        probe_used.set()

    monkeypatch.setattr(physical_inventory, "ThreadPoolExecutor", create)
    monkeypatch.setattr(closed_work_request, "_sample", sample)
    before = _fds()
    with pytest.raises(KeyboardInterrupt):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if not worker_finished.is_set() or probe_used.is_set() or _fds() != before:
        pytest.fail("an interrupted submission performed a lookup or retained resources")


def test_inventory_closes_reopened_leaf_if_type_stat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed fstat on the freshly reopened leaf propagates and releases it."""
    _file(tmp_path)
    original_fstat = os.fstat
    original_sample = closed_work_request._sample  # noqa: SLF001
    state = local()
    failed = Event()

    def reopen(path: Path) -> tuple[int, int] | None:
        state.reopening = True
        try:
            return original_sample(path)
        finally:
            state.reopening = False

    def refuse_stat(fd: int) -> os.stat_result:
        if getattr(state, "reopening", False) and not failed.is_set():
            failed.set()
            message = "pathname leaf stat failed"
            raise OSError(errno.EIO, message)
        return original_fstat(fd)

    monkeypatch.setattr(closed_work_request, "_sample", reopen)
    monkeypatch.setattr(os, "fstat", refuse_stat)
    before = _fds()
    with pytest.raises(PreparationRefused, match="pathname leaf stat failed"):
        observe_inventory(tmp_path, {}, frozenset(), _budget())
    if not failed.is_set() or _fds() != before:
        pytest.fail("reopened leaf stat did not fail or inventory leaked descriptors")


@pytest.mark.parametrize("interrupt_shutdown", [False, True])
def test_inventory_joins_pending_checks_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, interrupt_shutdown: bool
) -> None:
    """A producer or shutdown interruption cannot abandon a running pathname probe."""
    started = Event()
    release = Event()
    completed = Event()
    joining = Event()

    def interrupt() -> None:
        _wait(started)
        raise KeyboardInterrupt

    def start_join(attempt: int) -> None:
        if interrupt_shutdown and attempt == 1:
            raise KeyboardInterrupt
        joining.set()

    first, second = _ordered_files(tmp_path, monkeypatch, 2, after_first=interrupt)
    original_sample = closed_work_request._sample  # noqa: SLF001

    def sample(path: Path) -> tuple[int, int] | None:
        started.set()
        _wait(release)
        result = original_sample(path)
        completed.set()
        return result

    _track_shutdown(monkeypatch, start_join)
    monkeypatch.setattr(closed_work_request, "_sample", sample)
    before = _fds()
    with ThreadPoolExecutor(max_workers=1) as driver:
        observed = driver.submit(observe_inventory, tmp_path, {}, frozenset(), _budget())
        try:
            _wait(joining)
            if observed.done() or _open_histories((first, second)) != [first]:
                pytest.fail("interrupt abandoned a running check or lost its pathname pin")
        finally:
            release.set()
        with pytest.raises(KeyboardInterrupt):
            observed.result(_WAIT)
    if not completed.is_set() or _fds() != before:
        pytest.fail("interrupted inventory left a check or descriptors active")


def test_inventory_preserves_the_pathname_refusal_if_shutdown_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shutdown interrupt cannot mask an already determined earlier refusal."""
    first, second = _ordered_files(tmp_path, monkeypatch, 2)
    later_started = Event()
    release = Event()
    joining = Event()

    def sample(path: Path) -> None:
        if path == first:
            _wait(later_started)
            return
        later_started.set()
        _wait(release)

    def start_join(attempt: int) -> None:
        if attempt == 1:
            raise KeyboardInterrupt
        joining.set()

    _track_shutdown(monkeypatch, start_join)
    monkeypatch.setattr(closed_work_request, "_sample", sample)
    before = _fds()
    with ThreadPoolExecutor(max_workers=1) as driver:
        observed = driver.submit(observe_inventory, tmp_path, {}, frozenset(), _budget())
        try:
            _wait(joining)
            if observed.done() or _open_histories((second,)) != [second]:
                pytest.fail("interrupted shutdown lost the later lookup's pathname pin")
        finally:
            release.set()
        with pytest.raises(PreparationRefused, match="changed during header capture"):
            observed.result(_WAIT)
    if _fds() != before:
        pytest.fail("interrupted shutdown leaked descriptors")
