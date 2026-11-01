# Copyright (c) 2026

"""Exercise bounded installer behavior using isolated synthetic artifacts."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity
from codex_session_gc.installation import transaction
from codex_session_gc.installation.model import (
    InstallLayout,
    InstallOptions,
    InstallReceipt,
    InstallState,
)
from codex_session_gc.installation.process import OwnedProcessError, ProcessLimits, StorageBudget
from codex_session_gc.installation.provenance import PREDICATE_TYPE, verify_artifact
from codex_session_gc.installation.transaction import InstallationError, install
from tests import test_installation_payload as payload_fixture
from tests.test_installation_payload import VERSION, _bundle


def _commands(
    tmp_path: Path, *, fail: bool = False, grow: bool = False, bundle_data: bytes | None = None
) -> tuple[Path, Path]:
    """Create controlled verifier and installer executables."""
    gh, uv = tmp_path / "gh", tmp_path / "uv"
    data = _bundle() if bundle_data is None else bundle_data
    digest = hashlib.sha256(data).hexdigest()
    proof = [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": PREDICATE_TYPE,
                    "subject": [{"digest": {"sha256": digest}}],
                }
            }
        }
    ]
    gh.write_text(f"#!{sys.executable}\nprint({json.dumps(proof)!r})\n")
    uv.write_text(
        f"#!{sys.executable}\nimport pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'venv':\n"
        + (
            "    (pathlib.Path.cwd() / '.install-tmp/growth').write_bytes(b'x' * 4194304)\n"
            "    time.sleep(10)\n"
            if grow
            else ""
        )
        + "    path = pathlib.Path(args[-1]) / 'bin'\n"
        "    path.mkdir(parents=True)\n"
        "    (path / 'python').write_text('fixture')\n"
        "    (path / 'python').chmod(0o700)\n"
        "elif args[:2] == ['pip', 'install']:\n"
        "    assert '--require-hashes' in args and '--only-binary' in args\n"
        "    cli = pathlib.Path.cwd() / 'venv/bin/codex-session-gc'\n"
        "    cli.write_text('fixture'); cli.chmod(0o700)\n"
        f"    sys.exit({1 if fail else 0})\n"
        "else: sys.exit(2)\n"
    )
    gh.chmod(0o700)
    uv.chmod(0o700)
    return gh, uv


def _run(
    tmp_path: Path, *, fail: bool = False, grow: bool = False, identifier: UUID | None = None
) -> InstallReceipt:
    """Run an isolated installation attempt."""
    data = _bundle()
    gh, uv = _commands(tmp_path, fail=fail, grow=grow, bundle_data=data)
    archive = tmp_path / "bundle"
    archive.write_bytes(data)
    base = tmp_path / "app"
    base.mkdir(exist_ok=True)
    layout = InstallLayout(base)
    opts = InstallOptions(
        uv,
        Path(sys.executable),
        0,
        65536 if grow else 16 * 1024 * 1024,
        ProcessLimits(15, 65536),
        payload_fixture.VERSION,
    )
    attempt = identifier or uuid4()

    async def task() -> InstallReceipt:
        """Verify the synthetic bundle and install it."""
        with await verify_artifact(
            archive,
            ArtifactIdentity(hashlib.sha256(data).hexdigest(), len(data)),
            SourceIdentity("a" * 40, "refs/tags/test"),
            trusted_gh=gh,
        ) as lease:
            return await install(lease, layout, attempt, opts)

    return asyncio.run(task())


def test_final_path_without_activation_and_duplicate_refused(tmp_path: Path) -> None:
    """Final path without activation and duplicate refused."""
    base = tmp_path / "app"
    base.mkdir()
    old = base / "old"
    old.mkdir()
    current = base / "current"
    current.symlink_to(old)
    receipt = _run(tmp_path)
    if receipt.state is not InstallState.COMPLETED:
        pytest.fail("Unexpected: receipt.state is not InstallState.COMPLETED")
    if receipt.release != base / "releases" / VERSION:
        pytest.fail("Unexpected: receipt.release != base / 'releases' / VERSION")
    if not ((receipt.release / "venv/bin/python").is_file()):
        pytest.fail("Unexpected: not (receipt.release / 'venv/bin/python').is_...")
    if current.resolve() != old:
        pytest.fail("Unexpected: current.resolve() != old")
    with pytest.raises(InstallationError):
        _run(tmp_path)
    if current.resolve() != old:
        pytest.fail("Unexpected: current.resolve() != old")
    if not (receipt.release.exists()):
        pytest.fail("Unexpected: not receipt.release.exists()")


def test_failed_child_removes_only_owned_incomplete_root(tmp_path: Path) -> None:
    """Failed child removes only owned incomplete root."""
    base = tmp_path / "app"
    base.mkdir()
    old = base / "old"
    old.mkdir()
    with pytest.raises(OwnedProcessError):
        _run(tmp_path, fail=True)
    if not (old.is_dir()):
        pytest.fail("Unexpected: not old.is_dir()")
    if (base / "releases" / VERSION).exists():
        pytest.fail("Unexpected: (base / 'releases' / VERSION).exists()")
    records = list((base / "installations").glob("*.json"))
    if len(records) != 1:
        pytest.fail("Unexpected: len(records) != 1")
    if json.loads(records[0].read_text())["state"] != "failed":
        pytest.fail("Unexpected: json.loads(records[0].read_text())['state'] !...")


def test_unmanaged_root_never_adopted(tmp_path: Path) -> None:
    """Unmanaged root never adopted."""
    base = tmp_path / "app"
    target = base / "releases" / VERSION
    target.mkdir(parents=True)
    (target / "keep").write_text("untouched")
    with pytest.raises(InstallationError):
        _run(tmp_path)
    if (target / "keep").read_text() != "untouched":
        pytest.fail("Unexpected: (target / 'keep').read_text() != 'untouched'")


def test_unresolved_receipt_refuses_without_modification(tmp_path: Path) -> None:
    """Unresolved receipt refuses without modification."""
    base = tmp_path / "app"
    receipts = base / "installations"
    receipts.mkdir(parents=True)
    unknown = receipts / "external.json"
    unknown.write_text("{}")
    with pytest.raises(InstallationError):
        _run(tmp_path)
    if unknown.read_text() != "{}":
        pytest.fail("Unexpected: unknown.read_text() != '{}'")
    if (base / "releases" / VERSION).exists():
        pytest.fail("Unexpected: (base / 'releases' / VERSION).exists()")


def test_install_lock_refuses_concurrent_owner(tmp_path: Path) -> None:
    """Install lock refuses concurrent owner."""
    base = tmp_path / "app"
    base.mkdir()
    lock = base / ".install.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InstallationError):
            _run(tmp_path)
    if (base / "releases" / VERSION).exists():
        pytest.fail("Unexpected: (base / 'releases' / VERSION).exists()")


def test_uncleanable_owned_root_leaves_unresolved_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncleanable owned root leaves unresolved receipt."""

    def refuse_cleanup(_path: Path, _identity: tuple[int, int]) -> None:
        """Refuse cleanup."""
        message = "fixture cleanup failure"
        raise OSError(message)

    monkeypatch.setattr(transaction, "_remove_owned_root", refuse_cleanup)
    with pytest.raises(OSError, match="fixture cleanup failure"):
        _run(tmp_path, fail=True)
    base = tmp_path / "app"
    if not ((base / "releases" / VERSION).is_dir()):
        pytest.fail("Unexpected: not (base / 'releases' / VERSION).is_dir()")
    record = next((base / "installations").glob("*.json"))
    if json.loads(record.read_text())["state"] != "environment_ready":
        pytest.fail("Unexpected: json.loads(record.read_text())['state'] != 'e...")
    with pytest.raises(InstallationError):
        _run(tmp_path)


