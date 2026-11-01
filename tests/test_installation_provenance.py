# Copyright (c) 2026

"""Exercise provenance admission with a controlled verifier executable."""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, ParamSpec

import pytest

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity
from codex_session_gc.installation import provenance

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

P = ParamSpec("P")


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, ArtifactIdentity, SourceIdentity, Path]:
    """Create an artifact and a verifier that enforces a known policy."""
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"artifact")
    identity = ArtifactIdentity(hashlib.sha256(b"artifact").hexdigest(), 8)
    source = SourceIdentity("a" * 40, "refs/tags/v1")
    verifier = tmp_path / "gh"
    proof = [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": provenance.PREDICATE_TYPE,
                    "subject": [{"digest": {"sha256": identity.sha256}}],
                }
            }
        }
    ]
    flags = {
        "--repo": provenance.REPOSITORY,
        "--signer-workflow": provenance.SIGNER_WORKFLOW,
        "--source-digest": source.commit,
        "--source-ref": source.ref,
        "--predicate-type": provenance.PREDICATE_TYPE,
        "--format": "json",
    }
    verifier.write_text(
        f"#!{sys.executable}\n"
        "import sys, hashlib\n"
        f"flags = {flags!r}\n"
        "args = sys.argv[1:]\n"
        f"digest = {identity.sha256!r}\n"
        "ok = args[:2] == ['attestation', 'verify'] and '--deny-self-hosted-runners' in args\n"
        "ok = ok and hashlib.sha256(open(args[2], 'rb').read()).hexdigest() == digest\n"
        "ok = ok and all(args.count(k) == 1 and args[args.index(k) + 1] == v "
        "for k, v in flags.items())\n"
        f"print({json.dumps(proof)!r}) if ok else sys.exit(1)\n",
    )
    verifier.chmod(0o700)
    return artifact, identity, source, verifier


def run_verify(
    artifact: Path, identity: ArtifactIdentity, source: SourceIdentity, verifier: Path
) -> None:
    """Consume the controlled verifier's lease."""

    async def verify() -> None:
        with await provenance.verify_artifact(
            artifact, identity, source, trusted_gh=verifier
        ) as lease:
            lease.read(1024)

    asyncio.run(verify())


def test_valid(inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path]) -> None:
    """A policy-compliant verifier admits the exact bytes."""
    artifact, identity, source, verifier = inputs
    run_verify(artifact, identity, source, verifier)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("REPOSITORY", "wrong/repo"),
        ("SIGNER_WORKFLOW", "wrong/workflow"),
        ("PREDICATE_TYPE", "wrong/predicate"),
    ],
)
def test_wrong_policy(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    """The verifier rejects a different repository, workflow or predicate."""
    artifact, identity, source, verifier = inputs
    monkeypatch.setattr(provenance, field, value)
    with pytest.raises(provenance.ProvenanceRefusalError):
        run_verify(artifact, identity, source, verifier)


@pytest.mark.parametrize(
    "source", [SourceIdentity("b" * 40, "refs/tags/v1"), SourceIdentity("a" * 40, "refs/tags/v2")]
)
def test_wrong_source(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path], source: SourceIdentity
) -> None:
    """The verifier rejects a different source commit or ref."""
    artifact, identity, _, verifier = inputs
    with pytest.raises(provenance.ProvenanceRefusalError):
        run_verify(artifact, identity, source, verifier)


def test_missing_and_changed_artifact(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path],
) -> None:
    """A missing verifier or changed subject cannot be admitted."""
    artifact, identity, source, verifier = inputs
    with pytest.raises(provenance.ProvenanceRefusalError):
        run_verify(artifact, identity, source, verifier.parent / "missing")
    artifact.write_bytes(b"changed")
    with pytest.raises(provenance.ProvenanceRefusalError):
        run_verify(artifact, identity, source, verifier)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("print('not json')", provenance.RefusalReason.OUTPUT),
        ("import sys; sys.exit(2)", provenance.RefusalReason.VERIFIER_FAILED),
        ("import time; time.sleep(10)", provenance.RefusalReason.TIMEOUT),
    ],
)
def test_verifier_refusals(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path],
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    reason: provenance.RefusalReason,
) -> None:
    """Malformed output, failure and timeout all refuse admission."""
    artifact, identity, source, verifier = inputs
    verifier.write_text(f"#!{sys.executable}\n{body}\n")
    monkeypatch.setattr(provenance, "_TIMEOUT", 3.0)
    with pytest.raises(provenance.ProvenanceRefusalError) as error:
        run_verify(artifact, identity, source, verifier)
    if error.value.reason != reason:
        raise AssertionError


