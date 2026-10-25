# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004, ANN202, C408

from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from codex_session_gc.execution import closed_work_apply, one_shot, process_identity
from codex_session_gc.execution.closed_work_apply import DispatchLimits, apply_closed_work
from codex_session_gc.execution.operation_status import project_operation_status
from codex_session_gc.execution.progress import ProgressChannel, ProgressKind
from codex_session_gc.planning.closed_work import select_closed_work
from codex_session_gc.recovery import (
    AttemptId,
    LaunchStatus,
    ReceiptIdentityError,
    TerminalKind,
    TransitionError,
    inspect,
)
from tests.current_storage_fixture import _LIMITS, _fixture, _id

if TYPE_CHECKING:
    from codex_session_gc.execution.one_shot import OneShotResult

_FAKE = """
import sys,json,hashlib,os
sys.path.insert(0, SOURCE)
from pathlib import Path
from codex_session_gc.recovery import inspect, LaunchStatus
raw=sys.stdin.buffer.read()
r=json.loads(raw)
status,=inspect(Path(r['journal']))
unit=next(u for u in status.units if str(u.unit.unit_id.value)==r['unit_id'])
assert unit.request.data==raw
assert status.launches[-1].status is LaunchStatus.REGISTERED
assert json.loads(status.launches[-1].registration.incarnation.data)['pid']==os.getpid()
declared=json.loads(unit.unit.evidence.data)
assert declared['ownership_scope']==r['ownership_scope']['thread_ids']
assert declared['deletion_scope']==r['deletion_scope']
with open(OBSERVED,'a') as stream:stream.write(r['ownership_scope']['root_thread_id']+'\\n')
mode=MODE
if mode=='mixed':
 mode='confirmed' if r['ownership_scope']['root_thread_id'].endswith('000001') else 'refused'
if mode=='malformed':print('{');sys.exit(0)
print(json.dumps({'status':mode,'request_sha256':list(hashlib.sha256(raw).digest())}))
sys.exit({'confirmed':0,'refused':1,'partial':2,'unknown':3}[mode])
"""


def _setup(tmp_path: Path, mode: str = "confirmed"):
    observation, first, contract = _fixture(tmp_path)
    facts = observation
    second = select_closed_work(facts.snapshot, facts.cutoff, frozenset({_id(3)}))
    observed = tmp_path / "consumed"
    executable = tmp_path / "helper"
    source = str(Path(__file__).resolve().parents[1] / "src")
    executable.write_text(
        f"#!{sys.executable}\nSOURCE={source!r}\nOBSERVED={str(observed)!r}\nMODE={mode!r}\n"
        + _FAKE
    )
    executable.chmod(0o700)
    contract = replace(contract, binary_sha256=sha256(executable.read_bytes()).digest())
    options = dict(
        codex_home=tmp_path / "home",
        sqlite_home=tmp_path / "home",
        journal=tmp_path / "receipt.sqlite",
        helper_executable=executable,
        helper=contract,
        default_provider="openai",
        collection_limits=_LIMITS,
        dispatch_limits=DispatchLimits(5, 1_000_000, 4096, 1),
    )
    return facts, (first, second), options, observed


def test_two_roots_are_declared_before_owned_helper_and_dispatched_once(tmp_path: Path) -> None:
    facts, selections, options, observed = _setup(tmp_path)
    result = apply_closed_work(facts, selections, **options)
    assert result.outcome is TerminalKind.CONFIRMED
    assert result.lifecycle_recorded
    assert [x.outcome for x in result.roots] == [TerminalKind.CONFIRMED] * 2
    assert observed.read_text().splitlines() == [str(_id(1).value), str(_id(3).value)]
    (stored,) = inspect(options["journal"])
    assert len(stored.units) == len(stored.launches) == 2
    assert not stored.lifecycle_unresolved
    assert not stored.startup_unresolved
    context = json.loads(stored.start.evidence.data)
    assert context["cutoff"] == facts.cutoff.timestamp.isoformat()
    assert context["scopes"][0]["ownership_scope"] == [str(_id(1).value), str(_id(2).value)]
    for launch in stored.launches:
        pid = json.loads(launch.registration.incarnation.data)["pid"]
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
    with pytest.raises(ReceiptIdentityError):
        apply_closed_work(facts, selections, **options)
    assert len(observed.read_text().splitlines()) == 2