def test_cancelled_child_is_reaped_before_owned_root_cleanup(tmp_path: Path) -> None:
    """Cancelled child is reaped before owned root cleanup."""
    data = _bundle()
    gh, uv = _commands(tmp_path, bundle_data=data)
    marker = tmp_path / "child-ready"
    uv.write_text(
        f"#!{sys.executable}\nimport os, pathlib, time\n"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    archive = tmp_path / "bundle"
    archive.write_bytes(data)
    base = tmp_path / "app"
    base.mkdir()
    options = InstallOptions(
        uv, Path(sys.executable), 0, 16 * 1024 * 1024, ProcessLimits(70, 65536), VERSION
    )

    async def cancel() -> None:
        """Cancel an installation after its owned child starts."""
        with await verify_artifact(
            archive,
            ArtifactIdentity(hashlib.sha256(data).hexdigest(), len(data)),
            SourceIdentity("a" * 40, "refs/tags/test"),
            trusted_gh=gh,
        ) as lease:
            loop = asyncio.get_running_loop()
            ready = asyncio.Event()

            def check_marker() -> None:
                """Signal when the controlled child has started."""
                if marker.exists():
                    ready.set()
                else:
                    loop.call_later(0.01, check_marker)

            task = asyncio.create_task(install(lease, InstallLayout(base), uuid4(), options))
            check_marker()
            async with asyncio.timeout(10):
                await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(cancel())
    if (base / "releases" / VERSION).exists():
        pytest.fail("Unexpected: (base / 'releases' / VERSION).exists()")
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    receipt = next((base / "installations").glob("*.json"))
    if json.loads(receipt.read_text())["state"] != "failed":
        pytest.fail("Unexpected: json.loads(receipt.read_text())['state'] != '...")


def test_storage_watchdog_stops_growth_and_removes_owned_root(tmp_path: Path) -> None:
    """Storage watchdog stops growth and removes owned root."""
    with pytest.raises(OwnedProcessError) as failure:
        _run(tmp_path, grow=True)
    if failure.value.reason.value != "storage":
        pytest.fail("Unexpected: failure.value.reason.value != 'storage'")
    base = tmp_path / "app"
    if (base / "releases" / VERSION).exists():
        pytest.fail("Unexpected: (base / 'releases' / VERSION).exists()")
    record = next((base / "installations").glob("*.json"))
    if json.loads(record.read_text())["state"] != "failed":
        pytest.fail("Unexpected: json.loads(record.read_text())['state'] != 'f...")


def test_failed_then_retry_then_next_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed then retry then next version."""
    with pytest.raises(OwnedProcessError):
        _run(tmp_path, fail=True)
    first = _run(tmp_path)
    if first.state is not InstallState.COMPLETED:
        pytest.fail("Unexpected: first.state is not InstallState.COMPLETED")
    version = "0.4.2"
    monkeypatch.setattr(payload_fixture, "VERSION", version)
    monkeypatch.setattr(payload_fixture, "ROOT", f"codex-session-gc-{version}-linux-x86_64")
    monkeypatch.setattr(payload_fixture, "WHEEL", f"codex_session_gc-{version}-py3-none-any.whl")
    second = _run(tmp_path)
    if second.state is not InstallState.COMPLETED:
        pytest.fail("Unexpected: second.state is not InstallState.COMPLETED")
    if second.version != version:
        pytest.fail("Unexpected: second.version != version")
    if not (first.release is not None):
        pytest.fail("Unexpected: not first.release is not None")
    if not (first.release.exists()):
        pytest.fail("Unexpected: not first.release.exists()")


@pytest.mark.parametrize("kind", ["missing", "denied"])
def test_watchdog_handles_only_disappearance_as_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Watchdog handles only disappearance as transient."""

    class Entry:
        def stat(self, *, follow_symlinks: bool) -> object:
            """Simulate an entry disappearing or being inaccessible."""
            if follow_symlinks:
                pytest.fail("Unexpected: follow_symlinks")
            if kind == "missing":
                raise FileNotFoundError
            raise PermissionError

    class Entries:
        def __enter__(self) -> list[Entry]:
            """Return the simulated directory entries."""
            return [Entry()]

        def __exit__(self, *_args: object) -> None:
            """Close the simulated directory iterator."""
            return

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", lambda _directory: Entries())
        if kind == "missing":
            StorageBudget.capture(tmp_path, 1024, 0)
        else:
            with pytest.raises(OwnedProcessError) as error:
                StorageBudget.capture(tmp_path, 1024, 0)
            if error.value.reason.value != "storage":
                pytest.fail("Unexpected: error.value.reason.value != 'storage'")
