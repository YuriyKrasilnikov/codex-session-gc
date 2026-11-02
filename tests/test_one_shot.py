# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, EM101, TRY003

from __future__ import annotations

import concurrent.futures
import errno
import json
import os
import selectors
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from codex_session_gc import recovery
from codex_session_gc.execution import one_shot, process_identity
from codex_session_gc.execution.launch import LaunchBinding
from codex_session_gc.execution.one_shot import _await_readiness, _reap
from codex_session_gc.execution.transport import ChildCommand
from codex_session_gc.recovery import (
    AttemptId,
    LaunchId,
    LaunchPurpose,
    LaunchStatus,
    Payload,
    StartEnvelope,
    TerminalKind,
    TransitionError,
    Unit,
    UnitId,
    create,
    inspect,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_OUTPUT_BOUND = 4096

_FAKE = r"""
import os,sys,json,hashlib,time
sys.path.insert(0, os.environ['PACKAGE_ROOT'])
from pathlib import Path
import codex_session_gc.recovery as recovery
from codex_session_gc.recovery import inspect, LaunchStatus
assert os.path.samefile(recovery.__file__, os.environ['EXPECTED_RECOVERY']), (
 'unexpected application package'
)
assert sys.executable == os.environ['EXPECTED_PYTHON'], 'unexpected Python interpreter'
status, = inspect(Path(os.environ['RECEIPT']))
unit, = status.units
assert unit.request is not None, 'request intent must precede consumption'
launch = status.launches[-1]
assert launch.status is LaunchStatus.REGISTERED
assert launch.registration is not None
pid = json.loads(launch.registration.incarnation.data)['pid']
assert pid == os.getpid(), 'registration must identify consuming process'
raw = sys.stdin.buffer.read()
assert raw == unit.request.data, 'payload bytes changed'
p = Path(os.environ['OBSERVED'])
p.write_bytes((p.read_bytes() if p.exists() else b'') + b'consumed\n')
mode = os.environ['MODE']
outcome = dict(status=mode, request_sha256=list(hashlib.sha256(raw).digest()))
if mode == 'timeout':
 os.write(int(os.environ['ACK_WRITE']),b'x')
 os.read(int(os.environ['ACK_RELEASE']),1)
 os.write(2,b'wake')
 time.sleep(30)
if mode == 'malformed': print('{'); sys.exit(0)
if mode == 'nonobject': print('[]'); sys.exit(0)
if mode == 'digest': outcome['status']='confirmed'; outcome['request_sha256']=[0]*32
if mode == 'wrongexit': outcome['status']='confirmed'
if mode == 'status_type': outcome['status']={}
if mode == 'oversized': print('x'*100000); sys.exit(0)
if mode == 'stderr': sys.stderr.write('x'*100000); sys.stderr.flush()
if mode == 'duplicates': print('{"status":"confirmed","status":"confirmed"}'); sys.exit(0)
if mode == 'extra':
 print(json.dumps(dict(status='confirmed',request_sha256=outcome['request_sha256'])))
 print('{}'); sys.exit(0)
print(json.dumps(outcome), flush=True)
sys.exit({'confirmed':0,'refused':1,'partial':2,'unknown':3}.get(mode,1))
"""


def _setup(tmp_path: Path, mode: str) -> tuple[LaunchBinding, ChildCommand, UnitId, Payload, Path]:
    receipt = tmp_path / "receipt.sqlite"
    attempt, unit = AttemptId(uuid4()), UnitId(uuid4())
    request = Payload("application/test-native-request", 2, b'{"immutable":"request"}')
    evidence = Payload("application/test-evidence", 1, b"qualified")
    create(
        receipt,
        StartEnvelope(attempt, evidence, evidence, evidence, evidence),
        (Unit(unit, 0, evidence),),
    )
    binding = LaunchBinding(receipt, attempt, LaunchId(uuid4()), 1, LaunchPurpose.APPLY)
    observed = tmp_path / "observed"
    recovery_file = recovery.__file__
    if recovery_file is None:
        raise RuntimeError("The loaded recovery module has no file identity")
    package_file = Path(recovery_file).absolute()
    python = Path(sys.executable).absolute()
    command = ChildCommand(
        python,
        ("-B", "-c", _FAKE),
        {
            "PATH": os.environ["PATH"],
            "PACKAGE_ROOT": str(package_file.parent.parent.parent),
            "EXPECTED_RECOVERY": str(package_file),
            "EXPECTED_PYTHON": str(python),
            "RECEIPT": str(receipt),
            "OBSERVED": str(observed),
            "MODE": mode,
        },
    )
    return binding, command, unit, request, observed


def _evidence() -> Payload:
    return Payload("application/test-qualification", 1, b"qualified")


@pytest.mark.parametrize("contents", [b"", b"bad", b"codex-session-gc:ready-v1\nextra", b"codex"])
def test_invalid_readiness_refuses(contents: bytes) -> None:
    """An incomplete, wrong or overlong frame never authorizes identity observation."""
    reader, writer = os.pipe()
    try:
        if contents:
            os.write(writer, contents)
        os.close(writer)
        writer = -1
        with pytest.raises(ValueError, match="readiness frame"):
            _await_readiness(reader, time.monotonic() + 1)
    finally:
        os.close(reader)
        if writer >= 0:
            os.close(writer)


def test_readiness_requires_eof_and_obeys_deadline() -> None:
    """Even an exact frame cannot authorize while the writer remains open."""
    reader, writer = os.pipe()
    try:
        os.write(writer, b"codex-session-gc:ready-v1\n")
        with pytest.raises(TimeoutError):
            _await_readiness(reader, time.monotonic() + 0.05)
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize(
    "mode", ["empty", "partial", "wrong", "extra", "held", "partial_held", "late", "dead"]
)
def test_bad_or_dead_bootstrap_is_fenced_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Malformed, stalled or dead bootstrap processes are reaped before any grant."""
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")
    frame = b"codex-session-gc:ready-v1\n"
    payload = {
        "empty": b"",
        "partial": b"codex",
        "wrong": b"wrong",
        "extra": frame + b"x",
        "held": frame,
        "partial_held": b"codex",
        "late": frame,
        "dead": frame,
    }[mode]
    identity_seen = threading.Event()
    fds: list[int] = []
    original_pipe = os.pipe2

    def pipe(flags: int) -> tuple[int, int]:
        result = original_pipe(flags)
        fds.extend(result)
        return result

    def launcher(inner: ChildCommand, ready: int) -> ChildCommand:
        script = f"import os,time; os.write({ready}, {payload!r}); "
        script += (
            "os._exit(0)"
            if mode == "dead"
            else (
                "time.sleep(5)"
                if mode in {"held", "partial_held"}
                else f"os.close({ready}); time.sleep(5)"
            )
        )
        if mode == "late":
            script = f"import os,time; time.sleep(5); os.write({ready}, {frame!r})"
        return ChildCommand(
            Path(sys.executable).absolute(),
            ("-I", "-c", script),
            inner.environment,
            (*inner.inherited_descriptors, ready),
        )

    def identity(*_args: object, **_kwargs: object) -> process_identity.ChildIdentity:
        identity_seen.set()
        raise AssertionError("identity must not be read")

    monkeypatch.setattr(os, "pipe2", pipe)
    monkeypatch.setattr(one_shot, "owned_launcher_command", launcher)
    monkeypatch.setattr(one_shot, "observe_owned_child", identity)
    with pytest.raises(one_shot.OneShotError) as failure:
        one_shot.dispatch_once(binding, command, unit, request, _evidence(), deadline_seconds=0.3)
    assert failure.value.cleanup is one_shot.PreGrantCleanup.REAPED
    if mode != "dead":
        assert not identity_seen.is_set()
    assert not observed.exists()
    (journal,) = inspect(binding.receipt)
    assert journal.units[0].request is None
    assert journal.launches[0].status is LaunchStatus.FENCED
    for fd in fds:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)


def test_repeated_pre_readiness_interruption_closes_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated interruption before readiness fences each owned child and closes pipes."""
    original_pipe = os.pipe2
    children: list[subprocess.Popen[bytes]] = []
    descriptors: list[int] = []

    def reap(child: subprocess.Popen[bytes], seconds: float) -> bool:
        result = _reap(child, seconds)
        children.append(child)
        return result

    def pipe(flags: int) -> tuple[int, int]:
        result = original_pipe(flags)
        descriptors.extend(result)
        return result

    def interrupt(_fd: int, _deadline: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(one_shot, "_reap", reap)
    monkeypatch.setattr(os, "pipe2", pipe)
    monkeypatch.setattr(one_shot, "_await_readiness", interrupt)
    for index in range(2):
        case = tmp_path / str(index)
        case.mkdir()
        binding, command, unit, request, observed = _setup(case, "confirmed")
        with pytest.raises(KeyboardInterrupt):
            one_shot.dispatch_once(binding, command, unit, request, _evidence())
        assert not observed.exists()
        (journal,) = inspect(binding.receipt)
        assert journal.units[0].request is None
        assert journal.launches[0].status is LaunchStatus.FENCED
    expected_children = 2
    assert len(children) == expected_children
    assert all(child.poll() is not None for child in children)
    for fd in descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)


def test_paused_launcher_cannot_reach_identity_or_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child paused before its readiness write cannot acquire journal authority."""
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")
    gate_read, gate_write = os.pipe2(os.O_CLOEXEC)
    entered_read, entered_write = os.pipe2(os.O_CLOEXEC)
    identity_seen = threading.Event()
    original_command = process_identity.owned_launcher_command
    original_identity = process_identity.observe_owned_child
    delay = (
        "import os,sys; fd=int(sys.argv[1]); entered=int(sys.argv[2]); "
        "os.write(entered,b'x'); os.close(entered); os.read(fd,1); os.close(fd); "
        "os.execv(sys.executable,[sys.executable,*sys.argv[3:]])"
    )

    def delayed_launcher(inner: ChildCommand, ready: int) -> ChildCommand:
        launcher = original_command(inner, ready)
        return replace(
            launcher,
            arguments=("-I", "-c", delay, str(gate_read), str(entered_write), *launcher.arguments),
            inherited_descriptors=(*launcher.inherited_descriptors, gate_read, entered_write),
        )

    def identity(pid: int, environment: Mapping[str, str]) -> process_identity.ChildIdentity:
        identity_seen.set()
        return original_identity(pid, environment)

    monkeypatch.setattr(one_shot, "owned_launcher_command", delayed_launcher)
    monkeypatch.setattr(one_shot, "observe_owned_child", identity)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                one_shot.dispatch_once,
                binding,
                command,
                unit,
                request,
                _evidence(),
                deadline_seconds=5,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(entered_read, selectors.EVENT_READ)
                assert selector.select(2)
                assert os.read(entered_read, 1) == b"x"
            assert not identity_seen.is_set()
            (before,) = inspect(binding.receipt)
            assert before.launches[0].registration is None
            assert before.units[0].request is None
            assert not observed.exists()
            os.write(gate_write, b"x")
            result = future.result(timeout=6)
            assert result.status is TerminalKind.CONFIRMED
            assert identity_seen.is_set()
            assert observed.read_bytes() == b"consumed\n"
    finally:
        os.close(gate_read)
        os.close(gate_write)
        os.close(entered_read)
        os.close(entered_write)


@pytest.mark.parametrize("status", list(TerminalKind))
def test_exact_payload_digest_status_exit_and_owned_lifecycle(
    tmp_path: Path, status: TerminalKind
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, status.value)
    result = one_shot.dispatch_once(binding, command, unit, request, _evidence())
    assert result.reply is not None, (
        f"Expected a helper reply; status={result.status.value}; detail={result.detail!r}; "
        f"returncode={result.returncode}; stderr={result.stderr[:_OUTPUT_BOUND]!r}"
    )
    assert result.status is status
    assert result.request_digest == request.digest
    assert result.reaped
    assert result.closure_recorded
    assert result.terminal_recorded
    assert observed.read_bytes() == b"consumed\n"
    assert json.loads(result.reply.data)["status"] == status.value
    (journal,) = inspect(binding.receipt)
    assert journal.units[0].request == request
    assert journal.units[0].terminal_kind is status
    assert journal.units[0].terminal == result.reply
    assert journal.launches[0].status is LaunchStatus.CLOSED
    pid = json.loads(journal.launches[0].registration.incarnation.data)["pid"]
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.parametrize(
    "mode",
    [
        "malformed",
        "nonobject",
        "digest",
        "wrongexit",
        "status_type",
        "oversized",
        "stderr",
        "duplicates",
        "extra",
    ],
)
def test_ambiguous_outcome_is_unknown_without_resend(tmp_path: Path, mode: str) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, mode)
    result = one_shot.dispatch_once(
        binding,
        command,
        unit,
        request,
        _evidence(),
        deadline_seconds=5,
        maximum_output_bytes=_OUTPUT_BOUND,
    )
    assert result.status is TerminalKind.UNKNOWN
    assert result.reaped
    assert result.closure_recorded
    assert result.terminal_recorded
    assert len(result.stdout) + len(result.stderr) <= _OUTPUT_BOUND
    expected = observed.read_bytes() if observed.exists() else b""
    assert expected == b"consumed\n"
    (journal,) = inspect(binding.receipt)
    assert journal.units[0].terminal_kind is TerminalKind.UNKNOWN
    retry = LaunchBinding(
        binding.receipt, binding.attempt_id, LaunchId(uuid4()), 2, LaunchPurpose.APPLY
    )
    with pytest.raises(one_shot.OneShotError):
        one_shot.dispatch_once(retry, command, unit, request, _evidence())
    assert (observed.read_bytes() if observed.exists() else b"") == expected


def test_timeout_after_consumption_is_unknown_without_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the I/O deadline only after the real child has consumed the request."""
    binding, command, unit, request, observed = _setup(tmp_path, "timeout")
    ack_read, ack_write = os.pipe2(os.O_CLOEXEC)
    release_read, release_write = os.pipe2(os.O_CLOEXEC)
    expired = threading.Event()
    monotonic = time.monotonic

    class Clock:
        def monotonic(self) -> float:
            return monotonic() + (60 if expired.is_set() else 0)

    environment = dict(command.environment)
    environment.update(ACK_WRITE=str(ack_write), ACK_RELEASE=str(release_read))
    command = replace(
        command,
        environment=environment,
        inherited_descriptors=(*command.inherited_descriptors, ack_write, release_read),
    )
    monkeypatch.setattr(one_shot, "time", Clock())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                one_shot.dispatch_once,
                binding,
                command,
                unit,
                request,
                _evidence(),
                deadline_seconds=30,
                maximum_output_bytes=_OUTPUT_BOUND,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(ack_read, selectors.EVENT_READ)
                assert selector.select(10)
                assert os.read(ack_read, 1) == b"x"
            assert observed.read_bytes() == b"consumed\n"
            expired.set()
            os.write(release_write, b"x")
            result = future.result(timeout=5)
        assert result.status is TerminalKind.UNKNOWN
        assert result.reaped
        assert result.closure_recorded
        assert result.terminal_recorded
        assert "deadline" in result.detail
        (journal,) = inspect(binding.receipt)
        assert journal.units[0].terminal_kind is TerminalKind.UNKNOWN
        retry = LaunchBinding(
            binding.receipt, binding.attempt_id, LaunchId(uuid4()), 2, LaunchPurpose.APPLY
        )
        with pytest.raises(one_shot.OneShotError):
            one_shot.dispatch_once(retry, command, unit, request, _evidence())
        assert observed.read_bytes() == b"consumed\n"
    finally:
        for descriptor in (ack_read, ack_write, release_read, release_write):
            os.close(descriptor)


def test_dispatch_journal_refusal_never_grants_and_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")

    def refuse(*_args: object) -> None:
        raise TransitionError("synthetic journal refusal")

    monkeypatch.setattr(one_shot, "record_dispatch_intent", refuse)
    with pytest.raises(one_shot.OneShotError):
        one_shot.dispatch_once(binding, command, unit, request, _evidence())
    assert not observed.exists()
    (journal,) = inspect(binding.receipt)
    assert journal.units[0].request is None
    assert journal.launches[0].status is LaunchStatus.CLOSED
    terminal = journal.launches[0].terminal
    assert terminal is not None
    diagnostic = json.loads(terminal.data)
    assert diagnostic["pre_grant"]["stage"] == "dispatch_intent"
    assert diagnostic["pre_grant"]["identity"] is None
    assert diagnostic["pre_grant"]["child_reaped"] is True
    assert diagnostic["returncode"] is not None
    pid = json.loads(journal.launches[0].registration.incarnation.data)["pid"]
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_identity_failure_fences_waiting_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")
    diagnostic = process_identity.IdentityDiagnostic(
        process_identity.IdentityStage.ENVIRONMENT,
        process_identity.IdentityFailure.IO,
        process_identity.IdentityRead.INITIAL,
        error_number=errno.EACCES,
    )

    identity_attempts = 0

    def unavailable(*_: object) -> process_identity.ChildIdentity:
        nonlocal identity_attempts
        identity_attempts += 1
        raise process_identity.IdentityObservationError(diagnostic)

    monkeypatch.setattr(one_shot, "observe_owned_child", unavailable)
    with pytest.raises(one_shot.OneShotError):
        one_shot.dispatch_once(binding, command, unit, request, _evidence())
    assert not observed.exists()
    (journal,) = inspect(binding.receipt)
    assert journal.launches[0].status is LaunchStatus.FENCED
    assert journal.units[0].request is None
    terminal = journal.launches[0].terminal
    assert terminal is not None
    value = json.loads(terminal.data)
    assert value["returncode"] is not None
    assert value["pre_grant"]["stage"] == "identity"
    assert value["pre_grant"]["child_reaped"] is True
    assert value["pre_grant"]["identity"] == diagnostic.as_dict()
    assert identity_attempts == 1


def test_terminal_journal_failure_keeps_unknown_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")

    def refuse(*_args: object) -> None:
        raise TransitionError("synthetic terminal refusal")

    monkeypatch.setattr(one_shot, "record_terminal_evidence", refuse)
    result = one_shot.dispatch_once(binding, command, unit, request, _evidence())
    assert result.status is TerminalKind.UNKNOWN
    assert not result.terminal_recorded
    assert result.closure_recorded
    assert result.reaped
    assert observed.read_bytes() == b"consumed\n"
    (journal,) = inspect(binding.receipt)
    assert journal.units[0].request == request
    assert journal.units[0].terminal_kind is None


def test_request_bound_refuses_before_launch(tmp_path: Path) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")
    with pytest.raises(ValueError, match="request"):
        one_shot.dispatch_once(
            binding, command, unit, request, _evidence(), maximum_request_bytes=1
        )
    assert not observed.exists()
    (journal,) = inspect(binding.receipt)
    assert not journal.launches


def test_constructor_failure_without_a_handle_retains_cleanup_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EACCES, "Controlled process creation refusal")

    monkeypatch.setattr(one_shot.subprocess, "Popen", unavailable)

    with pytest.raises(one_shot.OneShotError) as raised:
        one_shot.dispatch_once(binding, command, unit, request, _evidence())

    assert raised.value.cleanup is one_shot.PreGrantCleanup.UNRESOLVED
    assert not observed.exists()
    (stored,) = inspect(binding.receipt)
    assert stored.units[0].request is None
    (launch,) = stored.launches
    assert launch.status is LaunchStatus.FENCED
    assert launch.terminal is not None
    evidence = json.loads(launch.terminal.data)
    assert evidence["pre_grant"]["cleanup"] == "unresolved"
    assert evidence["pre_grant"]["child_started"] is None
    assert evidence["pre_grant"]["child_reaped"] is False
    assert evidence["pre_grant"]["errno"] == errno.EACCES


