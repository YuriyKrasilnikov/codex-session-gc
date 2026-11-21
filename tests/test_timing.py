# Copyright (c) 2026
# ruff: noqa: D103, S101, PLR2004, PT011

"""Independent monotonic observations preserve nesting and interruption."""

import os
import threading

import pytest

from codex_session_gc.cli import timing_display
from codex_session_gc.execution.timing import Phase, TimingObserver, interval, observe


def test_nested_and_interrupted_intervals() -> None:
    values = iter((0, 10, 20, 30, 40, 50, 60, 70))
    observer = TimingObserver("preview", clock=lambda: next(values))
    with observe(observer), interval(Phase.SELECTION_MANIFEST):
        with interval(Phase.CONFIRMATION):
            pass
        with pytest.raises(ValueError), interval(Phase.SQL_PROJECTION):
            raise ValueError
    result = observer.snapshot()
    phases = {phase["name"]: phase for phase in result["phases"]}
    assert phases["selection_manifest"]["elapsed_ns"] == 50
    assert phases["confirmation"]["elapsed_ns"] == 10
    assert phases["sql_projection"]["completion"] == "interrupted"
    assert result["caller_elapsed_ns"] == 70


def test_undrained_terminal_does_not_block_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full nonblocking sink drops the frame and the consumer exits."""
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    try:
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        monkeypatch.setattr(timing_display.sys.stderr, "isatty", lambda: True)
        monkeypatch.setattr(timing_display.os, "open", lambda *_args: os.dup(write_fd))
        monkeypatch.setattr(timing_display.os, "isatty", lambda _fd: True)
        monkeypatch.setattr(timing_display.os, "fstat", lambda _fd: os.stat_result((0,) * 10))
        observer = TimingObserver("preview")
        finished = threading.Event()
        attempted = threading.Event()
        write = os.write

        def observed_write(fd: int, data: bytes) -> int:
            attempted.set()
            return write(fd, data)

        monkeypatch.setattr(timing_display.os, "write", observed_write)

        def run() -> None:
            with timing_display.live(observer), observer.phase(Phase.SQL_PROJECTION):
                attempted.wait(2)
            finished.set()

        worker = threading.Thread(target=run)
        worker.start()
        worker.join(3)
        assert attempted.is_set()
        assert finished.is_set()
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_failed_display_thread_start_keeps_command_and_closes_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A diagnostic reader that cannot start does not own the operation."""
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    candidate = os.dup(write_fd)
    stat = os.fstat
    try:
        monkeypatch.setattr(timing_display.sys.stderr, "isatty", lambda: True)
        monkeypatch.setattr(timing_display.os, "open", lambda *_args: candidate)
        monkeypatch.setattr(timing_display.os, "isatty", lambda _fd: True)
        monkeypatch.setattr(timing_display.os, "fstat", lambda _fd: stat(write_fd))

        def refuse_start(_thread: threading.Thread) -> None:
            raise RuntimeError

        monkeypatch.setattr(timing_display.Thread, "start", refuse_start)
        observer = TimingObserver("apply")
        with timing_display.live(observer), observer.phase(Phase.DISPATCH_ROUND_TRIP):
            pass
        snapshot = observer.snapshot()
        assert snapshot["phases"][0]["name"] == "dispatch_round_trip"
        with pytest.raises(OSError):
            stat(candidate)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_interruption_diagnostic_with_closed_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed diagnostic sink cannot replace the primary interruption."""
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK)
    os.close(read_fd)
    monkeypatch.setattr(timing_display, "_diagnostic_fd", lambda **_kwargs: write_fd)
    observer = TimingObserver("preview")
    observer.finish(interrupted=True)
    timing_display.interrupted(observer.snapshot())
    with pytest.raises(OSError):
        os.fstat(write_fd)
