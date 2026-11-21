# Copyright (c) 2026

"""Best-effort interactive timing display for a single CLI invocation."""

from __future__ import annotations

import os
import stat
import sys
from contextlib import contextmanager, suppress
from threading import Event, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from codex_session_gc.execution.timing import TimingObserver


def _diagnostic_fd(*, tty_only: bool) -> int | None:
    candidate = None
    try:
        if tty_only and not sys.stderr.isatty():
            return None
        original = os.fstat(2)
        is_pipe = stat.S_ISFIFO(original.st_mode)
        if not os.isatty(2) and (tty_only or not is_pipe):
            return None
        candidate = os.open("/proc/self/fd/2", os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        reopened = os.fstat(candidate)
        if (reopened.st_dev, reopened.st_ino) == (original.st_dev, original.st_ino):
            fd = candidate
            candidate = None
            return fd
    except OSError, ValueError:
        return None
    finally:
        if candidate is not None:
            with suppress(OSError):
                os.close(candidate)
    return None


def interrupted(summary: dict[str, object]) -> None:
    """Send the partial timing record only when stderr supports nonblocking delivery."""
    fd = _diagnostic_fd(tty_only=False)
    if fd is not None:
        try:
            with suppress(OSError):
                os.write(fd, (terminal(summary) + "\n").encode())
        finally:
            with suppress(OSError):
                os.close(fd)


@contextmanager
def live(observer: TimingObserver) -> Iterator[None]:
    """Poll a coalesced snapshot through a separate nonblocking terminal descriptor."""
    fd = _diagnostic_fd(tty_only=True)
    stop = Event()

    def consume() -> None:
        previous = None
        while not stop.wait(0.1):
            snapshot = observer.snapshot()
            stage = snapshot["current_stage"]
            if stage == previous:
                continue
            previous = stage
            frame = f"timing: {stage or 'waiting'}; {snapshot['caller_elapsed_ns']} ns\n".encode()
            try:
                if fd is None or os.write(fd, frame) != len(frame):
                    return
            except OSError:
                return

    thread = Thread(target=consume, daemon=True) if fd is not None else None
    try:
        if thread is not None:
            try:
                thread.start()
            except RuntimeError:
                thread = None
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join()
        if fd is not None:
            with suppress(OSError):
                os.close(fd)


def terminal(summary: dict[str, object]) -> str:
    """Render observed intervals without inferring unmeasured native phases."""
    elapsed = summary["caller_elapsed_ns"]
    completion = summary["counts_completion"]
    lines = [f"timings: {elapsed} ns caller elapsed ({completion})"]
    if summary.get("worker_elapsed_ns") is not None:
        lines.append(f"  worker elapsed: {summary['worker_elapsed_ns']} ns")
    phases = summary.get("phases")
    if (
        summary.get("worker_elapsed_ns") is None
        and summary.get("scope") == "preview"
        and isinstance(phases, list)
        and any(phase.get("name") == "preview_worker_wait" for phase in phases)
    ):
        lines.append("  worker observations: unavailable")
    worker_counts = summary.get("worker_counts")
    if isinstance(worker_counts, dict):
        completion = summary.get("worker_counts_completion")
        if completion is not None:
            lines.append(f"  worker counts ({completion}): {worker_counts}")
    phases = summary["phases"]
    if not isinstance(phases, list):
        return "\n".join(lines)
    lines.extend(
        f"  {phase['name']} ({phase['clock_domain']}): {phase['elapsed_ns']} ns "
        f"({phase['calls']} calls, {phase['completion']})"
        for phase in phases
    )
    counts = summary["counts"]
    if isinstance(counts, dict):
        lines.extend(f"  {name}: {value}" for name, value in counts.items())
    return "\n".join(lines)