def test_launch_intent_refusal_reports_no_process_cleanup_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")

    def refuse(*_args: object) -> None:
        raise TransitionError("Controlled launch intent refusal")

    monkeypatch.setattr(one_shot, "record_launch_intent", refuse)

    with pytest.raises(one_shot.OneShotError) as raised:
        one_shot.dispatch_once(binding, command, unit, request, _evidence())

    assert raised.value.cleanup is one_shot.PreGrantCleanup.NOT_STARTED
    assert not observed.exists()
    assert not inspect(binding.receipt)[0].launches


def test_constructor_can_start_a_waiting_child_before_raising_without_a_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")
    popen = one_shot.subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def constructor(*args: object, **kwargs: object) -> None:
        children.append(popen(*args, **kwargs))
        raise OSError(errno.EIO, "Controlled constructor error after child creation")

    monkeypatch.setattr(one_shot.subprocess, "Popen", constructor)
    try:
        with pytest.raises(one_shot.OneShotError) as raised:
            one_shot.dispatch_once(binding, command, unit, request, _evidence())
        assert children
        assert raised.value.cleanup is one_shot.PreGrantCleanup.UNRESOLVED
        (stored,) = inspect(binding.receipt)
        assert stored.units[0].request is None
        (launch,) = stored.launches
        assert launch.status is LaunchStatus.FENCED
        assert launch.registration is None
        assert launch.terminal is not None
        evidence = json.loads(launch.terminal.data)
        assert evidence["pre_grant"]["child_started"] is None
        assert evidence["pre_grant"]["cleanup"] == "unresolved"
        assert evidence["returncode"] is None
    finally:
        for child in children:
            try:
                child.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=2)
    assert not observed.exists()


