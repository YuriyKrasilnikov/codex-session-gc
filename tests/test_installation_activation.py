# Copyright (c) 2026

"""Exercise activation and interruption recovery in isolated synthetic layouts."""

from __future__ import annotations

import fcntl
import json
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from codex_session_gc.installation import activation
from codex_session_gc.installation.activation import (
    ActivationError,
    ActivationStatus,
    activate,
    activation_status,
    mcp_argv,
    observe_expected_link,
    reconcile_activation,
)
from codex_session_gc.installation.model import InstallLayout, InstallReceipt
from codex_session_gc.installation.transaction import InstallationError
from tests import test_installation_transaction as transaction_fixture
from tests.test_installation_payload import _bundle as make_bundle
from tests.test_installation_transaction import _run as run_fixture

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import ExitStack
    from pathlib import Path
    from uuid import UUID


def _require(condition: object, message: str) -> None:
    if not condition:
        pytest.fail(message)


def _run(tmp_path: Path) -> InstallReceipt:
    """Reuse one exact synthetic archive for both verification and consumption."""
    data = make_bundle()
    with patch.object(transaction_fixture, "_bundle", return_value=data):
        return run_fixture(tmp_path)


def _setup(tmp_path: Path) -> tuple[InstallLayout, UUID, Path, Path]:
    receipt = _run(tmp_path)
    root = receipt.release
    if root is None:
        pytest.fail("Synthetic installation has no root")
    layout = InstallLayout(tmp_path / "app")
    if receipt.binding is None:
        pytest.fail("Completed installation has no verified binding")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return layout, receipt.installation_id, bindir / "codex-session-gc", root


def test_absent_link_activation_and_versioned_mcp_argv(tmp_path: Path) -> None:
    """Activate an absent owned link and return exact versioned MCP arguments."""
    layout, identifier, link, root = _setup(tmp_path)
    expected = observe_expected_link(layout, link)
    result = activate(layout, identifier, link, expected)
    _require(result is ActivationStatus.ACTIVE, "activation did not succeed")
    _require(link.resolve() == root / "venv/bin/codex-session-gc", "link target differs")
    _require(activation_status(layout, identifier) is ActivationStatus.ACTIVE, "status differs")
    argv = mcp_argv(layout, identifier, tmp_path / "home", tmp_path / "receipts")
    _require(
        argv
        == (
            str(root / "venv/bin/codex-session-gc"),
            "mcp",
            "--home",
            str(tmp_path / "home"),
            "--helper",
            str(root / "codex-session-gc-native"),
            "--receipt-dir",
            str(tmp_path / "receipts"),
            "--support-install-dir",
            str(layout.base),
            "--support-installation-id",
            str(identifier),
        ),
        "MCP arguments differ",
    )


def test_explicit_previous_link_preserves_old_root(tmp_path: Path) -> None:
    """A named prior launcher remains untouched after switching the link."""
    layout, identifier, link, root = _setup(tmp_path)
    old = layout.base / "manual-old" / "launcher"
    old.parent.mkdir()
    old.write_text("old launcher")
    old.chmod(0o700)
    link.symlink_to(old)
    expected = observe_expected_link(layout, link)
    activate(layout, identifier, link, expected)
    _require(old.read_text() == "old launcher", "old installation changed")
    _require(link.resolve() == root / "venv/bin/codex-session-gc", "new link differs")


@pytest.mark.parametrize("kind", ["regular", "outside", "changed", "parent"])
def test_unknown_or_changed_link_refuses(tmp_path: Path, kind: str) -> None:
    """Refuse unrelated files, external targets and changed link or parent identities."""
    layout, identifier, link, _ = _setup(tmp_path)
    if kind == "regular":
        link.write_text("unmanaged")
        with pytest.raises(ActivationError):
            observe_expected_link(layout, link)
        return
    if kind == "outside":
        external = tmp_path / "external"
        external.write_text("external")
        external.chmod(0o700)
        link.symlink_to(external)
        with pytest.raises(ActivationError):
            observe_expected_link(layout, link)
        return
    expected = observe_expected_link(layout, link)
    if kind == "changed":
        link.write_text("concurrent")
    else:
        previous = link.parent.with_name("old-bin")
        link.parent.rename(previous)
        link.parent.mkdir()
    with pytest.raises(ActivationError):
        activate(layout, identifier, link, expected)