@pytest.mark.parametrize("mode", ["partial", "unknown", "malformed"])
def test_uncertain_root_halts_without_replay(tmp_path: Path, mode: str) -> None:
    facts, selections, options, observed = _setup(tmp_path, mode)
    progress = ProgressChannel()
    result = apply_closed_work(facts, selections, **options, progress=progress)
    expected = TerminalKind.PARTIAL if mode == "partial" else TerminalKind.UNKNOWN
    assert result.outcome is expected
    assert result.roots[0].outcome is expected
    assert result.roots[1].outcome is None
    assert result.roots[1].dispatch is None
    assert observed.read_text().splitlines() == [str(_id(1).value)]
    (stored,) = inspect(options["journal"])
    assert stored.unresolved_unit is stored.units[0]
    assert stored.units[1].request is None
    assert len(stored.launches) == 1
    events = progress.drain()
    returned = [event for event in events if event.kind is ProgressKind.ROOT_RESULT]
    assert [(event.root, event.outcome) for event in returned] == [(_id(1), expected)]
    assert {event.operation_id for event in events} == {result.attempt_id}


def test_progress_reports_actual_roots_and_uuid_without_additional_execution(
    tmp_path: Path,
) -> None:
    facts, selections, options, observed = _setup(tmp_path)
    progress = ProgressChannel()
    attempt = AttemptId(UUID(int=71))
    result = apply_closed_work(facts, selections, **options, attempt_id=attempt, progress=progress)
    events = progress.drain()
    assert [event.kind for event in events] == [
        ProgressKind.OPERATION,
        ProgressKind.ROOT_STARTED,
        ProgressKind.ROOT_RESULT,
        ProgressKind.ROOT_STARTED,
        ProgressKind.ROOT_RESULT,
    ]
    assert {event.operation_id for event in events} == {attempt}
    returned = [event for event in events if event.kind is ProgressKind.ROOT_RESULT]
    assert [(event.root, event.outcome) for event in returned] == [
        (root.root, root.outcome) for root in result.roots
    ]
    assert observed.read_text().splitlines() == [str(_id(1).value), str(_id(3).value)]
    (stored,) = inspect(options["journal"])
    assert stored.start.attempt_id == attempt
    assert len(stored.units) == len(stored.launches) == 2
    assert progress.drain() == ()


def test_waiting_execution_outlives_a_failing_and_discarded_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, selections, options, observed = _setup(tmp_path)
    progress = ProgressChannel()
    ready, release = threading.Event(), threading.Event()
    original = closed_work_apply.dispatch_once

    def gated(*args: object, **kwargs: object) -> OneShotResult:
        ready.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    def observe(channel: ProgressChannel) -> None:
        events = channel.drain()
        assert events[-1].kind is ProgressKind.ROOT_STARTED
        assert events[-1].root == _id(1)
        message = "Observer stopped"
        raise RuntimeError(message)

    monkeypatch.setattr(closed_work_apply, "dispatch_once", gated)
    with ThreadPoolExecutor(max_workers=2) as workers:
        future = workers.submit(apply_closed_work, facts, selections, **options, progress=progress)
        try:
            assert ready.wait(10)
            assert not future.done()
            observer = workers.submit(observe, progress)
            with pytest.raises(RuntimeError):
                observer.result(timeout=10)
            assert not future.done()
            del observer
            del progress
        finally:
            release.set()
        result = future.result(timeout=30)
    assert result.outcome is TerminalKind.CONFIRMED
    assert observed.read_text().splitlines() == [str(_id(1).value), str(_id(3).value)]
    (stored,) = inspect(options["journal"])
    assert len(stored.units) == len(stored.launches) == 2
    assert not stored.lifecycle_unresolved


def test_arbitrary_progress_callback_is_refused_before_receipt_or_dispatch(tmp_path: Path) -> None:
    facts, selections, options, observed = _setup(tmp_path)

    class Observer:
        def publish(self, _event: object) -> None:
            pytest.fail("Observer callback entered mutation")

    with pytest.raises(ValueError, match="concrete local observation channel"):
        apply_closed_work(facts, selections, **options, progress=Observer())
    assert not options["journal"].exists()
    assert not observed.exists()


