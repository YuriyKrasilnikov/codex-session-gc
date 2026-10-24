# Copyright (c) 2026
# ruff: noqa: D103, S101, S603

"""Behavioral tests for the parent-gated owned launcher."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from importlib import import_module
from pathlib import Path

import pytest

from codex_session_gc.execution import process_identity
from codex_session_gc.execution.process_identity import (
    OWNED_MARKER_ENV,
    Presence,
    child_presence,
    observe_owned_child,
)


def _launcher() -> Path:
    owned_launch = import_module("codex_session_gc.execution.owned_launch")

    return Path(owned_launch.__file__).resolve()


def _child(target: str, environment: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-I", str(_launcher()), sys.executable, "-c", target],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )


def test_grant_runs_target_and_preserves_the_first_request_bytes(tmp_path: Path) -> None:
    observed = tmp_path / "observed"
    environment = {
        "PATH": os.environ["PATH"],
        OWNED_MARKER_ENV: "marker-a",
        "OBSERVED": str(observed),
    }
    target = "import os,sys; open(os.environ['OBSERVED'],'wb').write(sys.stdin.buffer.readline())"
    child = _child(target, environment)
    assert child.stdin is not None
    child.stdin.write(b"codex-session-gc:grant-v1\n" + b'{"id":1}\n')
    child.stdin.close()
    assert child.wait(timeout=2) == 0
    assert observed.read_bytes() == b'{"id":1}\n'


def test_eof_and_invalid_grant_never_run_target(tmp_path: Path) -> None:
    for name, input_bytes in (("eof", b""), ("invalid", b"wrong-grant\n")):
        observed = tmp_path / name
        environment = {
            "PATH": os.environ["PATH"],
            OWNED_MARKER_ENV: name,
            "OBSERVED": str(observed),
        }
        child = _child("import os; open(os.environ['OBSERVED'],'wb').write(b'ran')", environment)
        assert child.stdin is not None
        if input_bytes:
            child.stdin.write(input_bytes)
        child.stdin.close()
        assert child.wait(timeout=2) != 0
        assert not observed.exists()


def test_grant_timeout_never_runs_target(tmp_path: Path) -> None:
    """An abandoned parent leaves its blocked child unable to start the native command."""
    observed = tmp_path / "timeout"
    environment = {
        "PATH": os.environ["PATH"],
        OWNED_MARKER_ENV: "timeout-marker",
        "OBSERVED": str(observed),
    }
    child = _child("import os; open(os.environ['OBSERVED'],'wb').write(b'ran')", environment)
    assert child.wait(timeout=6) != 0
    assert not observed.exists()


def test_owned_identity_is_alive_then_absent() -> None:
    environment = {"PATH": os.environ["PATH"], OWNED_MARKER_ENV: "identity-marker"}
    child = _child("import time; time.sleep(2)", environment)
    identity = observe_owned_child(child.pid, environment)
    assert identity is not None
    assert child_presence(identity) is Presence.ALIVE
    assert child.stdin is not None
    child.stdin.close()
    assert child.wait(timeout=2) != 0
    assert child_presence(identity) is Presence.ABSENT


@pytest.mark.parametrize("field", ["pid_namespace", "mount_namespace", "proc_mount"])
def test_same_child_identity_drift_is_ambiguous(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coordinate change cannot turn the same live child into an absence."""
    environment = {"PATH": os.environ["PATH"], OWNED_MARKER_ENV: "drift-marker"}
    child = _child("import time; time.sleep(2)", environment)
    identity = observe_owned_child(child.pid, environment)
    assert identity is not None
    changed = replace(identity, **{field: "changed"})
    monkeypatch.setattr(process_identity, "_read_identity", lambda *_: changed)
    assert child_presence(identity) is Presence.AMBIGUOUS
    assert child.stdin is not None
    child.stdin.close()
    child.wait(timeout=2)


def test_readable_replacement_in_the_same_coordinate_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different start tick is a positive replacement observation in one coordinate."""
    environment = {"PATH": os.environ["PATH"], OWNED_MARKER_ENV: "replacement-marker"}
    child = _child("import time; time.sleep(2)", environment)
    identity = observe_owned_child(child.pid, environment)
    assert identity is not None
    monkeypatch.setattr(
        process_identity,
        "_read_identity",
        lambda *_: replace(identity, start_ticks=identity.start_ticks + 1),
    )
    assert child_presence(identity) is Presence.ABSENT
    assert child.stdin is not None
    child.stdin.close()
    child.wait(timeout=2)


def test_missing_pid_during_a_changed_observer_view_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished PID is useful absence evidence only when both observer reads agree."""
    environment = {"PATH": os.environ["PATH"], OWNED_MARKER_ENV: "missing-view-marker"}
    child = _child("import time; time.sleep(2)", environment)
    identity = observe_owned_child(child.pid, environment)
    assert identity is not None
    assert child.stdin is not None
    child.stdin.close()
    child.wait(timeout=2)
    coordinates = iter(
        [
            (identity.boot_id, identity.proc_mount),
            (identity.boot_id, f"{identity.proc_mount}-changed"),
        ]
    )
    monkeypatch.setattr(process_identity, "_observer_coordinate", lambda: next(coordinates))
    assert child_presence(identity) is Presence.AMBIGUOUS


def test_restored_endpoints_do_not_hide_an_intermediate_coordinate_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A readable identity carries the observer coordinate used to read it."""
    environment = {"PATH": os.environ["PATH"], OWNED_MARKER_ENV: "intermediate-view-marker"}
    child = _child("import time; time.sleep(2)", environment)
    identity = observe_owned_child(child.pid, environment)
    assert identity is not None
    monkeypatch.setattr(
        process_identity,
        "_read_identity",
        lambda *_: replace(identity, proc_mount=f"{identity.proc_mount}-intermediate"),
    )
    monkeypatch.setattr(
        process_identity,
        "_observer_coordinate",
        lambda: (identity.boot_id, identity.proc_mount),
    )
    assert child_presence(identity) is Presence.AMBIGUOUS
    assert child.stdin is not None
    child.stdin.close()
    child.wait(timeout=2)
