# Copyright (c) 2026
# ruff: noqa: D107, EM101, TRY003

"""Conservative identities for children directly started by this process."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, fields
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Final

from codex_session_gc.execution.transport import ChildCommand
from codex_session_gc.recovery import (
    Payload,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


OWNED_MARKER_ENV: Final = "CODEX_SESSION_GC_OWNED_MARKER"
_PROC: Final = Path("/proc")


class Presence(StrEnum):
    """The result of a bounded observation of one stored child identity."""

    ALIVE = "alive"
    ABSENT = "absent"
    AMBIGUOUS = "ambiguous"


class IdentityStage(StrEnum):
    """The identity coordinate being observed when qualification failed."""

    INPUT = "input"
    START_BEFORE = "start_ticks_before"
    ENVIRONMENT = "environment"
    UID = "uid"
    BOOT = "boot_id"
    PID_NAMESPACE = "pid_namespace"
    MOUNT_NAMESPACE = "mount_namespace"
    PROC_MOUNT = "proc_mount"
    START_AFTER = "start_ticks_after"
    OWNERSHIP = "ownership"
    RECHECK = "recheck"


class IdentityFailure(StrEnum):
    """Why the observed identity cannot authorize a grant."""

    INVALID = "invalid"
    IO = "io"
    MALFORMED = "malformed"
    MISMATCH = "mismatch"
    UNSTABLE = "unstable"


class IdentityRead(StrEnum):
    """The acquisition context of one identity observation."""

    INITIAL = "initial"
    CONFIRMATION = "confirmation"
    PRESENCE = "presence"


@dataclass(frozen=True, slots=True)
class IdentityDiagnostic:
    """Failure coordinates without environment contents or observed secret values."""

    stage: IdentityStage
    reason: IdentityFailure
    reading: IdentityRead
    error_number: int | None = None
    changed_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Encode only failure labels, an OS error number and identity field names."""
        return {
            "stage": self.stage.value,
            "reason": self.reason.value,
            "reading": self.reading.value,
            "errno": self.error_number,
            "changed_fields": list(self.changed_fields),
        }