def test_native_refusal_is_reported_and_next_independent_root_runs(tmp_path: Path) -> None:
    facts, selections, options, observed = _setup(tmp_path, "refused")
    result = apply_closed_work(facts, selections, **options)
    assert result.outcome is TerminalKind.REFUSED
    assert all(x.outcome is TerminalKind.REFUSED for x in result.roots)
    assert len(observed.read_text().splitlines()) == 2


def test_collection_failure_has_no_launch_or_dispatch(tmp_path: Path) -> None:
    facts, selections, options, observed = _setup(tmp_path)
    options["collection_limits"] = replace(_LIMITS, maximum_total_bytes=1)
    result = apply_closed_work(facts, selections, **options)
    assert result.outcome is TerminalKind.REFUSED
    assert all(x.outcome is None for x in result.roots)
    assert not observed.exists()
    (stored,) = inspect(options["journal"])
    assert not stored.launches
    assert all(x.request is None for x in stored.units)


def test_digest_mismatch_and_overlapping_scope_refuse_before_receipt(tmp_path: Path) -> None:
    facts, selections, options, observed = _setup(tmp_path)
    qualified_helper = options["helper"]
    options["helper"] = replace(qualified_helper, binary_sha256=b"x" * 32)
    # Refusal type and absence of effects are the contract; human wording is not.
    with pytest.raises(ValueError):  # noqa: PT011
        apply_closed_work(facts, selections, **options)
    assert not options["journal"].exists()
    assert not observed.exists()
    options["helper"] = qualified_helper
    # Refusal type and absence of effects are the contract; human wording is not.
    with pytest.raises(ValueError):  # noqa: PT011
        apply_closed_work(facts, (selections[0], selections[0]), **options)
    assert not options["journal"].exists()
    assert not observed.exists()


def test_exact_child_subset_preserves_full_work_ownership_declaration(tmp_path: Path) -> None:
    facts, selections, options, observed = _setup(tmp_path)
    subset = select_closed_work(
        facts.snapshot, facts.cutoff, selections[0].roots, selected_threads=frozenset({_id(2)})
    )
    result = apply_closed_work(facts, (subset,), **options)
    assert result.outcome is TerminalKind.CONFIRMED
    (stored,) = inspect(options["journal"])
    request = json.loads(stored.units[0].request.data)
    assert request["deletion_scope"] == [str(_id(2).value)]
    assert request["ownership_scope"]["thread_ids"] == [str(_id(1).value), str(_id(2).value)]
    assert observed.read_text().splitlines() == [str(_id(1).value)]


def test_dispatch_size_refusal_never_launches_helper(tmp_path: Path) -> None:
    facts, selections, options, observed = _setup(tmp_path)
    options["dispatch_limits"] = DispatchLimits(5, 1, 4096, 1)
    result = apply_closed_work(facts, selections, **options)
    assert result.outcome is TerminalKind.REFUSED
    assert result.roots[0].outcome is None
    assert result.roots[1].outcome is None
    assert not observed.exists()
    (stored,) = inspect(options["journal"])
    assert not stored.launches
    assert all(unit.request is None for unit in stored.units)


def test_mixed_confirmed_and_refused_is_partial_without_reclassifying_roots(tmp_path: Path) -> None:
    facts, selections, options, observed = _setup(tmp_path, "mixed")
    result = apply_closed_work(facts, selections, **options)
    assert result.outcome is TerminalKind.PARTIAL
    assert [root.outcome for root in result.roots] == [TerminalKind.CONFIRMED, TerminalKind.REFUSED]
    assert len(observed.read_text().splitlines()) == 2