@pytest.mark.parametrize("moment", ["before", "after"])
def test_interrupted_switch_status_is_observation_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    """Classify the saved intent using the actual link without another switch."""
    layout, identifier, link, _ = _setup(tmp_path)
    expected = observe_expected_link(layout, link)
    original = vars(activation)["_switch"]

    def interrupt(parent_fd: int, temporary: str, name: str) -> None:
        if moment == "after":
            original(parent_fd, temporary, name)
        raise RuntimeError(moment)

    monkeypatch.setattr(activation, "_switch", interrupt)
    with pytest.raises(RuntimeError, match=moment):
        activate(layout, identifier, link, expected)
    before = link.readlink() if link.is_symlink() else None
    record = layout.receipts / f"{identifier}.json"
    record_before = record.read_bytes()
    status = activation_status(layout, identifier)
    _require(
        status
        is (ActivationStatus.ACTIVE if moment == "after" else ActivationStatus.NOT_ACTIVATED),
        "interrupted status differs",
    )
    _require((link.readlink() if link.is_symlink() else None) == before, "status changed the link")
    _require(record.read_bytes() == record_before, "status changed the receipt")
    _require(reconcile_activation(layout, identifier) is status, "reconciliation differs")
    _require(
        json.loads(record.read_bytes())["activation"]["phase"] == status.value,
        "reconciliation did not record observed outcome",
    )
    _require(
        (link.readlink() if link.is_symlink() else None) == before,
        "reconciliation retried the link switch",
    )


def test_changed_executable_refuses_activation(tmp_path: Path) -> None:
    """A launcher that no longer matches its completed receipt is not activated."""
    layout, identifier, link, root = _setup(tmp_path)
    expected = observe_expected_link(layout, link)
    (root / "venv/bin/codex-session-gc").write_text("changed")
    with pytest.raises(ActivationError):
        activate(layout, identifier, link, expected)
    _require(not link.exists(), "unexpected link was created")