class IdentityObservationError(Exception):
    """Refusal to authorize a child from incomplete or changing identity evidence."""

    def __init__(self, diagnostic: IdentityDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(
            f"Owned identity refused: {diagnostic.reading.value}/{diagnostic.stage.value}; "
            f"reason={diagnostic.reason.value}; errno={diagnostic.error_number}; "
            f"fields={','.join(diagnostic.changed_fields)}"
        )


class GatedLaunchError(Exception):
    """Raised when a blocked child cannot be durably admitted to native execution."""

    def __init__(
        self,
        message: str,
        *,
        primary: Exception | None = None,
        secondary: tuple[Exception, ...] = (),
    ) -> None:
        super().__init__(message)
        self.primary = primary
        self.secondary = secondary


@dataclass(frozen=True, slots=True)
class ChildIdentity:
    """The exact Linux incarnation observed while an owned child is blocked."""

    pid: int
    marker: str
    start_ticks: int
    boot_id: str
    pid_namespace: str
    mount_namespace: str
    uid: int
    proc_mount: str

    def incarnation(self) -> Payload:
        """Encode this identity in a stable form suitable for the receipt."""
        encoded = json.dumps(
            {
                "boot_id": self.boot_id,
                "marker": self.marker,
                "mount_namespace": self.mount_namespace,
                "pid": self.pid,
                "pid_namespace": self.pid_namespace,
                "proc_mount": self.proc_mount,
                "start_ticks": self.start_ticks,
                "uid": self.uid,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return Payload("application/codex-session-gc.child-identity+json", 1, encoded)


def observe_owned_child(pid: int, environment: Mapping[str, str]) -> ChildIdentity:
    """Require two matching owned-child readings or raise a value-free diagnostic."""
    marker = environment.get(OWNED_MARKER_ENV)
    if type(pid) is not int or pid < 1 or type(marker) is not str or not marker:
        raise IdentityObservationError(
            IdentityDiagnostic(IdentityStage.INPUT, IdentityFailure.INVALID, IdentityRead.INITIAL)
        )
    first = _read_identity(pid, marker, reading=IdentityRead.INITIAL)
    second = _read_identity(pid, marker, reading=IdentityRead.CONFIRMATION)
    if first != second:
        changed = tuple(
            field.name
            for field in fields(ChildIdentity)
            if getattr(first, field.name) != getattr(second, field.name)
        )
        raise IdentityObservationError(
            IdentityDiagnostic(
                IdentityStage.RECHECK,
                IdentityFailure.UNSTABLE,
                IdentityRead.CONFIRMATION,
                changed_fields=changed,
            )
        )
    return first


def child_presence(identity: ChildIdentity) -> Presence:  # noqa: C901, PLR0911
    """Classify only the stored PID in its recorded visible procfs coordinate."""
    try:
        before = _observer_coordinate()
    except OSError:
        return Presence.AMBIGUOUS
    if before != _stored_coordinate(identity):
        return Presence.AMBIGUOUS
    path = _PROC / str(identity.pid)
    try:
        path.stat()
    except FileNotFoundError:
        return _absence_after_coordinate_check(identity)
    except OSError:
        return Presence.AMBIGUOUS
    try:
        observed = _read_identity(identity.pid, identity.marker)
    except IdentityObservationError:
        return Presence.AMBIGUOUS
    try:
        after = _observer_coordinate()
    except OSError:
        return Presence.AMBIGUOUS
    if after != before or after != _stored_coordinate(identity):
        return Presence.AMBIGUOUS
    if _stored_coordinate(observed) != _stored_coordinate(identity):
        return Presence.AMBIGUOUS
    if observed.start_ticks != identity.start_ticks:
        return Presence.ABSENT
    if observed == identity:
        return Presence.ALIVE
    # The same process can change its namespace or the observer coordinate.
    return Presence.AMBIGUOUS


def owned_launcher_command(command: ChildCommand) -> ChildCommand:
    """Wrap one native command in the installed resource that waits for a grant."""
    launcher = Path(str(files("codex_session_gc.execution").joinpath("owned_launch.py"))).resolve()
    if not launcher.is_file():
        raise GatedLaunchError("The installed launcher resource is unavailable.")
    if not command.environment.get(OWNED_MARKER_ENV):
        raise GatedLaunchError("The complete child environment lacks an owned marker.")
    return ChildCommand(
        Path(sys.executable).resolve(),
        ("-I", str(launcher), str(command.executable), *command.arguments),
        command.environment,
        command.inherited_descriptors,
    )


def _read_identity(
    pid: int, expected_marker: str, *, reading: IdentityRead = IdentityRead.PRESENCE
) -> ChildIdentity:
    path = _PROC / str(pid)
    stage = IdentityStage.START_BEFORE
    try:
        stat_before = _start_ticks(path / "stat")
        stage = IdentityStage.ENVIRONMENT
        marker = _environment_marker(path / "environ")
        stage = IdentityStage.UID
        uid = _uid(path / "status")
        stage = IdentityStage.BOOT
        boot = _boot_id()
        stage = IdentityStage.PID_NAMESPACE
        pid_namespace = os.fsdecode((path / "ns/pid").readlink())
        stage = IdentityStage.MOUNT_NAMESPACE
        mount_namespace = os.fsdecode((path / "ns/mnt").readlink())
        stage = IdentityStage.PROC_MOUNT
        proc_mount = _proc_coordinate()
        identity = ChildIdentity(
            pid, marker, stat_before, boot, pid_namespace, mount_namespace, uid, proc_mount
        )
        stage = IdentityStage.START_AFTER
        stat_after = _start_ticks(path / "stat")
    except OSError as error:
        raise IdentityObservationError(
            IdentityDiagnostic(stage, IdentityFailure.IO, reading, error_number=error.errno)
        ) from error
    except (IndexError, ValueError) as error:
        raise IdentityObservationError(
            IdentityDiagnostic(stage, IdentityFailure.MALFORMED, reading)
        ) from error
    mismatch = tuple(
        name
        for name, matches in (("marker", marker == expected_marker), ("uid", uid == os.getuid()))
        if not matches
    )
    if mismatch:
        raise IdentityObservationError(
            IdentityDiagnostic(
                IdentityStage.OWNERSHIP, IdentityFailure.MISMATCH, reading, changed_fields=mismatch
            )
        )
    if stat_before != stat_after:
        raise IdentityObservationError(
            IdentityDiagnostic(
                IdentityStage.START_AFTER,
                IdentityFailure.UNSTABLE,
                reading,
                changed_fields=("start_ticks",),
            )
        )
    return identity


def _environment_marker(path: Path) -> str:
    for entry in path.read_bytes().split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key.decode() == OWNED_MARKER_ENV:
            return value.decode()
    raise ValueError("The owned marker is absent.")


def _start_ticks(path: Path) -> int:
    # The second field is parenthesized and can include spaces; field 22 follows it.
    fields = path.read_text().rpartition(")")[2].split()
    return int(fields[19])


def _uid(path: Path) -> int:
    for line in path.read_text().splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise ValueError("The process uid is absent.")


def _boot_id() -> str:
    return (_PROC / "sys/kernel/random/boot_id").read_text().strip()


def _stored_coordinate(identity: ChildIdentity) -> tuple[str, str]:
    return identity.boot_id, identity.proc_mount


def _absence_after_coordinate_check(identity: ChildIdentity) -> Presence:
    try:
        after = _observer_coordinate()
    except OSError:
        return Presence.AMBIGUOUS
    return Presence.ABSENT if after == _stored_coordinate(identity) else Presence.AMBIGUOUS


def _observer_coordinate() -> tuple[str, str]:
    return _boot_id(), _proc_coordinate()


def _proc_coordinate() -> str:
    namespace = os.fsdecode(Path("/proc/self/ns/mnt").readlink())
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[4] == "/proc" and " - proc " in line:  # noqa: PLR2004
            return f"{namespace}:{line}"
    raise OSError("The procfs mount coordinate is unavailable.")
