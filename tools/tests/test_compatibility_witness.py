# Copyright (c) 2026

"""Selected witness adapters execute only controlled disposable fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import TypedDict, Unpack

import pytest

from codex_session_gc.artifacts import ArtifactIdentity
from tools.compatibility import witness


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[Path, witness.WitnessInputs, dict[str, str]]:
    """Create source-qualified fixture inputs without touching a real home."""
    root = tmp_path / "source"
    root.mkdir()
    names = (
        "native/helper/Cargo.toml",
        "native/helper/Cargo.lock",
        "native/helper/rust-toolchain.toml",
        "native/helper/src/main.rs",
        "native/patches/test.patch",
    )
    entries = []
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"synthetic {name}\n".encode()
        path.write_bytes(data)
        entries.append({"path": name, "bytes": len(data), "sha256": _sha(data)})
    helper = tmp_path / "helper"
    helper.write_bytes(b"fixture helper")
    helper_sha = _sha(helper.read_bytes())
    profile = root / "src/codex_session_gc/execution/closed_work_contract.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"binary_sha256": list(bytes.fromhex(helper_sha))}))
    manifest = {"inputs": entries}
    manifest_sha = _sha((json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    provenance = root / "native/build-inputs/provenance.json"
    provenance.parent.mkdir(parents=True)
    provenance.write_text(
        json.dumps(
            {"current_delivery_inputs": {"manifest": manifest, "manifest_sha256": manifest_sha}}
        )
    )
    return (
        root,
        witness.WitnessInputs(helper, ArtifactIdentity(helper_sha, helper.stat().st_size)),
        {
            "helper_sha256": helper_sha,
            "profile_sha256": _sha(profile.read_bytes()),
            "native_inputs_sha256": manifest_sha,
        },
    )


class _Options(TypedDict, total=False):
    cases: list[dict[str, str]] | None
    report_format: str
    arguments: list[str] | None
    output_bytes: int
    seconds: int
    scratch_bytes: int
    free_floor_bytes: int
    fixture: dict[str, object] | None


def _spec(
    root: Path, execution: dict[str, str], script: str, **options: Unpack[_Options]
) -> witness.WitnessSpec:
    path = root / "checks/run.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script)
    raw = path.read_bytes()
    source = witness.SourceFile("checks/run.py", ArtifactIdentity(_sha(raw), len(raw)))
    value = {
        "format": "codex-session-gc-witness-v1",
        "name": "fixture",
        "scope": "disposable fixture cases",
        "review_source": "review/fixture",
        "script": source.path,
        "sources": [
            {"path": source.path, "sha256": source.identity.sha256, "size": source.identity.size}
        ],
        "source_envelope_sha256": witness.source_envelope_sha256((source,)),
        "execution": execution,
        "fixture": options.get("fixture"),
        "arguments": options.get("arguments") or [],
        "native_cases": None,
        "report_format": options.get("report_format", "native_jsonl"),
        "expected_cases": options.get("cases") or [{"name": "first", "status": "confirmed"}],
        "seconds": options.get("seconds", 10),
        "output_bytes": options.get("output_bytes", 8192),
        "scratch_bytes": options.get("scratch_bytes", 1024 * 1024),
        "free_floor_bytes": options.get("free_floor_bytes", 0),
    }
    return witness.parse_spec(json.dumps(value).encode())


def _run(
    spec: witness.WitnessSpec, root: Path, inputs: witness.WitnessInputs
) -> witness.WitnessReceipt:
    return asyncio.run(witness.run_witness(spec, root, inputs, Path(sys.executable).absolute()))


def test_explicit_jsonl_cases_use_owned_home(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """Only expected cases produce evidence, with an isolated environment."""
    root, inputs, execution = fixture
    spec = _spec(
        root,
        execution,
        """import json, os, pathlib