def test_activation_uses_install_lock(tmp_path: Path) -> None:
    """A competing installer prevents an activation switch."""
    layout, identifier, link, _ = _setup(tmp_path)
    expected = observe_expected_link(layout, link)
    with layout.lock.open("r+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InstallationError):
            activate(layout, identifier, link, expected)
    _require(not link.exists(), "activation ignored the lock")


def test_reconciliation_keeps_unrecognized_link_untouched(tmp_path: Path) -> None:
    """A later unmanaged file is unknown and never replaced or removed."""
    layout, identifier, link, _ = _setup(tmp_path)
    activate(layout, identifier, link, observe_expected_link(layout, link))
    link.unlink()
    link.write_text("unmanaged")
    _require(
        activation_status(layout, identifier) is ActivationStatus.UNKNOWN, "status was not unknown"
    )
    _require(
        reconcile_activation(layout, identifier) is ActivationStatus.UNKNOWN,
        "reconcile was not unknown",
    )
    _require(link.read_text() == "unmanaged", "unmanaged link was altered")


def test_changed_release_after_activation_is_unknown(tmp_path: Path) -> None:
    """A link to an altered release is not reported as a healthy activation."""
    layout, identifier, link, root = _setup(tmp_path)
    activate(layout, identifier, link, observe_expected_link(layout, link))
    (root / "codex-session-gc-native").write_bytes(b"changed")
    _require(
        activation_status(layout, identifier) is ActivationStatus.UNKNOWN,
        "changed release was active",
    )


def test_unresolved_intent_blocks_later_install_until_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded interruption remains unresolved until explicit observation is saved."""
    layout, identifier, link, _ = _setup(tmp_path)

    def interrupt(_parent_fd: int, _temporary: str, _name: str) -> None:
        message = "interrupted before switch"
        raise RuntimeError(message)

    monkeypatch.setattr(activation, "_switch", interrupt)
    with pytest.raises(RuntimeError, match="interrupted before switch"):
        activate(layout, identifier, link, observe_expected_link(layout, link))
    with pytest.raises(InstallationError, match="activation intent is unresolved"):
        _run(tmp_path)
    _require(
        reconcile_activation(layout, identifier) is ActivationStatus.NOT_ACTIVATED,
        "reconciliation did not observe the prior link",
    )
    with pytest.raises(InstallationError, match="Version root already exists"):
        _run(tmp_path)


def test_unbound_completed_receipt_is_not_adopted(tmp_path: Path) -> None:
    """A completed record without a verified manifest binding cannot be activated."""
    layout, identifier, link, _ = _setup(tmp_path)
    record = layout.receipts / f"{identifier}.json"
    data = json.loads(record.read_bytes())
    del data["binding"]
    record.write_text(json.dumps(data))
    with pytest.raises(ActivationError, match="completed, path-bound"):
        activate(layout, identifier, link, observe_expected_link(layout, link))
    _require(not link.exists(), "unbound installation was activated")
    with pytest.raises(InstallationError, match="unresolved"):
        _run(tmp_path)


def test_unknown_receipt_field_is_not_adopted(tmp_path: Path) -> None:
    """An unfamiliar installation record cannot authorize a link switch."""
    layout, identifier, link, _ = _setup(tmp_path)
    record = layout.receipts / f"{identifier}.json"
    data = json.loads(record.read_bytes())
    data["unrecognized"] = "value"
    record.write_text(json.dumps(data))
    with pytest.raises(ActivationError, match="completed, path-bound"):
        activate(layout, identifier, link, observe_expected_link(layout, link))
    _require(not link.exists(), "unrecognized record authorized activation")


@pytest.mark.parametrize("changed", ["destination", "parent", "temporary"])
def test_changes_during_release_validation_refuse_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """Changes during the last release validation cannot be overwritten by activation."""
    layout, identifier, link, _ = _setup(tmp_path)
    expected = observe_expected_link(layout, link)
    original = cast(
        "Callable[[InstallLayout, dict[str, object], ExitStack], Path]",
        vars(activation)["_checked_release"],
    )
    calls = 0
    final_validation = 2
    foreign: Path | None = None

    def changed_during_check(
        selected_layout: InstallLayout, data: dict[str, object], resources: ExitStack
    ) -> Path:
        nonlocal calls, foreign
        result = original(selected_layout, data, resources)
        calls += 1
        if calls == final_validation:
            if changed == "destination":
                foreign = link
                foreign.write_text("foreign destination")
            elif changed == "parent":
                old_parent = link.parent.with_name("old-bin")
                link.parent.rename(old_parent)
                link.parent.mkdir()
                foreign = link.parent / "foreign"
                foreign.write_text("foreign parent")
            else:
                saved: object = json.loads((layout.receipts / f"{identifier}.json").read_bytes())
                if not isinstance(saved, dict):
                    pytest.fail("Saved activation record is not an object")
                intent: object = saved.get("activation")
                if not isinstance(intent, dict) or not isinstance(intent.get("temporary"), str):
                    pytest.fail("Saved temporary identity is unavailable")
                foreign = link.parent / intent["temporary"]
                foreign.unlink()
                foreign.write_text("foreign temporary")
        return result

    monkeypatch.setattr(activation, "_checked_release", changed_during_check)
    with pytest.raises(ActivationError):
        activate(layout, identifier, link, expected)
    if calls != final_validation or foreign is None:
        pytest.fail("The last release-validation change was not exercised")
    _require(foreign.read_text().startswith("foreign"), "foreign entry was changed or removed")
    if changed == "temporary":
        _require(
            reconcile_activation(layout, identifier) is ActivationStatus.UNKNOWN,
            "unrecognized temporary link should remain unresolved",
        )


def test_unknown_switch_result_is_not_recorded_as_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign post-switch replacement remains untouched and the intent stays unresolved."""
    layout, identifier, link, _ = _setup(tmp_path)
    original = vars(activation)["_switch"]

    def replace_after_switch(parent_fd: int, temporary: str, name: str) -> None:
        original(parent_fd, temporary, name)
        link.unlink()
        link.write_text("foreign after switch")

    monkeypatch.setattr(activation, "_switch", replace_after_switch)
    with pytest.raises(ActivationError):
        activate(layout, identifier, link, observe_expected_link(layout, link))
    _require(link.read_text() == "foreign after switch", "foreign switch result was modified")
    record = json.loads((layout.receipts / f"{identifier}.json").read_bytes())
    _require(record["activation"]["phase"] == "intent", "unknown result was marked active")
    _require(
        activation_status(layout, identifier) is ActivationStatus.UNKNOWN,
        "unknown result was misclassified",
    )
