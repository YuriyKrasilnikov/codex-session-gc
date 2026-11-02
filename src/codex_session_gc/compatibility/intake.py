# Copyright (c) 2026

"""Explicit verified intake of one content-addressed support record."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity
from codex_session_gc.artifacts.bundle import MANIFEST_MAX_BYTES
from codex_session_gc.compatibility.record import MAX_RECORD_BYTES
from codex_session_gc.compatibility.store import (
    SupportIntakeReceipt,
    SupportStoreError,
    promote_support,
)
from codex_session_gc.installation.discovery import fetch_metadata
from codex_session_gc.installation.download import download_asset
from codex_session_gc.installation.process import ProcessLimits
from codex_session_gc.installation.provenance import (
    REPOSITORY,
    VERIFIER_OUTPUT_LIMIT,
    preflight_gh,
    verify_artifact,
)

if TYPE_CHECKING:
    from codex_session_gc.installation.download import Transport
    from codex_session_gc.installation.model import InstallLayout


@dataclass(frozen=True, slots=True)
class SupportIntakeRequest:
    """Caller-selected identities and trusted verifier for explicit intake."""

    layout: InstallLayout
    installation_id: UUID
    asset_id: int
    expected: ArtifactIdentity
    gc_source: SourceIdentity
    trusted_gh: Path
    network_seconds: float


async def intake_support(
    request: SupportIntakeRequest, transport: Transport
) -> SupportIntakeReceipt:
    """Verify explicitly selected release bytes, then promote the same descriptor lease."""
    if (
        type(request.installation_id) is not UUID
        or type(request.asset_id) is not int
        or request.asset_id <= 0
        or type(request.expected) is not ArtifactIdentity
        or not 0 < request.expected.size <= MAX_RECORD_BYTES
        or type(request.gc_source) is not SourceIdentity
        or request.gc_source.ref != "refs/heads/main"
        or not request.trusted_gh.is_absolute()
    ):
        message = "Invalid explicit support intake identity."
        raise SupportStoreError(message)
    limits = ProcessLimits(request.network_seconds, VERIFIER_OUTPUT_LIMIT)
    with tempfile.TemporaryDirectory(prefix="gc-support-intake-") as temporary:
        directory = Path(temporary)
        await preflight_gh(request.trusted_gh, directory, limits)
        metadata = await fetch_metadata(
            transport,
            f"https://api.github.com/repos/{REPOSITORY}/releases/assets/{request.asset_id}",
            MANIFEST_MAX_BYTES,
            limits.seconds,
        )
        url = metadata.get("browser_download_url")
        if (
            metadata.get("id") != request.asset_id
            or type(metadata.get("id")) is not int
            or metadata.get("state") != "uploaded"
            or metadata.get("size") != request.expected.size
            or type(metadata.get("size")) is not int
            or metadata.get("digest") != f"sha256:{request.expected.sha256}"
            or not isinstance(url, str)
        ):
            message = "Support release asset metadata does not match its identity."
            raise SupportStoreError(message)
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or not parsed.path.startswith(f"/{REPOSITORY}/releases/download/")
            or parsed.query
            or parsed.fragment
        ):
            message = "Support asset URL is outside the fixed repository."
            raise SupportStoreError(message)
        destination = directory / "support.json"
        deadline = asyncio.get_running_loop().time() + limits.seconds
        await download_asset(transport, url, request.expected, destination, deadline=deadline)
        lease = await verify_artifact(
            destination, request.expected, request.gc_source, trusted_gh=request.trusted_gh
        )
        with lease:
            return promote_support(request.layout, request.installation_id, lease, request.asset_id)