home = pathlib.Path(os.environ["HOME"])
assert home == pathlib.Path(os.environ["CODEX_HOME"])
assert home.parent == pathlib.Path(os.environ["TMPDIR"])
assert pathlib.Path(os.environ["GC_NATIVE_BINARY"]).is_file()
assert not pathlib.Path(os.environ["GC_NATIVE_FIXTURE"]).exists()
print(json.dumps({"case": "first", "status": "confirmed"}))
""",
    )
    receipt = _run(spec, root, inputs)
    if (
        receipt.cases != spec.expected_cases
        or receipt.claim_verified
        or receipt.source_coverage_verified
        or receipt.spec_sha256 != witness.witness_spec_sha256(spec)
    ):
        raise AssertionError


def test_nested_native_outcome_format(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """Nested native outcomes use their explicitly declared representation."""
    root, inputs, execution = fixture
    spec = _spec(
        root,
        execution,
        'print(\'{"case":"first","outcome":{"status":"refused"}}\')\n',
        report_format="native_outcome_jsonl",
        cases=[{"name": "first", "status": "refused"}],
    )
    if _run(spec, root, inputs).cases != spec.expected_cases:
        raise AssertionError


@pytest.mark.parametrize(
    ("script", "cases", "limit"),
    [
        ('print(\'{"case":"first","status":"confirmed"}\', end="")\n', None, 8192),
        (
            'print(\'{"case":"first","status":"confirmed"}\')\n',
            [{"name": "first", "status": "confirmed"}, {"name": "second", "status": "confirmed"}],
            8192,
        ),
        ('print(\'{"case":"first","status":"skipped"}\')\n', None, 8192),
        ('print(\'{"case":"first","status":"confirmed","status":"confirmed"}\')\n', None, 8192),
        ("import sys; sys.exit(2)\n", None, 8192),
        ('print("x" * 10000)\n', None, 128),
    ],
)
def test_failed_partial_skipped_or_oversize_results_refuse(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
    script: str,
    cases: list[dict[str, str]] | None,
    limit: int,
) -> None:
    """Incomplete, failing, skipped, malformed and excessive output are not evidence."""
    root, inputs, execution = fixture
    spec = _spec(root, execution, script, cases=cases, output_bytes=limit)
    with pytest.raises(witness.WitnessError, match=r"."):
        _run(spec, root, inputs)


def test_changed_script_and_execution_binding_refuse(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """The script and qualified source identities must match the declaration."""
    root, inputs, execution = fixture
    spec = _spec(root, execution, 'print(\'{"case":"first","status":"confirmed"}\')\n')
    (root / spec.script).write_text("changed\n")
    with pytest.raises(witness.WitnessError, match="Input"):
        _run(spec, root, inputs)
    wrong = {**execution, "profile_sha256": "a" * 64}
    spec = _spec(root, wrong, 'print(\'{"case":"first","status":"confirmed"}\')\n')
    with pytest.raises(witness.WitnessError, match="Execution source"):
        _run(spec, root, inputs)


def test_mutation_during_witness_refuses(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """A witness cannot return evidence after changing a declared source file."""
    root, inputs, execution = fixture
    spec = _spec(
        root,
        execution,
        """from pathlib import Path
