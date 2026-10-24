# Copyright (c) 2026
# ruff: noqa: D100, D103, S101, PLR2004, ANN202, C408

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from codex_session_gc.execution.closed_work_apply import DispatchLimits, apply_closed_work
from codex_session_gc.planning.closed_work import select_closed_work
from codex_session_gc.recovery import ReceiptIdentityError, TerminalKind, inspect
from tests.current_storage_fixture import _LIMITS, _fixture, _id

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
    result = apply_closed_work(facts, selections, **options)
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
