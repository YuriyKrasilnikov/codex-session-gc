# Copyright (c) 2026

"""Installation paths, identity and explicit lifecycle outcomes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from codex_session_gc.installation.process import ProcessLimits


class InstallState(StrEnum):
    """A version is staged without activation or cleanup of existing versions."""

    STARTED = "started"
    PAYLOAD_VERIFIED = "payload_verified"
    ENVIRONMENT_READY = "environment_ready"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class InstallLayout:
    """Owner-controlled application directory and its managed installation locations."""

    base: Path

    def __post_init__(self) -> None:
        """Require an explicit absolute directory; its ownership is checked on use."""
        if not self.base.is_absolute():
            message = "Installation base must be absolute."
            raise ValueError(message)

    @property
    def releases(self) -> Path:
        """Return final version roots."""
        return self.base / "releases"

    @property
    def receipts(self) -> Path:
        """Return installation receipts, separate from runtime cleanup receipts."""
        return self.base / "installations"

    @property
    def lock(self) -> Path:
        """Return the installation coordinator lock."""
        return self.base / ".install.lock"


@dataclass(frozen=True, slots=True)
class InstallBinding:
    """Exact manifest and launcher identity observed at installation completion."""

    manifest_sha256: str
    launcher_sha256: str
    launcher_size: int


@dataclass(frozen=True, slots=True)
class InstallReceipt:
    """Durable installation identity and last recorded phase."""

    installation_id: UUID
    state: InstallState
    version: str | None
    release: Path | None
    binding: InstallBinding | None = None


@dataclass(frozen=True, slots=True)
class InstallOptions:
    """Explicit trusted executables and resource bounds for installation."""

    trusted_uv: Path
    trusted_python: Path
    reserve_bytes: int
    environment_reserve_bytes: int
    process_limits: ProcessLimits
    expected_version: str

    def __post_init__(self) -> None:
        """Bind installation to one stable release version before root creation."""
        if (
            not isinstance(self.expected_version, str)
            or re.fullmatch(
                r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", self.expected_version
            )
            is None
        ):
            message = "A stable X.Y.Z expected version is required."
            raise ValueError(message)
