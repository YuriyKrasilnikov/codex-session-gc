# Copyright (c) 2026

"""Serialize installation into an absent, final, unactivated version root."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager, suppress
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID

from codex_session_gc.installation.model import (
    InstallBinding,
    InstallLayout,
    InstallOptions,
    InstallReceipt,
    InstallState,
)
from codex_session_gc.installation.payload import Payload, extract_payload
from codex_session_gc.installation.process import StorageBudget, run_owned

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from codex_session_gc.installation.provenance import VerifiedArtifact


class InstallationError(Exception):
    """Installation was refused or failed without activating a version."""


def _fail(message: str) -> NoReturn:
    raise InstallationError(message)


def _owned_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        message = "Installation directory is unavailable."
        raise InstallationError(message) from error
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        _fail("Installation directory is not owner-controlled.")


def _ensure_directory(path: Path) -> None:
    with suppress(FileExistsError):
        path.mkdir(mode=0o700)
    _owned_directory(path)


@contextmanager
def _lock(layout: InstallLayout) -> Iterator[None]:
    _owned_directory(layout.base)
    if layout.base.resolve(strict=True) != layout.base:
        _fail("Installation base must not traverse aliases.")
    fd = os.open(layout.lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            _fail("Installation lock is not owner-controlled.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            message = "Another installation owns the lock."
            raise InstallationError(message) from error
        yield
    finally:
        os.close(fd)


def _write_receipt(fd: int, receipt: InstallReceipt) -> None:
    data = asdict(receipt)
    data["installation_id"] = str(receipt.installation_id)
    data["state"] = receipt.state.value
    data["release"] = None if receipt.release is None else str(receipt.release)
    encoded = (json.dumps(data, sort_keys=True) + "\n").encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    with os.fdopen(os.dup(fd), "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _unique_receipt(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("Installation receipt has duplicate fields.")
        result[key] = value
    return result


def _require_settled_receipts(layout: InstallLayout) -> None:
    for path in layout.receipts.iterdir():
        _validate_receipt(layout, path)


def _read_receipt(path: Path) -> object:
    max_receipt_bytes = 65536
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_size > max_receipt_bytes
            ):
                _fail("Installation receipt is not a bounded owned regular file.")
            data = json.load(stream, object_pairs_hook=_unique_receipt)
    except (OSError, ValueError) as error:
        message = "Installation receipt is unresolved."
        raise InstallationError(message) from error
    return data


def _valid_link_record(value: object, *, optional: bool) -> bool:
    if value is None:
        return optional
    return (
        isinstance(value, dict)
        and set(value) == {"device", "inode", "mtime_ns", "ctime_ns", "target"}
        and all(type(value[key]) is int for key in ("device", "inode", "mtime_ns", "ctime_ns"))
        and isinstance(value["target"], str)
    )


def _valid_settled_activation(value: object, release: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "phase",
        "link",
        "parent_device",
        "parent_inode",
        "old",
        "new",
        "temporary",
    }:
        return False
    return (
        value["phase"] in ("active", "not_activated")
        and isinstance(value["link"], str)
        and value["link"].startswith("/")
        and type(value["parent_device"]) is int
        and type(value["parent_inode"]) is int
        and isinstance(value["temporary"], str)
        and re.fullmatch(r"\.codex-session-gc-[0-9a-f-]{36}\.tmp", value["temporary"]) is not None
        and _valid_link_record(value["old"], optional=True)
        and _valid_link_record(value["new"], optional=False)
        and isinstance(value["new"], dict)
        and isinstance(release, str)
        and value["new"]["target"] == f"{release}/venv/bin/codex-session-gc"
    )


def _validate_binding_and_activation(data: dict[str, object]) -> None:
    binding = data["binding"]
    if data.get("state") == InstallState.COMPLETED.value:
        if (
            not isinstance(binding, dict)
            or set(binding) != {"manifest_sha256", "launcher_sha256", "launcher_size"}
            or not all(
                isinstance(binding.get(key), str)
                and re.fullmatch(r"[a-f0-9]{64}", str(binding[key])) is not None
                for key in ("manifest_sha256", "launcher_sha256")
            )
            or type(binding.get("launcher_size")) is not int
            or binding["launcher_size"] < 0
        ):
            _fail("Completed installation receipt lacks a valid binding.")
    elif binding is not None:
        _fail("Incomplete installation receipt has an unexpected binding.")
    if "activation" in data and (
        data.get("state") != InstallState.COMPLETED.value
        or not _valid_settled_activation(data["activation"], data.get("release"))
    ):
        _fail("An activation intent is unresolved.")


def _validate_receipt(layout: InstallLayout, path: Path) -> None:
    if path.suffix != ".json":
        _fail("Unresolved installation receipt path.")
    data = _read_receipt(path)
    if not isinstance(data, dict) or set(data) not in (
        {"installation_id", "state", "version", "release", "binding"},
        {"installation_id", "state", "version", "release", "binding", "activation"},
    ):
        _fail("Installation receipt is unresolved.")
    _validate_binding_and_activation(data)
    try:
        identifier = UUID(data["installation_id"])
    except (ValueError, TypeError, AttributeError) as error:
        message = "Installation receipt identity is invalid."
        raise InstallationError(message) from error
    if str(identifier) != path.stem or data["state"] not in {
        InstallState.COMPLETED.value,
        InstallState.FAILED.value,
    }:
        _fail("An earlier installation receipt is unresolved.")
    version, release = data["version"], data["release"]
    if release is None and data["state"] == InstallState.FAILED.value:
        if version is not None and (
            not isinstance(version, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version) is None
            or ".." in version
        ):
            _fail("Installation receipt version is invalid.")
        return
    if (
        not isinstance(version, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version) is None
        or ".." in version
        or release != str(layout.releases / version)
    ):
        _fail("Installation receipt release path is unresolved.")
    release_path = layout.releases / version
    path_exists = os.path.lexists(release_path)
    if path_exists != (data["state"] == InstallState.COMPLETED.value):
        _fail("Installation receipt release state is unresolved.")
    if path_exists:
        _owned_directory(release_path)


def _trusted_executable(path: Path) -> None:
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        _fail("A trusted executable must be an existing absolute executable file.")


def _remove_owned_root(root: Path, identity: tuple[int, int]) -> None:
    info = root.lstat()
    if (info.st_dev, info.st_ino) != identity or not stat.S_ISDIR(info.st_mode):
        _fail("Owned installation root changed before cleanup.")
    shutil.rmtree(root)


def _environment_capacity(root: Path, reserve: int) -> None:
    if type(reserve) is not int or reserve < 0:
        _fail("Environment storage reserve is invalid.")
    volume = os.statvfs(root)
    if volume.f_bavail * volume.f_frsize < reserve:
        _fail("Insufficient environment installation capacity.")


def _environment(root: Path, reserve: int) -> tuple[dict[str, str], Path]:
    _environment_capacity(root, reserve)
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if key in os.environ
    }
    temporary = root / ".install-tmp"
    temporary.mkdir(mode=0o700)
    for key in ("TMPDIR", "HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "UV_CACHE_DIR"):
        environment[key] = str(temporary)
    return environment, temporary


def _environment_budget(
    root: Path, options: InstallOptions
) -> tuple[dict[str, str], Path, StorageBudget]:
    budget = StorageBudget.capture(root, options.environment_reserve_bytes, options.reserve_bytes)
    environment, temporary = _environment(
        root, options.environment_reserve_bytes + options.reserve_bytes
    )
    return environment, temporary, budget


def _hash_owned_file(path: Path) -> tuple[str, int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _fail("Installation binding requires a regular file.")
        digest = hashlib.sha256()
        offset = 0
        while data := os.pread(fd, 1024 * 1024, offset):
            digest.update(data)
            offset += len(data)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or offset != before.st_size:
            _fail("Installation binding file changed while read.")
        return digest.hexdigest(), offset
    finally:
        os.close(fd)


def _finish_environment(
    payload: Payload, temporary: Path, lease: VerifiedArtifact
) -> InstallBinding:
    cli = payload.root / "venv/bin/codex-session-gc"
    if not cli.is_file() or cli.is_symlink() or not os.access(cli, os.X_OK):
        _fail("Installed CLI entry point is unavailable.")
    shutil.rmtree(temporary)
    manifest_digest, _ = _hash_owned_file(payload.root / "manifest.json")
    if manifest_digest != payload.manifest_sha256:
        _fail("Installed manifest changed after verified extraction.")
    launcher_digest, launcher_size = _hash_owned_file(cli)
    lease.validate()
    return InstallBinding(manifest_digest, launcher_digest, launcher_size)


def _validate_options(options: InstallOptions, installation_id: UUID) -> None:
    for executable in (options.trusted_uv, options.trusted_python):
        _trusted_executable(executable)
    if type(installation_id) is not UUID:
        _fail("Installation identity must be a UUID.")


async def install(
    lease: VerifiedArtifact,
    layout: InstallLayout,
    installation_id: UUID,
    options: InstallOptions,
) -> InstallReceipt:
    """Create a verified final-path environment without switching any active link."""
    _validate_options(options, installation_id)
    with _lock(layout):
        _ensure_directory(layout.releases)
        _ensure_directory(layout.receipts)
        _require_settled_receipts(layout)
        receipt_path = layout.receipts / f"{installation_id}.json"
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        receipt = InstallReceipt(installation_id, InstallState.STARTED, None, None)
        owned_root: Path | None = None
        owned_identity: tuple[int, int] | None = None
        try:
            _write_receipt(fd, receipt)

            def create(version: str) -> Path:
                nonlocal owned_root, owned_identity, receipt
                path = layout.releases / version
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError as error:
                    message = "Version root already exists and cannot be adopted."
                    raise InstallationError(message) from error
                info = path.lstat()
                owned_root, owned_identity = path, (info.st_dev, info.st_ino)
                receipt = replace(receipt, version=version, release=path)
                _write_receipt(fd, receipt)
                return path

            payload = extract_payload(
                lease,
                create,
                reserve_bytes=options.reserve_bytes,
                expected_version=options.expected_version,
            )
            receipt = replace(receipt, state=InstallState.PAYLOAD_VERIFIED)
            _write_receipt(fd, receipt)
            (payload.root / "codex-session-gc-native").chmod(0o700, follow_symlinks=False)
            environment, temporary, budget = _environment_budget(payload.root, options)
            limits = replace(options.process_limits, storage=budget)
            await run_owned(
                [
                    str(options.trusted_uv),
                    "venv",
                    "--no-config",
                    "--no-cache",
                    "--no-project",
                    "--no-python-downloads",
                    "--python",
                    str(options.trusted_python),
                    str(payload.root / "venv"),
                ],
                cwd=payload.root,
                environment=environment,
                limits=limits,
            )
            _trusted_executable(payload.root / "venv/bin/python")
            receipt = replace(receipt, state=InstallState.ENVIRONMENT_READY)
            _write_receipt(fd, receipt)
            await run_owned(
                [
                    str(options.trusted_uv),
                    "pip",
                    "install",
                    "--no-config",
                    "--no-cache",
                    "--python",
                    str(payload.root / "venv/bin/python"),
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "-r",
                    "install-requirements.txt",
                ],
                cwd=payload.root,
                environment=environment,
                limits=limits,
            )
            binding = _finish_environment(payload, temporary, lease)
            receipt = replace(receipt, state=InstallState.COMPLETED, binding=binding)
            _write_receipt(fd, receipt)
        except BaseException:
            if owned_root is not None and owned_identity is not None:
                _remove_owned_root(owned_root, owned_identity)
            receipt = replace(receipt, state=InstallState.FAILED, release=None, binding=None)
            _write_receipt(fd, receipt)
            raise
        else:
            return receipt
        finally:
            os.close(fd)
