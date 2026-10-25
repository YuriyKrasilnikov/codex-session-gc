# Copyright (c) 2026
# ruff: noqa: D103, S101, S603

"""Behavioral tests for the parent-gated owned launcher."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.execution import process_identity
from codex_session_gc.execution.process_identity import (
    OWNED_MARKER_ENV,
    Presence,
    child_presence,
    observe_owned_child,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_CLEANUP_TIMEOUT_SECONDS = 2
_STDERR_DIAGNOSTIC_BYTES = 4096


def _launcher() -> Path:
    owned_launch = import_module("codex_session_gc.execution.owned_launch")

    return Path(owned_launch.__file__).resolve()


@contextmanager
def _child(target: str, environment: dict[str, str]) -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen(
        [sys.executable, "-I", str(_launcher()), sys.executable, "-c", target],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    failure: BaseException | None = None
    try:
        yield child
    except BaseException as error:
        failure = error
        raise
    finally:
        before_cleanup = _child_observation(child)
        cleanup_errors: list[Exception] = []
        try:
            _reap_child(child)
        except (OSError, subprocess.SubprocessError) as error:
            cleanup_errors.append(error)
        stderr = _bounded_stderr(child)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError as error:
                    cleanup_errors.append(error)
        note = (
            f"Owned child before cleanup: {before_cleanup}; "
            f"after cleanup: returncode={child.returncode}; stderr={stderr}"
        )
        if failure is not None:
            failure.add_note(note)
        if cleanup_errors:
            if failure is not None:
                for cleanup_error in cleanup_errors:
                    failure.add_note(f"Owned child cleanup failed: {cleanup_error!r}")
            else:
                failures = ExceptionGroup("Owned child cleanup failed", cleanup_errors)
                failures.add_note(note)
                raise failures


def _child_observation(child: subprocess.Popen[bytes]) -> str:
    returncode = child.poll()
    try:
        status = (Path("/proc") / str(child.pid) / "status").read_text()
    except OSError as error:
        state = f"unavailable errno={error.errno}"
    else:
        states = [line for line in status.splitlines() if line.startswith("State:")]
        state = repr(states)
    return f"pid={child.pid}; returncode={returncode}; state={state}"


def _reap_child(child: subprocess.Popen[bytes]) -> None:
    try:
        if child.stdin is not None and not child.stdin.closed:
            child.stdin.close()
    finally:
        try:
            child.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)


def _bounded_stderr(child: subprocess.Popen[bytes]) -> str:
    if child.stderr is None:
        return "unavailable: no stderr pipe"
    try:
        os.set_blocking(child.stderr.fileno(), False)
        captured = os.read(child.stderr.fileno(), _STDERR_DIAGNOSTIC_BYTES)
    except OSError as error:
        return f"unavailable errno={error.errno}"
    return f"first {_STDERR_DIAGNOSTIC_BYTES} bytes: {captured!r}"


def test_grant_runs_target_and_preserves_the_first_request_bytes(tmp_path: Path) -> None:
    observed = tmp_path / "observed"
    environment = {
        "PATH": os.environ["PATH"],
        OWNED_MARKER_ENV: "marker-a",
        "OBSERVED": str(observed),
    }
    target = "import os,sys; open(os.environ['OBSERVED'],'wb').write(sys.stdin.buffer.readline())"
    with _child(target, environment) as child:
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
        with _child(
            "import os; open(os.environ['OBSERVED'],'wb').write(b'ran')", environment
        ) as child:
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
    with _child("import os; open(os.environ['OBSERVED'],'wb').write(b'ran')", environment) as child:
        assert child.wait(timeout=6) != 0
        assert not observed.exists()


def test_owned_identity_is_alive_then_absent() -> None:
    environment = {"PATH": os.environ["PATH"], OWNED_MARKER_ENV: "identity-marker"}
    with _child("import time; time.sleep(2)", environment) as child:
        identity = observe_owned_child(child.pid, environment)
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
    with _child("import time; time.sleep(2)", environment) as child:
        identity = observe_owned_child(child.pid, environment)
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
    with _child("import time; time.sleep(2)", environment) as child:
        identity = observe_owned_child(child.pid, environment)
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
    with _child("import time; time.sleep(2)", environment) as child:
        identity = observe_owned_child(child.pid, environment)
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
    with _child("import time; time.sleep(2)", environment) as child:
        identity = observe_owned_child(child.pid, environment)
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


def test_identity_refusal_reaps_child_and_preserves_diagnostic(tmp_path: Path) -> None:
    observed = tmp_path / "refused-target"
    environment = {"PATH": os.environ["PATH"], "OBSERVED": str(observed)}
    with (
        pytest.raises(process_identity.IdentityObservationError) as raised,
        _child("import os; open(os.environ['OBSERVED'],'wb').write(b'ran')", environment) as child,
    ):
        observe_owned_child(child.pid, {OWNED_MARKER_ENV: "expected-marker-not-present-in-child"})

    assert raised.value.diagnostic.stage is process_identity.IdentityStage.ENVIRONMENT
    assert any("Owned child before cleanup:" in note for note in raised.value.__notes__)
    assert child.returncode is not None
    assert not observed.exists()
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)


def _identity_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict[str, str]]:
    pid = 543210
    path = tmp_path / str(pid)
    (path / "ns").mkdir(parents=True)
    (path / "stat").write_text(f"{pid} (fixture child) " + " ".join(["0"] * 19 + ["42"]))
    (path / "environ").write_bytes(
        f"{OWNED_MARKER_ENV}=private-marker-value\0PRIVATE_KEY=private-secret-value\0".encode()
    )
    (path / "status").write_text(f"Uid:\t{os.getuid()}\t0\t0\t0\n")
    (path / "ns/pid").symlink_to("pid:[fixture]")
    (path / "ns/mnt").symlink_to("mnt:[fixture]")
    (tmp_path / "sys/kernel/random").mkdir(parents=True)
    (tmp_path / "sys/kernel/random/boot_id").write_text("fixture-boot")
    monkeypatch.setattr(process_identity, "_PROC", tmp_path)
    monkeypatch.setattr(process_identity, "_proc_coordinate", lambda: "fixture-proc")
    return pid, {OWNED_MARKER_ENV: "private-marker-value"}


def test_unavailable_identity_preserves_stage_errno_without_environment_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, environment = _identity_proc(tmp_path, monkeypatch)
    (tmp_path / str(pid) / "environ").unlink()

    with pytest.raises(process_identity.IdentityObservationError) as raised:
        observe_owned_child(pid, environment)

    diagnostic = raised.value.diagnostic
    assert diagnostic.stage is process_identity.IdentityStage.ENVIRONMENT
    assert diagnostic.reason is process_identity.IdentityFailure.IO
    assert diagnostic.reading is process_identity.IdentityRead.INITIAL
    assert diagnostic.error_number == errno.ENOENT
    serialized = json.dumps(diagnostic.as_dict()) + str(raised.value)
    assert "private-marker-value" not in serialized
    assert "private-secret-value" not in serialized


def test_malformed_identity_preserves_parse_stage_without_raw_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, environment = _identity_proc(tmp_path, monkeypatch)
    (tmp_path / str(pid) / "status").write_text("Uid:\tprivate-secret-value")

    with pytest.raises(process_identity.IdentityObservationError) as raised:
        observe_owned_child(pid, environment)

    assert raised.value.diagnostic.stage is process_identity.IdentityStage.UID
    assert raised.value.diagnostic.reason is process_identity.IdentityFailure.MALFORMED
    assert "private-secret-value" not in str(raised.value)


def test_marker_mismatch_preserves_only_the_field_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, environment = _identity_proc(tmp_path, monkeypatch)
    environment[OWNED_MARKER_ENV] = "different-private-marker"

    with pytest.raises(process_identity.IdentityObservationError) as raised:
        observe_owned_child(pid, environment)

    diagnostic = raised.value.diagnostic
    assert diagnostic.reason is process_identity.IdentityFailure.MISMATCH
    assert diagnostic.changed_fields == ("marker",)
    assert "private-marker" not in json.dumps(diagnostic.as_dict()) + str(raised.value)


def test_two_read_identity_change_preserves_field_names_without_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, environment = _identity_proc(tmp_path, monkeypatch)
    ticks = iter([42, 42, 43, 43])
    monkeypatch.setattr(process_identity, "_start_ticks", lambda _: next(ticks))

    with pytest.raises(process_identity.IdentityObservationError) as raised:
        observe_owned_child(pid, environment)

    diagnostic = raised.value.diagnostic
    assert diagnostic.stage is process_identity.IdentityStage.RECHECK
    assert diagnostic.reason is process_identity.IdentityFailure.UNSTABLE
    assert diagnostic.reading is process_identity.IdentityRead.CONFIRMATION
    assert diagnostic.changed_fields == ("start_ticks",)


def test_unreadable_identity_keeps_presence_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, environment = _identity_proc(tmp_path, monkeypatch)
    identity = observe_owned_child(pid, environment)
    (tmp_path / str(pid) / "environ").unlink()

    assert child_presence(identity) is Presence.AMBIGUOUS


@pytest.mark.parametrize("pid", [0, True])
def test_invalid_identity_input_refuses_before_process_reads(
    pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        process_identity, "_read_identity", lambda *_args, **_kwargs: pytest.fail("No process read")
    )

    with pytest.raises(process_identity.IdentityObservationError) as raised:
        observe_owned_child(pid, {OWNED_MARKER_ENV: "private-marker"})

    assert raised.value.diagnostic.stage is process_identity.IdentityStage.INPUT
    assert raised.value.diagnostic.reason is process_identity.IdentityFailure.INVALID


def test_initial_identity_stat_drift_is_not_a_matching_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid, environment = _identity_proc(tmp_path, monkeypatch)
    ticks = iter([42, 43])
    monkeypatch.setattr(process_identity, "_start_ticks", lambda _: next(ticks))

    with pytest.raises(process_identity.IdentityObservationError) as raised:
        observe_owned_child(pid, environment)

    diagnostic = raised.value.diagnostic
    assert diagnostic.stage is process_identity.IdentityStage.START_AFTER
    assert diagnostic.reason is process_identity.IdentityFailure.UNSTABLE
    assert diagnostic.reading is process_identity.IdentityRead.INITIAL
    assert diagnostic.changed_fields == ("start_ticks",)