path = Path(__file__)
path.write_text(path.read_text() + "# changed\\n")
print('{"case":"first","status":"confirmed"}')
""",
    )
    with pytest.raises(witness.WitnessError, match="Input"):
        _run(spec, root, inputs)


@pytest.mark.parametrize("skipped", [False, True])
def test_junit_exact_cases_and_skips(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]], *, skipped: bool
) -> None:
    """JUnit reports require complete explicitly expected passing cases."""
    root, inputs, execution = fixture
    child = "<skipped/>" if skipped else ""
    xml = (
        f'<testsuites><testsuite tests="1" errors="0" failures="0" skipped="{int(skipped)}">'
        f'<testcase classname="sample" name="one">{child}</testcase></testsuite></testsuites>'
    )
    spec = _spec(
        root,
        execution,
        f"import pathlib,sys\npathlib.Path(sys.argv[1]).write_text({xml!r})\n",
        report_format="pytest_junit",
        arguments=["{report}"],
        cases=[{"name": "sample::one", "status": "passed"}],
    )
    if skipped:
        with pytest.raises(witness.WitnessError, match="skips"):
            _run(spec, root, inputs)
    elif _run(spec, root, inputs).cases != spec.expected_cases:
        raise AssertionError


def test_junit_entity_declaration_refuses(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """XML declarations cannot expand entities during result normalization."""
    root, inputs, execution = fixture
    xml = '<!DOCTYPE x [<!ENTITY y "z">]><testsuite tests="0" errors="0" failures="0" skipped="0"/>'
    spec = _spec(
        root,
        execution,
        f"import pathlib,sys\npathlib.Path(sys.argv[1]).write_text({xml!r})\n",
        report_format="pytest_junit",
        arguments=["{report}"],
        cases=[{"name": "sample::one", "status": "passed"}],
    )
    with pytest.raises(witness.WitnessError, match="forbidden"):
        _run(spec, root, inputs)


def test_cancellation_reaps_child(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """Cancellation of a running witness reaps its process before returning."""
    root, inputs, execution = fixture
    marker = root / "started"
    script = """import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
"""
    spec = _spec(root, execution, script, arguments=["{source}/started"])

    python = Path(sys.executable).absolute()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        started: asyncio.Future[int] = loop.create_future()

        def check_marker() -> None:
            if started.done():
                return
            if marker.exists():
                started.set_result(int(marker.read_text()))
            else:
                loop.call_later(0.01, check_marker)

        task = asyncio.create_task(witness.run_witness(spec, root, inputs, python))
        check_marker()
        async with asyncio.timeout(10):
            pid = await started
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(run())


def test_fixture_artifact_must_match_declared_identity(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]], tmp_path: Path
) -> None:
    """A supplied fixture helper is passed only after its bytes are verified."""
    root, inputs, execution = fixture
    fixture_path = tmp_path / "fixture-helper"
    fixture_path.write_bytes(b"fixture binary")
    identity = ArtifactIdentity(_sha(fixture_path.read_bytes()), fixture_path.stat().st_size)
    spec = _spec(
        root,
        execution,
        'import json, os, pathlib\nassert pathlib.Path(os.environ["GC_NATIVE_FIXTURE"]).is_file()\n'
        'print(json.dumps({"case":"first","status":"confirmed"}))\n',
        fixture={"sha256": identity.sha256, "size": identity.size},
    )
    bound = witness.WitnessInputs(inputs.helper, inputs.helper_identity, fixture_path)
    receipt = _run(spec, root, bound)
    if (
        receipt.fixture != identity
        or receipt.helper != inputs.helper_identity
        or receipt.sources != spec.sources
    ):
        raise AssertionError
    fixture_path.write_bytes(b"changed")
    with pytest.raises(witness.WitnessError, match="Input"):
        _run(spec, root, bound)


def test_deadline_terminates_witness(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """An overdue child cannot produce a partial success receipt."""
    root, inputs, execution = fixture
    spec = _spec(root, execution, "import time; time.sleep(30)\n", seconds=1)
    with pytest.raises(witness.WitnessError, match=r"timeout|deadline"):
        _run(spec, root, inputs)


def test_unsafe_argument_and_unknown_spec_field_refuse(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """A declaration cannot select an arbitrary home or add unreviewed fields."""
    root, _, execution = fixture
    with pytest.raises(witness.WitnessError, match="argument"):
        _spec(root, execution, "pass\n", arguments=["--home=/root"])
    with pytest.raises(witness.WitnessError, match="fields"):
        witness.parse_spec(b'{"format":"codex-session-gc-witness-v1","extra":true}')


def test_expected_native_unknown_is_an_observed_negative_control(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """An expected unknown operation outcome is retained without authorizing support."""
    root, inputs, execution = fixture
    spec = _spec(
        root,
        execution,
        'print(\'{"case":"first","outcome":{"status":"unknown"}}\')\n',
        report_format="native_outcome_jsonl",
        cases=[{"name": "first", "status": "unknown"}],
    )
    receipt = _run(spec, root, inputs)
    if (
        receipt.cases != spec.expected_cases
        or receipt.claim_verified
        or receipt.authorizes_operation
    ):
        raise AssertionError


def test_source_envelope_digest_cannot_be_rebound(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """A source-envelope identity must describe the declared source files."""
    root, _, execution = fixture
    spec = _spec(root, execution, "pass\n")
    with pytest.raises(witness.WitnessError, match="envelope digest"):
        replace(spec, source_envelope_sha256="f" * 64)


def test_incomplete_junit_suite_refuses(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """A JUnit suite reporting more tests than it contains is not evidence."""
    root, inputs, execution = fixture
    xml = (
        '<testsuite tests="2" errors="0" failures="0" skipped="0">'
        '<testcase classname="sample" name="one"/></testsuite>'
    )
    spec = _spec(
        root,
        execution,
        f"import pathlib,sys\npathlib.Path(sys.argv[1]).write_text({xml!r})\n",
        report_format="pytest_junit",
        arguments=["{report}"],
        cases=[{"name": "sample::one", "status": "passed"}],
    )
    with pytest.raises(witness.WitnessError, match="incomplete"):
        _run(spec, root, inputs)


def test_owned_scratch_budget_refuses_growth(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """A witness exceeding its owned scratch budget cannot return evidence."""
    root, inputs, execution = fixture
    script = (
        "import pathlib, os, time\n"
        'pathlib.Path(os.environ["TMPDIR"]).joinpath("large").write_bytes(b"x" * 200000)\n'
        "time.sleep(2)\n"
    )
    spec = _spec(root, execution, script, scratch_bytes=1024)
    with pytest.raises(witness.WitnessError, match="storage"):
        _run(spec, root, inputs)


def test_real_pytest_junit_is_normalized(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """A real tiny pytest run produces the declared passing JUnit case."""
    root, inputs, execution = fixture
    script = """import pathlib, pytest, sys
