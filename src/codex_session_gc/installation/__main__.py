# Copyright (c) 2026

"""Resolve, verify and install one release without changing Codex configuration."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import platform
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from uuid import UUID, uuid4

from codex_session_gc.artifacts.bundle import MANIFEST_MAX_BYTES, BundleManifestError
from codex_session_gc.installation.activation import (
    ActivationError,
    ActivationStatus,
    activate,
    activation_status,
    mcp_argv,
    observe_expected_link,
    reconcile_activation,
)
from codex_session_gc.installation.discovery import DiscoveryError, discover_release
from codex_session_gc.installation.download import DownloadError, HttpsTransport, download_asset
from codex_session_gc.installation.model import InstallLayout, InstallOptions
from codex_session_gc.installation.payload import PayloadRefusalError
from codex_session_gc.installation.process import OwnedProcessError, ProcessLimits
from codex_session_gc.installation.provenance import (
    ProvenanceRefusalError,
    VerifierPreflightError,
    preflight_gh,
    verify_artifact,
)
from codex_session_gc.installation.transaction import (
    InstallationError,
    _owned_directory,
    _read_receipt,
    _validate_receipt,
    install,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from codex_session_gc.installation.activation import ExpectedLink
    from codex_session_gc.installation.download import Transport

_OPERATION_SECONDS = 6 * 60 * 60
_NETWORK_SECONDS = 120
_OUTPUT_BYTES = 1024 * 1024


class InstallerError(Exception):
    """The requested installation cannot safely proceed."""


class Action(StrEnum):
    """The explicitly requested installation lifecycle endpoint."""

    ACTIVATE = "activate"
    STAGE = "stage"
    STATUS = "status"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class Limits:
    """Caller-selected bounds; absent disk overrides use fresh filesystem fractions."""

    operation_seconds: float = _OPERATION_SECONDS
    network_seconds: float = _NETWORK_SECONDS
    metadata_bytes: int = MANIFEST_MAX_BYTES
    output_bytes: int = _OUTPUT_BYTES
    reserve_bytes: int | None = None
    environment_bytes: int | None = None
    staging_reserve_bytes: int | None = None
    max_asset_bytes: int | None = None

    def __post_init__(self) -> None:
        """Reject nonfinite, nonpositive or ambiguous resource bounds."""
        for value in (self.operation_seconds, self.network_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                _fail("Duration bounds must be finite and positive.")
        for value in (self.metadata_bytes, self.output_bytes):
            if type(value) is not int or value <= 0:
                _fail("Byte bounds must be positive integers.")
        for optional_value in (
            self.reserve_bytes,
            self.environment_bytes,
            self.staging_reserve_bytes,
            self.max_asset_bytes,
        ):
            if optional_value is not None and (
                type(optional_value) is not int or optional_value <= 0
            ):
                _fail("Disk overrides must be positive integers.")


@dataclass(frozen=True, slots=True)
class Tools:
    """Independently trusted absolute executables selected by the caller or environment."""

    gh: Path
    uv: Path
    python: Path


@dataclass(frozen=True, slots=True)
class Request:
    """An explicit selected release and installation destination."""

    release: str
    action: Action
    layout: InstallLayout
    bin_link: Path
    expected_current: Path | None
    home: Path | None
    receipt_dir: Path | None
    tools: Tools | None
    limits: Limits
    installation_id: UUID | None = None


def _fail(message: str) -> NoReturn:
    raise InstallerError(message)


def _tool(explicit: Path | None, name: str) -> Path:
    raw = str(explicit) if explicit is not None else shutil.which(name)
    if not raw:
        _fail(f"A separately trusted {name} executable is required.")
    path = Path(raw)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        _fail(f"The separately trusted {name} executable must be an absolute executable file.")
    return path.resolve(strict=True)


def _ensure_directory(path: Path) -> None:
    """Create missing path components without traversing symlinks."""
    if not path.is_absolute():
        _fail("Installation directories must be absolute.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            if component in ("", ".", ".."):
                _fail("Installation directory has an ambiguous component.")
            with contextlib.suppress(FileExistsError):
                os.mkdir(component, 0o700, dir_fd=fd)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            _fail("Installation directory is not owned by the current user.")
    finally:
        os.close(fd)


def _managed_link(
    layout: InstallLayout, link: Path, expected: ExpectedLink, home: Path, receipt_dir: Path
) -> bool:
    """Recognize only an observed link bound to a valid settled active receipt."""
    if expected.link is None or not os.path.lexists(layout.receipts):
        return False
    _owned_directory(layout.receipts)
    for path in layout.receipts.iterdir():
        _validate_receipt(layout, path)
        try:
            identifier = UUID(path.stem)
        except ValueError as error:
            message = "An installation receipt has an invalid identity."
            raise InstallerError(message) from error
        record = _read_receipt(path)
        if not isinstance(record, dict):
            _fail("An installation receipt is invalid.")
        activation = record.get("activation")
        if (
            not isinstance(activation, dict)
            or activation.get("phase") != "active"
            or activation.get("link") != str(link)
        ):
            continue
        if activation_status(layout, identifier) is ActivationStatus.ACTIVE:
            target = Path(expected.link.target)
            if not target.is_absolute():
                target = link.parent / target
            return target.resolve(strict=True) == Path(
                mcp_argv(layout, identifier, home, receipt_dir)[0]
            )
    return False


def _expected(request: Request) -> ExpectedLink:
    if request.home is None or request.receipt_dir is None:
        _fail("Activation requires explicit --home and --receipt-dir paths.")
    _ensure_directory(request.bin_link.parent)
    expected = observe_expected_link(request.layout, request.bin_link)
    if expected.link is None:
        if request.expected_current is not None:
            _fail("An expected current launcher was supplied but the link is absent.")
        return expected
    target = Path(expected.link.target)
    if not target.is_absolute():
        target = request.bin_link.parent / target
    target = target.resolve(strict=True)
    if request.expected_current is not None:
        if (
            not request.expected_current.is_absolute()
            or request.expected_current.resolve(strict=True) != target
        ):
            _fail("The current launcher differs from --expected-current.")
    elif not _managed_link(
        request.layout, request.bin_link, expected, request.home, request.receipt_dir
    ):
        _fail("An unmanaged current link requires --expected-current.")
    return expected


def _free(path: Path) -> int:
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def _recover(request: Request) -> dict[str, object]:
    if type(request.installation_id) is not UUID:
        _fail("Status and reconciliation require --installation-id.")
    if request.action is Action.STATUS:
        _owned_directory(request.layout.base)
        _owned_directory(request.layout.receipts)
        path = request.layout.receipts / f"{request.installation_id}.json"
        record = _read_receipt(path)
        if isinstance(record, dict) and "activation" not in record:
            _validate_receipt(request.layout, path)
            if record.get("state") != "completed":
                _fail("Installation is not completed.")
            return {"status": "staged", "installation_id": str(request.installation_id)}
        status = activation_status(request.layout, request.installation_id)
    else:
        status = reconcile_activation(request.layout, request.installation_id)
    return {"status": status.value, "installation_id": str(request.installation_id)}


def _activate_saved(request: Request) -> dict[str, object]:
    identifier = request.installation_id
    if type(identifier) is not UUID or request.home is None or request.receipt_dir is None:
        _fail("Existing activation requires an installation ID and MCP paths.")
    _owned_directory(request.layout.base)
    _owned_directory(request.layout.receipts)
    path = request.layout.receipts / f"{identifier}.json"
    record = _read_receipt(path)
    if not isinstance(record, dict) or "activation" in record:
        _fail("An existing activation intent cannot be dispatched again; use status or reconcile.")
    _validate_receipt(request.layout, path)
    if record.get("state") != "completed":
        _fail("Only a completed installation can be activated.")
    expected = _expected(request)
    sys.stderr.write(f"Installation ID: {identifier}; release: {record['release']}\n")
    try:
        activate(request.layout, identifier, request.bin_link, expected)
    except (ActivationError, InstallationError, OSError, ValueError, KeyboardInterrupt) as error:
        message = (
            f"Installation {identifier} is staged at {record['release']}; "
            "activation may require reconciliation before retry."
        )
        raise InstallerError(message) from error
    return {
        "status": "active",
        "version": record["version"],
        "installation_id": str(identifier),
        "release": record["release"],
        "mcp_argv": mcp_argv(request.layout, identifier, request.home, request.receipt_dir),
    }


def _validate_request_context(request: Request) -> None:
    if type(request.action) is not Action:
        _fail("Installation action is invalid.")
    if sys.platform != "linux" or platform.machine() != "x86_64":
        _fail("Only linux-x86_64 release assets are supported.")
    if request.action is Action.ACTIVATE and (
        request.home is None
        or request.receipt_dir is None
        or not request.home.is_absolute()
        or not request.home.is_dir()
        or not request.receipt_dir.is_absolute()
    ):
        _fail("Activation requires an existing absolute --home and an absolute --receipt-dir.")


async def execute(request: Request, transport: Transport) -> dict[str, object]:
    """Verify and stage one release, optionally switching an explicitly observed CLI link."""
    _validate_request_context(request)
    if request.action in (Action.STATUS, Action.RECONCILE):
        return _recover(request)
    if request.installation_id is not None:
        if request.action is not Action.ACTIVATE:
            _fail("An installation ID can only be used for activation, status or reconciliation.")
        return _activate_saved(request)
    if request.tools is None:
        _fail("Installation requires separately trusted executables.")
    with tempfile.TemporaryDirectory(prefix="codex-gc-download-") as temporary:
        staging = Path(temporary)
        try:
            await preflight_gh(
                request.tools.gh,
                staging,
                ProcessLimits(request.limits.network_seconds, request.limits.output_bytes),
            )
        except VerifierPreflightError as error:
            raise InstallerError(str(error)) from error
        release = await discover_release(
            transport,
            request.release,
            max_metadata_bytes=request.limits.metadata_bytes,
            network_seconds=request.limits.network_seconds,
        )
        _ensure_directory(request.layout.base)
        expected = _expected(request) if request.action is Action.ACTIVATE else None
        staging_free = _free(staging)
        staging_reserve = request.limits.staging_reserve_bytes or staging_free // 2
        if release.artifact.size > staging_free - staging_reserve or (
            request.limits.max_asset_bytes is not None
            and release.artifact.size > request.limits.max_asset_bytes
        ):
            _fail("Insufficient staging capacity for the selected asset.")
        artifact = staging / "release.tar.gz"
        await download_asset(
            transport,
            release.asset_url,
            release.artifact,
            artifact,
            deadline=asyncio.get_running_loop().time() + request.limits.network_seconds,
        )
        with await verify_artifact(
            artifact, release.artifact, release.source, trusted_gh=request.tools.gh
        ) as lease:
            available = _free(request.layout.base)
            reserve = request.limits.reserve_bytes or available // 2
            environment = request.limits.environment_bytes or available // 4
            options = InstallOptions(
                request.tools.uv,
                request.tools.python,
                reserve,
                environment,
                ProcessLimits(request.limits.operation_seconds, request.limits.output_bytes),
                release.version,
            )
            identifier = uuid4()
            receipt = await install(lease, request.layout, identifier, options)
        sys.stderr.write(f"Installation ID: {identifier}; release: {receipt.release}\n")
        result: dict[str, object] = {
            "status": "staged",
            "version": release.version,
            "installation_id": str(identifier),
            "release": str(receipt.release),
        }
        if request.action is Action.ACTIVATE:
            if expected is None or request.home is None or request.receipt_dir is None:
                _fail("Activation context is incomplete.")
            try:
                activate(request.layout, identifier, request.bin_link, expected)
            except (
                ActivationError,
                InstallationError,
                OSError,
                ValueError,
                KeyboardInterrupt,
            ) as error:
                message = (
                    f"Installation {identifier} is staged at {receipt.release}; "
                    "activation may require reconciliation before retry."
                )
                raise InstallerError(message) from error
            result["status"] = "active"
            result["mcp_argv"] = mcp_argv(
                request.layout, identifier, request.home, request.receipt_dir
            )
        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="latest", help="Stable release tag, or latest")
    parser.add_argument("--action", choices=list(Action), type=Action, default=Action.ACTIVATE)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--receipt-dir", type=Path)
    parser.add_argument("--expected-current", type=Path)
    parser.add_argument("--installation-id", type=UUID)
    parser.add_argument(
        "--install-dir", type=Path, default=Path.home() / ".local/share/codex-session-gc"
    )
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument("--gh", type=Path)
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--operation-seconds", type=float, default=_OPERATION_SECONDS)
    parser.add_argument("--network-seconds", type=float, default=_NETWORK_SECONDS)
    parser.add_argument("--metadata-bytes", type=int, default=MANIFEST_MAX_BYTES)
    parser.add_argument("--output-bytes", type=int, default=_OUTPUT_BYTES)
    parser.add_argument("--reserve-bytes", type=int)
    parser.add_argument("--environment-bytes", type=int)
    parser.add_argument("--staging-reserve-bytes", type=int)
    parser.add_argument("--max-asset-bytes", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the shared installed or zipapp installer entry point."""
    arguments = _parser().parse_args(argv)
    try:
        limits = Limits(
            arguments.operation_seconds,
            arguments.network_seconds,
            arguments.metadata_bytes,
            arguments.output_bytes,
            arguments.reserve_bytes,
            arguments.environment_bytes,
            arguments.staging_reserve_bytes,
            arguments.max_asset_bytes,
        )
        tools = None
        existing_action = (
            arguments.action is Action.ACTIVATE and arguments.installation_id is not None
        )
        if arguments.action not in (Action.STATUS, Action.RECONCILE) and not existing_action:
            tools = Tools(
                _tool(arguments.gh, "gh"),
                _tool(arguments.uv, "uv"),
                _tool(arguments.python, "python"),
            )
            sys.stderr.write(
                f"Using local executables (trust them separately): gh={tools.gh}, uv={tools.uv}, "
                f"python={tools.python}\n"
            )
        request = Request(
            arguments.release,
            arguments.action,
            InstallLayout(arguments.install_dir),
            arguments.bin_dir / "codex-session-gc",
            arguments.expected_current,
            arguments.home,
            arguments.receipt_dir,
            tools,
            limits,
            arguments.installation_id,
        )
        result = asyncio.run(execute(request, HttpsTransport()))
    except (
        InstallerError,
        DiscoveryError,
        DownloadError,
        OwnedProcessError,
        ProvenanceRefusalError,
        InstallationError,
        PayloadRefusalError,
        ActivationError,
        BundleManifestError,
        OSError,
        ValueError,
        TimeoutError,
        KeyboardInterrupt,
    ) as error:
        sys.stderr.write(f"Installation refused: {error}\n")
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