def test_wrong_subject(inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path]) -> None:
    """A verifier response for different bytes is refused."""
    artifact, identity, source, verifier = inputs
    proof = [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": provenance.PREDICATE_TYPE,
                    "subject": [{"digest": {"sha256": "b" * 64}}],
                }
            }
        }
    ]
    verifier.write_text(f"#!{sys.executable}\nprint({json.dumps(proof)!r})\n")
    with pytest.raises(provenance.ProvenanceRefusalError, match="output"):
        run_verify(artifact, identity, source, verifier)


def test_replacement_and_mutation(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path],
) -> None:
    """A lease follows the opened inode and refuses changes to it."""
    artifact, identity, source, verifier = inputs

    async def verify() -> None:
        lease = await provenance.verify_artifact(artifact, identity, source, trusted_gh=verifier)
        saved = artifact.with_name("saved")
        artifact.rename(saved)
        artifact.write_bytes(b"replaced")
        with lease:
            if lease.read(8) != b"artifact":
                raise AssertionError
        lease = await provenance.verify_artifact(saved, identity, source, trusted_gh=verifier)
        saved.write_bytes(b"modified")
        with pytest.raises(provenance.ProvenanceRefusalError, match="artifact"):
            lease.read(8)
        lease.close()

    asyncio.run(verify())


def test_symlink_refused(inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path]) -> None:
    """Opening a symbolic link cannot substitute another artifact."""
    artifact, identity, source, verifier = inputs
    link = artifact.with_name("link")
    link.symlink_to(artifact)
    with pytest.raises(provenance.ProvenanceRefusalError, match="artifact"):
        run_verify(link, identity, source, verifier)