@pytest.mark.parametrize("cleanup", ["reaped", "unresolved", "constructor_unknown"])
def test_pre_grant_cleanup_controls_definitive_lifecycle_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    facts, selections, options, observed = _setup(tmp_path)

    class _WaitingProcess:
        pid = 543210
        returncode = -15 if cleanup == "reaped" else None

    def spawn(*_args: object, **_kwargs: object) -> _WaitingProcess:
        if cleanup == "constructor_unknown":
            message = "Controlled constructor error without a returned process handle"
            raise OSError(message)
        return _WaitingProcess()

    def unavailable(*_args: object) -> process_identity.ChildIdentity:
        diagnostic = process_identity.IdentityDiagnostic(
            process_identity.IdentityStage.ENVIRONMENT,
            process_identity.IdentityFailure.IO,
            process_identity.IdentityRead.INITIAL,
        )
        raise process_identity.IdentityObservationError(diagnostic)

    monkeypatch.setattr(one_shot.subprocess, "Popen", spawn)
    monkeypatch.setattr(one_shot, "observe_owned_child", unavailable)
    monkeypatch.setattr(one_shot, "_reap", lambda *_args: cleanup == "reaped")

    result = apply_closed_work(facts, selections, **options)

    assert not observed.exists()
    (stored,) = inspect(options["journal"])
    assert all(unit.request is None for unit in stored.units)
    projected = project_operation_status(stored)
    (launch,) = stored.launches
    assert launch.status is LaunchStatus.FENCED
    assert launch.registration is None
    assert launch.terminal is not None
    evidence = json.loads(launch.terminal.data)
    if cleanup in {"unresolved", "constructor_unknown"}:
        assert result.outcome is TerminalKind.UNKNOWN
        assert not result.lifecycle_recorded
        assert stored.shutdown is None
        assert stored.lifecycle is None
        assert projected.outcome is TerminalKind.UNKNOWN
        assert projected.lifecycle_unresolved
    else:
        assert result.outcome is TerminalKind.REFUSED
        assert result.lifecycle_recorded
        assert stored.shutdown is not None
        assert stored.lifecycle_kind is TerminalKind.CONFIRMED
        assert projected.outcome is TerminalKind.REFUSED
        assert not projected.lifecycle_unresolved
    expected_cleanup = "unresolved" if cleanup == "constructor_unknown" else cleanup
    assert evidence["pre_grant"]["cleanup"] == expected_cleanup
    if cleanup == "constructor_unknown":
        assert evidence["pre_grant"]["child_started"] is None


def test_unresolved_later_launcher_keeps_prior_delivery_and_whole_attempt_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, selections, options, observed = _setup(tmp_path)
    popen = one_shot.subprocess.Popen
    started = False

    class _WaitingProcess:
        pid = 543210
        returncode = None

    def identity(*_args: object) -> process_identity.ChildIdentity:
        diagnostic = process_identity.IdentityDiagnostic(
            process_identity.IdentityStage.ENVIRONMENT,
            process_identity.IdentityFailure.IO,
            process_identity.IdentityRead.INITIAL,
        )
        raise process_identity.IdentityObservationError(diagnostic)

    def spawn(*args: object, **kwargs: object):
        nonlocal started
        if not started:
            started = True
            return popen(*args, **kwargs)
        monkeypatch.setattr(one_shot, "observe_owned_child", identity)
        monkeypatch.setattr(one_shot, "_reap", lambda *_args: False)
        return _WaitingProcess()

    monkeypatch.setattr(one_shot.subprocess, "Popen", spawn)

    result = apply_closed_work(facts, selections, **options)

    assert result.outcome is TerminalKind.UNKNOWN
    assert not result.lifecycle_recorded
    assert observed.read_text().splitlines() == [str(next(iter(selections[0].roots)).value)]
    (stored,) = inspect(options["journal"])
    assert stored.units[0].terminal_kind is TerminalKind.CONFIRMED
    assert stored.units[1].request is None
    assert stored.launches[0].status is LaunchStatus.CLOSED
    assert stored.launches[1].status is LaunchStatus.FENCED
    assert stored.shutdown is None
    assert stored.lifecycle is None
    projected = project_operation_status(stored)
    assert projected.outcome is TerminalKind.UNKNOWN
    assert projected.lifecycle_unresolved


def test_proven_pre_spawn_refusal_settles_without_a_launch_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, selections, options, observed = _setup(tmp_path)

    def refuse_intent(*_args: object) -> None:
        message = "Controlled refusal before process construction"
        raise TransitionError(message)

    monkeypatch.setattr(one_shot, "record_launch_intent", refuse_intent)
    monkeypatch.setattr(
        one_shot.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Process construction must remain unreachable"),
    )

    result = apply_closed_work(facts, selections, **options)

    assert not observed.exists()
    (stored,) = inspect(options["journal"])
    assert not stored.launches
    assert all(unit.request is None for unit in stored.units)
    assert result.outcome is TerminalKind.REFUSED
    assert result.lifecycle_recorded
    assert stored.shutdown is not None
    assert stored.lifecycle is None
    projected = project_operation_status(stored)
    assert projected.outcome is TerminalKind.REFUSED
    assert not projected.lifecycle_unresolved
