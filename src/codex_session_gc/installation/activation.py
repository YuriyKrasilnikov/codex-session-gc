# Copyright (c) 2026

"""Conditionally activate a completed release and reconcile interrupted switches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID, uuid4

from codex_session_gc.artifacts.bundle import MANIFEST_MAX_BYTES, parse_manifest
from codex_session_gc.installation.model import CompletedInstallation, InstallBinding
from codex_session_gc.installation.payload import _wheel_binding
from codex_session_gc.installation.transaction import (
    InstallationError,
    _owned_directory,
    _read_receipt,
    _valid_link_record,
    _validate_receipt,
    installation_lock,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from codex_session_gc.installation.helper_observation import HelperObservation
    from codex_session_gc.installation.model import InstallLayout
    from codex_session_gc.installation.read_budget import ReadBudget


class ActivationError(Exception):
    """An activation cannot be safely admitted or reconciled."""


class ActivationStatus(StrEnum):
    """Observed link state, independent of whether a caller received a reply."""

    NOT_ACTIVATED = "not_activated"
    ACTIVE = "active"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LinkIdentity:
    """Identity and target of the symlink the caller explicitly observed."""

    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    target: str


@dataclass(frozen=True, slots=True)
class ExpectedLink:
    """The bin parent and either an absent link or its exact prior identity."""

    parent_device: int
    parent_inode: int
    link: LinkIdentity | None


def _fail(message: str) -> NoReturn:
    raise ActivationError(message)


def _regular_fd(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        os.close(fd)
        _fail("An installed executable is not a regular executable file.")
    return fd


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _digest(fd: int, budget: ReadBudget | None = None) -> str:
    digest = hashlib.sha256()
    offset = 0
    expected_size = os.fstat(fd).st_size
    if budget is not None:
        budget.check(expected_size)
    while data := os.pread(fd, min(1024 * 1024, expected_size - offset + 1), offset):
        digest.update(data)
        offset += len(data)
        if budget is not None:
            budget.check(offset)
        if offset > expected_size:
            _fail("Installed file grew during validation.")
    if offset != expected_size:
        _fail("Installed file changed during validation.")
    if budget is not None:
        budget.check()
    return digest.hexdigest()


def _read_manifest(path: Path, budget: ReadBudget | None = None) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if budget is not None:
            budget.check(info.st_size)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MANIFEST_MAX_BYTES:
            _fail("Installed manifest is not bounded regular data.")
        data = os.pread(fd, info.st_size + 1, 0)
        if budget is not None:
            budget.check()
        if len(data) != info.st_size or _fingerprint(info) != _fingerprint(os.fstat(fd)):
            _fail("Installed manifest changed during validation.")
        return data
    finally:
        os.close(fd)


def _receipt(
    layout: InstallLayout, installation_id: UUID, *, data: object | None = None
) -> dict[str, object]:
    _owned_directory(layout.receipts)
    if data is None:
        data = _read_receipt(layout.receipts / f"{installation_id}.json")
    if (
        not isinstance(data, dict)
        or set(data)
        not in (
            {"installation_id", "state", "version", "release", "binding"},
            {"installation_id", "state", "version", "release", "binding", "activation"},
        )
        or ("activation" in data and not isinstance(data["activation"], dict))
        or data.get("installation_id") != str(installation_id)
        or data.get("state") != "completed"
        or not isinstance(data.get("version"), str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", str(data["version"])) is None
        or ".." in str(data["version"])
        or data.get("release") != str(layout.releases / str(data["version"]))
    ):
        _fail("A completed, path-bound installation receipt is required.")
    return data


def _release_helper_digest(
    path: Path,
    descriptor: int,
    resources: ExitStack,
    budget: ReadBudget | None,
    observation: HelperObservation | None,
) -> str:
    if observation is None:
        return _digest(descriptor, budget)
    if budget is None:
        _fail("A helper observation requires the shared read budget.")
    initial = _fingerprint(os.fstat(descriptor))
    reused = observation.digest_for(path, descriptor, budget)

    @contextmanager
    def recheck_helper() -> Iterator[None]:
        yield
        current = observation.digest_for(path, descriptor, budget)
        if _fingerprint(os.fstat(descriptor)) != initial or (
            reused is not None and current != reused
        ):
            _fail("Installed helper changed during observation.")

    resources.enter_context(recheck_helper())
    return reused if reused is not None else _digest(descriptor, budget)


def _checked_release(
    layout: InstallLayout,
    data: dict[str, object],
    resources: ExitStack,
    budget: ReadBudget | None = None,
    helper_observation: HelperObservation | None = None,
) -> Path:
    version = data["version"]
    if not isinstance(version, str):
        _fail("Installation version is invalid.")
    root = layout.releases / version
    _owned_directory(layout.releases)
    _owned_directory(root)
    raw = _read_manifest(root / "manifest.json", budget)
    binding = data.get("binding")
    if (
        not isinstance(binding, dict)
        or set(binding) != {"manifest_sha256", "launcher_sha256", "launcher_size"}
        or hashlib.sha256(raw).hexdigest() != binding["manifest_sha256"]
    ):
        _fail("Installation receipt does not bind its verified manifest.")
    manifest = parse_manifest(raw)
    if manifest.version != version:
        _fail("Installed version and manifest disagree.")
    wheel = f"codex_session_gc-{version}-py3-none-any.whl"
    helper_path = root / "codex-session-gc-native"
    _owned_directory(root / "venv")
    _owned_directory(root / "venv/bin")
    cli_path = root / "venv/bin/codex-session-gc"
    helper = _regular_fd(helper_path)
    resources.callback(os.close, helper)
    cli = _regular_fd(cli_path)
    resources.callback(os.close, cli)
    wheel_fd = os.open(root / wheel, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    resources.callback(os.close, wheel_fd)
    paths = ((helper_path, helper), (cli_path, cli), (root / wheel, wheel_fd))
    before = tuple(_fingerprint(os.fstat(fd)) for _, fd in paths)
    if (
        not stat.S_ISREG(os.fstat(wheel_fd).st_mode)
        or os.fstat(wheel_fd).st_size != manifest.files[wheel].size
        or os.fstat(helper).st_size != manifest.files["codex-session-gc-native"].size
        or type(binding["launcher_size"]) is not int
        or os.fstat(cli).st_size != binding["launcher_size"]
    ):
        _fail("Installed executable or wheel size differs from its receipt.")
    wheel_hash = _digest(wheel_fd, budget)
    helper_hash = _release_helper_digest(helper_path, helper, resources, budget, helper_observation)
    cli_hash = _digest(cli, budget)
    if (
        wheel_hash != manifest.files[wheel].sha256
        or helper_hash != manifest.files["codex-session-gc-native"].sha256
        or cli_hash != binding["launcher_sha256"]
    ):
        _fail("Installed executable or wheel differs from its receipt.")
    _wheel_binding(
        root,
        version,
        manifest.raw,
        {wheel: {"sha256": wheel_hash}, "codex-session-gc-native": {"sha256": helper_hash}},
        budget=budget,
    )
    for (path, fd), identity in zip(paths, before, strict=True):
        if _fingerprint(os.fstat(fd)) != identity or _fingerprint(path.lstat()) != identity:
            _fail("Installed files changed during validation.")
    return root


@contextmanager
def completed_installation(
    layout: InstallLayout,
    installation_id: UUID,
    *,
    budget: ReadBudget | None = None,
    helper_observation: HelperObservation | None = None,
) -> Iterator[CompletedInstallation]:
    """Hold verified completed-release descriptors; callers lock separately for mutation."""
    if helper_observation is not None and budget is None:
        _fail("A helper observation requires the shared read budget.")
    if budget is not None:
        budget.check()
    _owned_directory(layout.base)
    if layout.base.resolve(strict=True) != layout.base:
        _fail("Installation base must not traverse aliases.")
    receipt_path = layout.receipts / f"{installation_id}.json"
    before = _fingerprint(receipt_path.stat(follow_symlinks=False))
    data = _receipt(layout, installation_id, data=_validate_receipt(layout, receipt_path, budget))
    with ExitStack() as resources:
        root = _checked_release(layout, data, resources, budget, helper_observation)
        if _fingerprint(receipt_path.stat(follow_symlinks=False)) != before:
            _fail("Installation receipt changed during validation.")
        binding = data.get("binding")
        if not isinstance(binding, dict):
            _fail("Installation receipt binding is unavailable.")
        yield CompletedInstallation(
            installation_id,
            root,
            InstallBinding(
                binding["manifest_sha256"], binding["launcher_sha256"], binding["launcher_size"]
            ),
        )
        if budget is not None:
            budget.check()
        if _fingerprint(receipt_path.stat(follow_symlinks=False)) != before:
            _fail("Installation receipt changed during observation.")


def _link_identity(parent_fd: int, name: str) -> LinkIdentity | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        _fail("Activation link is not a symlink.")
    target = os.readlink(name, dir_fd=parent_fd)
    return LinkIdentity(info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, target)


def _open_parent(link: Path) -> int:
    if not link.is_absolute() or link.name in ("", ".", ".."):
        _fail("Activation link must be an absolute filename.")
    parent = link.parent
    _owned_directory(parent)
    if parent.resolve(strict=True) != parent:
        _fail("Activation parent must not traverse aliases.")
    return os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)


def observe_expected_link(layout: InstallLayout, link: Path) -> ExpectedLink:
    """Capture the current owned link and parent as an explicit future precondition."""
    parent_fd = _open_parent(link)
    try:
        info = os.fstat(parent_fd)
        identity = _link_identity(parent_fd, link.name)
        if identity is not None:
            target = Path(identity.target)
            if not target.is_absolute():
                target = link.parent / target
            resolved = target.resolve(strict=True)
            if (
                not resolved.is_relative_to(layout.base)
                or not resolved.is_file()
                or not os.access(resolved, os.X_OK)
            ):
                _fail("Existing link does not name an executable under the application base.")
        return ExpectedLink(info.st_dev, info.st_ino, identity)
    finally:
        os.close(parent_fd)


def _parent_matches(fd: int, link: Path, expected: ExpectedLink) -> bool:
    info = os.fstat(fd)
    try:
        current = link.parent.lstat()
    except FileNotFoundError:
        return False
    return (
        (info.st_dev, info.st_ino)
        == (expected.parent_device, expected.parent_inode)
        == (current.st_dev, current.st_ino)
    )


def _record(layout: InstallLayout, installation_id: UUID, data: dict[str, object]) -> None:
    """Replace the existing receipt atomically after syncing a task-owned new file."""
    directory_fd = os.open(layout.receipts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = f".{installation_id}.{uuid4()}.tmp"
    try:
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
        )
        try:
            with os.fdopen(fd, "wb") as output:
                output.write((json.dumps(data, sort_keys=True) + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
            os.replace(
                name, f"{installation_id}.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
            os.fsync(directory_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _link_record(identity: LinkIdentity | None) -> dict[str, object] | None:
    return None if identity is None else asdict(identity)


def _switch(parent_fd: int, temporary: str, name: str) -> None:
    os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    os.fsync(parent_fd)


def activate(
    layout: InstallLayout, installation_id: UUID, link: Path, expected: ExpectedLink
) -> ActivationStatus:
    """Record an intent, then conditionally switch one caller-observed CLI symlink."""
    with installation_lock(layout), ExitStack() as resources:
        data = _receipt(layout, installation_id)
        if data.get("activation") is not None:
            _fail("This installation already has an activation intent.")
        root = _checked_release(layout, data, resources)
        parent_fd = _open_parent(link)
        resources.callback(os.close, parent_fd)
        if (
            not _parent_matches(parent_fd, link, expected)
            or _link_identity(parent_fd, link.name) != expected.link
        ):
            _fail("Activation link or parent changed since observation.")
        if expected.link is not None and observe_expected_link(layout, link) != expected:
            _fail("Existing activation target is outside the accepted application base.")
        temporary = f".codex-session-gc-{uuid4()}.tmp"
        os.symlink(str(root / "venv/bin/codex-session-gc"), temporary, dir_fd=parent_fd)
        temp_identity = _link_identity(parent_fd, temporary)
        if temp_identity is None:
            _fail("Temporary activation link disappeared.")
        try:
            intent = {
                "phase": "intent",
                "link": str(link),
                "parent_device": expected.parent_device,
                "parent_inode": expected.parent_inode,
                "old": _link_record(expected.link),
                "new": _link_record(temp_identity),
                "temporary": temporary,
            }
            data["activation"] = intent
            _record(layout, installation_id, data)
            if (
                not _parent_matches(parent_fd, link, expected)
                or _link_identity(parent_fd, link.name) != expected.link
            ):
                _fail("Activation link or parent changed before switch.")
            _checked_release(layout, data, resources)
            if (
                not _parent_matches(parent_fd, link, expected)
                or _link_identity(parent_fd, link.name) != expected.link
                or _link_identity(parent_fd, temporary) != temp_identity
            ):
                _fail("Activation state changed immediately before switch.")
            _switch(parent_fd, temporary, link.name)
            current = _link_identity(parent_fd, link.name)
            if (
                not _parent_matches(parent_fd, link, expected)
                or current is None
                or (current.device, current.inode, current.target)
                != (temp_identity.device, temp_identity.inode, temp_identity.target)
            ):
                _fail("Activation switch result is unknown.")
            intent["phase"] = "active"
            _record(layout, installation_id, data)
            return ActivationStatus.ACTIVE
        finally:
            with suppress(ActivationError, OSError):
                if _link_identity(parent_fd, temporary) == temp_identity:
                    os.unlink(temporary, dir_fd=parent_fd)


def _identity_from_record(value: object) -> LinkIdentity | None:
    if not _valid_link_record(value, optional=True):
        _fail("Activation link identity is malformed.")
    if value is None:
        return None
    if not isinstance(value, dict):
        _fail("Activation link identity is malformed.")
    return LinkIdentity(
        value["device"], value["inode"], value["mtime_ns"], value["ctime_ns"], value["target"]
    )


def _release_intact(layout: InstallLayout, data: dict[str, object]) -> bool:
    try:
        with ExitStack() as resources:
            _checked_release(layout, data, resources)
    except ActivationError, InstallationError, OSError, ValueError:
        return False
    return True


def activation_status(layout: InstallLayout, installation_id: UUID) -> ActivationStatus:
    """Read the saved intent and classify the current symlink without changing it."""
    data = _receipt(layout, installation_id)
    intent = data.get("activation")
    if not isinstance(intent, dict) or set(intent) != {
        "phase",
        "link",
        "parent_device",
        "parent_inode",
        "old",
        "new",
        "temporary",
    }:
        _fail("No well-formed activation intent is recorded.")
    if (
        intent["phase"] not in ("intent", "active", "not_activated")
        or not isinstance(intent["link"], str)
        or type(intent["parent_device"]) is not int
        or type(intent["parent_inode"]) is not int
    ):
        _fail("Activation intent is malformed.")
    link = Path(intent["link"])
    old, new = _identity_from_record(intent["old"]), _identity_from_record(intent["new"])
    if new is None or new.target != f"{data['release']}/venv/bin/codex-session-gc":
        _fail("Activation target does not match its bound installation.")
    try:
        parent_fd = _open_parent(link)
    except ActivationError, OSError:
        return ActivationStatus.UNKNOWN
    try:
        expected = ExpectedLink(intent["parent_device"], intent["parent_inode"], old)
        if not _parent_matches(parent_fd, link, expected):
            return ActivationStatus.UNKNOWN
        try:
            current = _link_identity(parent_fd, link.name)
        except ActivationError, OSError:
            return ActivationStatus.UNKNOWN
        if current == old:
            return ActivationStatus.NOT_ACTIVATED
        if (
            current is not None
            and new is not None
            and (current.device, current.inode, current.target)
            == (new.device, new.inode, new.target)
        ):
            return (
                ActivationStatus.ACTIVE
                if _release_intact(layout, data)
                else ActivationStatus.UNKNOWN
            )
        return ActivationStatus.UNKNOWN
    finally:
        os.close(parent_fd)


def reconcile_activation(layout: InstallLayout, installation_id: UUID) -> ActivationStatus:
    """Record an observed terminal state without retrying or reversing the switch."""
    with installation_lock(layout):
        status = activation_status(layout, installation_id)
        if status is ActivationStatus.UNKNOWN:
            return status
        data = _receipt(layout, installation_id)
        intent = data.get("activation")
        if not isinstance(intent, dict):
            _fail("Activation intent is malformed.")
        temporary, raw_link = intent.get("temporary"), intent.get("link")
        if (
            not isinstance(temporary, str)
            or re.fullmatch(r"\.codex-session-gc-[0-9a-f-]{36}\.tmp", temporary) is None
            or not isinstance(raw_link, str)
        ):
            _fail("Activation temporary identity is malformed.")
        try:
            UUID(temporary.removeprefix(".codex-session-gc-").removesuffix(".tmp"))
        except ValueError as error:
            message = "Activation temporary identity is malformed."
            raise ActivationError(message) from error
        link = Path(raw_link)
        parent_fd = _open_parent(link)
        try:
            expected = ExpectedLink(
                int(intent["parent_device"]),
                int(intent["parent_inode"]),
                _identity_from_record(intent["old"]),
            )
            if not _parent_matches(parent_fd, link, expected):
                return ActivationStatus.UNKNOWN
            try:
                temporary_identity = _link_identity(parent_fd, temporary)
            except ActivationError, OSError:
                return ActivationStatus.UNKNOWN
            new = _identity_from_record(intent["new"])
            if temporary_identity is not None:
                if temporary_identity != new:
                    return ActivationStatus.UNKNOWN
                os.unlink(temporary, dir_fd=parent_fd)
                os.fsync(parent_fd)
            intent["phase"] = status.value
            _record(layout, installation_id, data)
            return status
        finally:
            os.close(parent_fd)


def mcp_argv(
    layout: InstallLayout, installation_id: UUID, home: Path, receipt_dir: Path
) -> tuple[str, ...]:
    """Return a verified versioned MCP command without changing client configuration."""
    if not home.is_absolute() or not receipt_dir.is_absolute():
        _fail("MCP paths must be absolute.")
    if activation_status(layout, installation_id) is not ActivationStatus.ACTIVE:
        _fail("The installation is not currently activated.")
    with ExitStack() as resources:
        release = _checked_release(layout, _receipt(layout, installation_id), resources)
        return (
            str(release / "venv/bin/codex-session-gc"),
            "mcp",
            "--home",
            str(home),
            "--helper",
            str(release / "codex-session-gc-native"),
            "--receipt-dir",
            str(receipt_dir),
            "--support-install-dir",
            str(layout.base),
            "--support-installation-id",
            str(installation_id),
        )