def test_output_cap_and_descendant_pipe(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Large output and a descendant holding stdout both finish by refusal."""
    artifact, identity, source, verifier = inputs
    monkeypatch.setattr(provenance, "_TIMEOUT", 3.0)
    verifier.write_text(f"#!{sys.executable}\nprint('x' * 20000)\n")
    monkeypatch.setattr(provenance, "_OUTPUT_LIMIT", 100)
    with pytest.raises(provenance.ProvenanceRefusalError, match="output"):
        run_verify(artifact, identity, source, verifier)
    verifier.write_text(f"#!{sys.executable}\nimport os, time\nif os.fork() == 0: time.sleep(20)\n")
    with pytest.raises(provenance.ProvenanceRefusalError, match="timeout"):
        run_verify(artifact, identity, source, verifier)


def test_cancel_execution(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation terminates and reaps a running verifier."""
    artifact, identity, source, verifier = inputs
    verifier.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(20)\n")
    actual = asyncio.create_subprocess_exec

    async def run() -> None:
        started = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []

        def observe(
            call: Callable[P, Awaitable[asyncio.subprocess.Process]],
        ) -> Callable[P, Awaitable[asyncio.subprocess.Process]]:
            async def inner(*args: P.args, **kwargs: P.kwargs) -> asyncio.subprocess.Process:
                child = await call(*args, **kwargs)
                children.append(child)
                started.set()
                return child

            return inner

        monkeypatch.setattr(asyncio, "create_subprocess_exec", observe(actual))
        task = asyncio.create_task(
            provenance.verify_artifact(artifact, identity, source, trusted_gh=verifier),
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if not children or children[0].returncode is None:
            raise AssertionError

    asyncio.run(run())


def count_fds() -> int:
    """Count descriptors owned by the current process."""
    return len(list(Path("/proc/self/fd").iterdir()))


def test_cancel_creation(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during creation still reaps the child and closes descriptors."""
    artifact, identity, source, verifier = inputs
    actual = asyncio.create_subprocess_exec

    async def run() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        def delay(
            call: Callable[P, Awaitable[asyncio.subprocess.Process]],
        ) -> Callable[P, Awaitable[asyncio.subprocess.Process]]:
            async def inner(*args: P.args, **kwargs: P.kwargs) -> asyncio.subprocess.Process:
                entered.set()
                await release.wait()
                return await call(*args, **kwargs)

            return inner

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delay(actual))
        before = count_fds()
        task = asyncio.create_task(
            provenance.verify_artifact(artifact, identity, source, trusted_gh=verifier)
        )
        task.add_done_callback(lambda _: entered.set())
        async with asyncio.timeout(10):
            await entered.wait()
        if task.done():
            await task
            raise AssertionError
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if count_fds() != before:
            raise AssertionError

    asyncio.run(run())


def test_repeated_cancel_during_creation(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated cancellation cannot interrupt child and descriptor cleanup."""
    artifact, identity, source, verifier = inputs
    verifier.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(20)\n")
    actual_creation = asyncio.create_subprocess_exec

    async def run() -> None:
        entered = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        children: list[asyncio.subprocess.Process] = []

        def delay(
            call: Callable[P, Awaitable[asyncio.subprocess.Process]],
        ) -> Callable[P, Awaitable[asyncio.subprocess.Process]]:
            async def inner(*args: P.args, **kwargs: P.kwargs) -> asyncio.subprocess.Process:
                entered.set()
                await release.wait()
                child = await call(*args, **kwargs)
                children.append(child)
                return child

            return inner

        monkeypatch.setattr(asyncio, "create_subprocess_exec", delay(actual_creation))
        before = count_fds()
        task = asyncio.create_task(
            provenance.verify_artifact(artifact, identity, source, trusted_gh=verifier),
        )
        async with asyncio.timeout(10):
            await entered.wait()
            task.cancel()
            asyncio.get_running_loop().call_soon(cleaning.set)
            await cleaning.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        if len(children) != 1 or children[0].returncode != -signal.SIGKILL or count_fds() != before:
            raise AssertionError

    asyncio.run(run())


def _result(
    subjects: list[object], predicate: str = provenance.PREDICATE_TYPE
) -> dict[str, object]:
    return {"verificationResult": {"statement": {"predicateType": predicate, "subject": subjects}}}


@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_verified_results_and_subjects(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path], *, reverse: bool
) -> None:
    """Every result may contain multiple matching or unrelated well-formed subjects."""
    artifact, identity, source, verifier = inputs
    subject = {"digest": {"sha256": identity.sha256, "arn": "arn:example:artifact/1"}}
    other = {"digest": {"sha512": "base64/+=="}}
    subjects: list[object] = [subject, other, subject]
    if reverse:
        subjects.reverse()
    results = [_result(subjects), _result([other, subject]), _result([subject])]
    if reverse:
        results.reverse()
    verifier.write_text(f"#!{sys.executable}\nprint({json.dumps(results)!r})\n")
    run_verify(artifact, identity, source, verifier)


@pytest.mark.parametrize(
    "case",
    [
        "empty-results",
        "non-object-result",
        "missing-subjects",
        "empty-subjects",
        "non-object-subject",
        "empty-digest",
        "non-string-digest",
        "invalid-digest",
        "invalid-sha256",
        "invalid-algorithm",
        "malformed-subject-first",
        "invalid-result-first",
        "missing-match",
        "wrong-predicate",
        "duplicate-keys",
        "truncated",
        "excessive-nesting",
    ],
)
def test_each_result_and_subject_must_be_valid(
    inputs: tuple[Path, ArtifactIdentity, SourceIdentity, Path], case: str
) -> None:
    """Malformed or conflicting siblings refuse the whole verifier response."""
    artifact, identity, source, verifier = inputs
    matching = {"digest": {"sha256": identity.sha256}}
    valid = _result([matching])
    invalid: dict[str, object] | object
    invalid = {
        "non-object-result": None,
        "missing-subjects": {
            "verificationResult": {"statement": {"predicateType": provenance.PREDICATE_TYPE}}
        },
        "empty-subjects": _result([]),
        "non-object-subject": _result([matching, None]),
        "empty-digest": _result([matching, {"digest": {}}]),
        "non-string-digest": _result([matching, {"digest": {"sha512": 2}}]),
        "invalid-digest": _result([matching, {"digest": {"sha512": ""}}]),
        "invalid-sha256": _result([matching, {"digest": {"sha256": "abcd"}}]),
        "invalid-algorithm": _result([matching, {"digest": {"": "aa"}}]),
        "malformed-subject-first": _result([None, matching]),
        "missing-match": _result([{"digest": {"sha256": "b" * 64}}]),
        "wrong-predicate": _result([matching], "wrong"),
    }.get(case)
    raw = json.dumps([] if case == "empty-results" else [valid, invalid])
    if case == "invalid-result-first":
        raw = json.dumps([None, valid])
    if case == "duplicate-keys":
        raw = '[{"verificationResult":{},"verificationResult":{}}]'
    elif case == "truncated":
        raw = json.dumps([valid])[:-1]
    elif case == "excessive-nesting":
        raw = "[" * 1500 + "]" * 1500
    verifier.write_text(f"#!{sys.executable}\nprint({raw!r})\n")
    with pytest.raises(provenance.ProvenanceRefusalError, match="output"):
        run_verify(artifact, identity, source, verifier)