pathlib.Path("test_sample.py").write_text("def test_one():\\n    assert 2 + 2 == 4\\n")
raise SystemExit(pytest.main([
    "-q", "-p", "no:cacheprovider", "--junitxml=" + sys.argv[1], "test_sample.py",
]))
"""
    spec = _spec(
        root,
        execution,
        script,
        report_format="pytest_junit",
        arguments=["{report}"],
        cases=[{"name": "test_sample::test_one", "status": "passed"}],
    )
    receipt = _run(spec, root, inputs)
    if receipt.cases != spec.expected_cases or receipt.claim_verified:
        raise AssertionError


def test_pytest_unexpected_pass_is_not_evidence(
    fixture: tuple[Path, witness.WitnessInputs, dict[str, str]],
) -> None:
    """An unexpected pass cannot be normalized as an ordinary passing witness."""
    root, inputs, execution = fixture
    script = """import pathlib, pytest, sys
pathlib.Path("test_sample.py").write_text(
    "import pytest\\n@pytest.mark.xfail(reason='expected failure')\\ndef test_one():\\n    pass\\n"
)
raise SystemExit(pytest.main([
    "-q", "-p", "no:cacheprovider", "--junitxml=" + sys.argv[1], "test_sample.py",
]))
"""
    spec = _spec(
        root,
        execution,
        script,
        report_format="pytest_junit",
        arguments=["{report}"],
        cases=[{"name": "test_sample::test_one", "status": "passed"}],
    )
    with pytest.raises(witness.WitnessError, match="failed"):
        _run(spec, root, inputs)
