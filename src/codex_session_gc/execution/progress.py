# Copyright (c) 2026

"""Volatile one-way observations of existing Python execution boundaries."""

from dataclasses import dataclass
from enum import StrEnum
from queue import Empty, SimpleQueue
from typing import final

from codex_session_gc.domain import ThreadId
from codex_session_gc.recovery import AttemptId, TerminalKind


class ProgressKind(StrEnum):
    """Facts known by the Python composition, independent of native internal progress."""

    OPERATION = "operation"
    ROOT_STARTED = "root_started"
    ROOT_RESULT = "root_result"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One operation identity or returned root result; no deletion-completion certificate."""

    kind: ProgressKind
    operation_id: AttemptId
    root: ThreadId | None = None
    outcome: TerminalKind | None = None

    def __post_init__(self) -> None:
        """Keep operation identity and returned-result facts distinct."""
        if type(self.kind) is not ProgressKind or type(self.operation_id) is not AttemptId:
            message = "Progress requires a typed operation identity and event kind."
            raise ValueError(message)
        root_event = self.kind in {ProgressKind.ROOT_STARTED, ProgressKind.ROOT_RESULT}
        if root_event != (self.root is not None) or (
            self.root is not None and type(self.root) is not ThreadId
        ):
            message = "Only root boundary events carry a thread identity."
            raise ValueError(message)
        if self.outcome is not None and (
            self.kind is not ProgressKind.ROOT_RESULT or type(self.outcome) is not TerminalKind
        ):
            message = "Only a returned root result carries its native outcome."
            raise ValueError(message)


@final
class ProgressChannel:
    """A local FIFO whose producer never waits for or invokes the observer."""

    __slots__ = ("_events",)

    def __init__(self) -> None:
        """Own only volatile events, with no thread, receipt or execution command."""
        self._events: SimpleQueue[ProgressEvent] = SimpleQueue()

    def publish(self, event: ProgressEvent) -> None:
        """Append a finite boundary event without entering consumer code."""
        self._events.put_nowait(event)

    def drain(self) -> tuple[ProgressEvent, ...]:
        """Take currently available events without waiting for execution."""
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except Empty:
                return tuple(events)


def checked_channel(value: object) -> ProgressChannel | None:
    """Reject arbitrary callback objects before any execution effect is admitted."""
    if value is not None and type(value) is not ProgressChannel:
        message = "Progress requires the concrete local observation channel."
        raise ValueError(message)
    return value
