# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, EM101, TRY003

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from codex_session_gc.execution import one_shot
from codex_session_gc.execution.launch import LaunchBinding
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

_OUTPUT_BOUND = 4096

_FAKE = r"""
import os,sys,json,hashlib,time
sys.path.insert(0, os.environ['SOURCE'])
from pathlib import Path
from codex_session_gc.recovery import inspect, LaunchStatus
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
if mode == 'timeout': time.sleep(30)
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
    command = ChildCommand(
        Path(sys.executable).resolve(),
        ("-B", "-c", _FAKE),
        {
            "PATH": os.environ["PATH"],
            "SOURCE": str(Path(__file__).resolve().parents[1] / "src"),
            "RECEIPT": str(receipt),
            "OBSERVED": str(observed),
            "MODE": mode,
        },
    )
    return binding, command, unit, request, observed


def _evidence() -> Payload:
    return Payload("application/test-qualification", 1, b"qualified")


@pytest.mark.parametrize("status", list(TerminalKind))
def test_exact_payload_digest_status_exit_and_owned_lifecycle(
    tmp_path: Path, status: TerminalKind
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, status.value)
    result = one_shot.dispatch_once(binding, command, unit, request, _evidence())
    assert result.status is status
    assert result.request_digest == request.digest
    assert result.reaped
    assert result.closure_recorded
    assert result.terminal_recorded
    assert observed.read_bytes() == b"consumed\n"
    assert result.reply is not None
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
        "timeout",
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
        deadline_seconds=0.8 if mode == "timeout" else 5,
        maximum_output_bytes=_OUTPUT_BOUND,
    )
    assert result.status is TerminalKind.UNKNOWN
    assert result.reaped
    assert result.closure_recorded
    assert result.terminal_recorded
    assert len(result.stdout) + len(result.stderr) <= _OUTPUT_BOUND
    expected = observed.read_bytes() if observed.exists() else b""
    assert expected in ({b"", b"consumed\n"} if mode == "timeout" else {b"consumed\n"})
    (journal,) = inspect(binding.receipt)
    assert journal.units[0].terminal_kind is TerminalKind.UNKNOWN
    retry = LaunchBinding(
        binding.receipt, binding.attempt_id, LaunchId(uuid4()), 2, LaunchPurpose.APPLY
    )
    with pytest.raises(one_shot.OneShotError):
        one_shot.dispatch_once(retry, command, unit, request, _evidence())
    assert (observed.read_bytes() if observed.exists() else b"") == expected


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
    pid = json.loads(journal.launches[0].registration.incarnation.data)["pid"]
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_identity_failure_fences_waiting_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, command, unit, request, observed = _setup(tmp_path, "confirmed")
    monkeypatch.setattr(one_shot, "observe_owned_child", lambda *_: None)
    with pytest.raises(one_shot.OneShotError):
        one_shot.dispatch_once(binding, command, unit, request, _evidence())
    assert not observed.exists()
    (journal,) = inspect(binding.receipt)
    assert journal.launches[0].status is LaunchStatus.FENCED
    assert journal.units[0].request is None


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