@pytest.mark.parametrize("failure", ["launcher", "intent"])
def test_preflight_or_intent_refusal_starts_no_child_and_closes_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Preflight and journal refusal preserve no-launch semantics and close both pipe ends."""
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")
    descriptors: list[int] = []
    original_pipe = os.pipe2
    spawn_seen = False
    intent_seen = False

    def pipe(flags: int) -> tuple[int, int]:
        result = original_pipe(flags)
        descriptors.extend(result)
        return result

    def spawn(*_args: object, **_kwargs: object) -> None:
        nonlocal spawn_seen
        spawn_seen = True
        raise AssertionError("preflight refusal started a child")

    def refuse_intent(*_args: object, **_kwargs: object) -> None:
        nonlocal intent_seen
        intent_seen = True
        raise TransitionError("synthetic launch intent refusal")

    monkeypatch.setattr(os, "pipe2", pipe)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    if failure == "launcher":
        monkeypatch.setattr(process_identity, "files", lambda _package: tmp_path / "missing")
        monkeypatch.setattr(one_shot, "record_launch_intent", refuse_intent)
        with pytest.raises(process_identity.GatedLaunchError):
            one_shot.dispatch_once(binding, command, unit, request, _evidence())
        assert not intent_seen
    else:
        monkeypatch.setattr(one_shot, "record_launch_intent", refuse_intent)
        with pytest.raises(one_shot.OneShotError) as raised:
            one_shot.dispatch_once(binding, command, unit, request, _evidence())
        assert raised.value.cleanup is one_shot.PreGrantCleanup.NOT_STARTED
        assert intent_seen
    assert not spawn_seen
    assert not observed.exists()
    (journal,) = inspect(binding.receipt)
    assert not journal.launches
    assert journal.units[0].request is None
    expected_descriptors = 2
    assert len(descriptors) == expected_descriptors
    for fd in descriptors:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)
