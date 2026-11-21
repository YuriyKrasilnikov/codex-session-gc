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


def test_worker_timing_is_separate_from_business_report(tmp_path: object) -> None:
    """A private worker result adds clock-domain observations without changing preview."""
    import asyncio  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from codex_session_gc.cli.console_tasks import (  # noqa: PLC0415
        Intent,
        async_preview,
        parse_cutoff,
    )
    from codex_session_gc.domain import ThreadId  # noqa: PLC0415
    from tests.current_storage_fixture import _GRANDCHILD, _MAIN, _native_home  # noqa: PLC0415

    assert isinstance(tmp_path, Path)
    _native_home(tmp_path)
    intent = Intent(
        tmp_path,
        parse_cutoff("2030-01-01"),
        (ThreadId.parse(_MAIN),),
        (ThreadId.parse(_GRANDCHILD),),
    )

    async def run() -> None:
        plain = await async_preview(intent)
        observer = TimingObserver("preview")
        with observe(observer):
            timed = await async_preview(intent)
        assert timed == plain
        summary = observer.snapshot()
        assert summary["worker_elapsed_ns"] is not None
        assert any(phase["clock_domain"] == "worker" for phase in summary["phases"])
        assert any(phase["name"] == "preview_worker_wait" for phase in summary["phases"])

    asyncio.run(run())


def test_invalid_nested_worker_timing_cannot_suppress_business_result(tmp_path: object) -> None:
    """A valid worker report survives an excessively nested diagnostic payload."""
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from codex_session_gc.cli.console_tasks import (  # noqa: PLC0415
        _preview_result,
        _read_worker_timing,
    )

    assert isinstance(tmp_path, Path)
    (tmp_path / "result.json").write_text(json.dumps({"result": "ok"}))
    (tmp_path / "timings.json").write_text("[" * 1500 + "]" * 1500)
    result = _preview_result(tmp_path)
    observer = TimingObserver("preview")
    _read_worker_timing(tmp_path, observer)
    assert result == {"result": "ok"}
    assert observer.snapshot()["worker_elapsed_ns"] is None


def test_executor_submission_failure_closes_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed submission leaves no active interval and preserves the error."""
    import asyncio  # noqa: PLC0415

    from codex_session_gc.execution.timing import measured_to_thread  # noqa: PLC0415

    async def fail(*_args: object, **_kwargs: object) -> object:
        message = "submission failed"
        raise RuntimeError(message)

    async def run() -> None:
        observer = TimingObserver("apply")
        monkeypatch.setattr(asyncio, "to_thread", fail)
        with observe(observer), pytest.raises(RuntimeError, match="submission failed"):
            await measured_to_thread(lambda: None)
        result = observer.snapshot()
        assert result["current_stage"] is None
        assert [phase["calls"] for phase in result["phases"]] == [1]
        assert result["phases"][0]["name"] == "executor_wait"
        assert result["phases"][0]["completion"] == "interrupted"

    asyncio.run(run())


def test_worker_summary_displays_counts_and_unavailable_state() -> None:
    """The operator sees real worker counts or an explicit absence of data."""
    summary = {
        "scope": "preview",
        "caller_elapsed_ns": 8,
        "counts": {},
        "counts_completion": "complete",
        "worker_elapsed_ns": 4,
        "worker_counts": {"headers_read": 3},
        "worker_counts_completion": "interrupted",
        "phases": [],
    }
    rendered = timing_display.terminal(summary)
    assert "worker elapsed: 4 ns" in rendered
    assert "worker counts (interrupted)" in rendered
    assert "headers_read" in rendered
    summary.update(worker_elapsed_ns=None, worker_counts_completion=None)
    summary["phases"] = [
        {
            "name": "preview_worker_wait",
            "clock_domain": "caller",
            "elapsed_ns": 2,
            "calls": 1,
            "completion": "complete",
        }
    ]
    assert "worker observations: unavailable" in timing_display.terminal(summary)


def test_preview_preserves_result_when_worker_timing_is_invalid(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed optional worker output cannot change a completed preview."""
    import asyncio  # noqa: PLC0415
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from codex_session_gc.cli import console_tasks  # noqa: PLC0415
    from codex_session_gc.domain import ThreadId  # noqa: PLC0415

    assert isinstance(tmp_path, Path)
    identifier = ThreadId.parse("11111111-1111-4111-8111-111111111111")
    intent = console_tasks.Intent(
        tmp_path, console_tasks.parse_cutoff("2030-01-01"), (identifier,), (identifier,)
    )

    class Exited:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def spawn(*args: object, **_kwargs: object) -> Exited:
        directory = Path(str(args[-1]))
        (directory / "result.json").write_text(json.dumps({"business": "ok"}))
        (directory / "timings.json").write_text("[" * 1500 + "]" * 1500)
        return Exited()

    async def run() -> None:
        monkeypatch.setattr(console_tasks.asyncio, "create_subprocess_exec", spawn)
        observer = TimingObserver("preview")
        with observe(observer):
            result = await console_tasks.async_preview(intent)
        assert result == {"business": "ok"}
        assert observer.snapshot()["worker_elapsed_ns"] is None

    asyncio.run(run())


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
