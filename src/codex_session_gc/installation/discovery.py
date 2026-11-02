# Copyright (c) 2026

"""Resolve one immutable public release asset and its tag's commit."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity
from codex_session_gc.installation.provenance import REPOSITORY

if TYPE_CHECKING:
    from codex_session_gc.installation.download import Transport

_API = f"https://api.github.com/repos/{REPOSITORY}"


class DiscoveryError(Exception):
    """Release metadata is ambiguous, unsupported or not bound to a tag commit."""


@dataclass(frozen=True, slots=True)
class Release:
    """One resolved version, artifact identity, download URL and source identity."""

    tag: str
    version: str
    asset_url: str
    artifact: ArtifactIdentity
    source: SourceIdentity


def _fail(message: str) -> NoReturn:
    raise DiscoveryError(message)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("Release metadata contains duplicate keys.")
        result[key] = value
    return result


async def fetch_metadata(
    transport: Transport, url: str, limit: int, network_seconds: float
) -> dict[str, object]:
    """Fetch one bounded JSON object using the caller's explicit network deadline."""
    deadline = asyncio.get_running_loop().time() + network_seconds
    data = bytearray()

    def collect(block: bytes) -> None:
        data.extend(block)
        if len(data) > limit:
            _fail("Release metadata exceeds its byte bound.")

    async with asyncio.timeout_at(deadline):
        await transport.get(url, limit=limit, deadline=deadline, sink=collect)
    try:
        value: object = json.loads(data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as error:
        message = "Release metadata is not valid JSON."
        raise DiscoveryError(message) from error
    if not isinstance(value, dict):
        _fail("Release metadata must be an object.")
    return value


def _tag(value: object) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value) is None
    ):
        _fail("A stable vX.Y.Z release tag is required.")
    return value, value[1:]


def _asset(metadata: dict[str, object], tag: str, version: str) -> tuple[str, ArtifactIdentity]:
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        _fail("Release assets are unavailable.")
    name = f"codex-session-gc-{version}-linux-x86_64.tar.gz"
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        _fail("Release must contain exactly one platform bundle.")
    asset = matches[0]
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    digest = asset.get("digest")
    size = asset.get("size")
    if (
        asset.get("browser_download_url") != expected_url
        or asset.get("state") != "uploaded"
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or type(size) is not int
        or size <= 0
    ):
        _fail("Release asset URL, state, digest or size is invalid.")
    return expected_url, ArtifactIdentity(digest.removeprefix("sha256:"), size)


async def _commit(transport: Transport, tag: str, limit: int, network_seconds: float) -> str:
    reference = await fetch_metadata(
        transport, f"{_API}/git/ref/tags/{tag}", limit, network_seconds
    )
    if reference.get("ref") != f"refs/tags/{tag}":
        _fail("Resolved Git reference does not match the release tag.")
    obj = reference.get("object")
    for depth in range(6):
        if not isinstance(obj, dict) or obj.get("type") not in ("commit", "tag"):
            _fail("Git tag does not resolve to a commit.")
        sha = obj.get("sha")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            _fail("Git tag object identity is invalid.")
        if obj["type"] == "commit":
            return sha
        annotated = await fetch_metadata(
            transport, f"{_API}/git/tags/{sha}", limit, network_seconds
        )
        if annotated.get("sha") != sha or (depth == 0 and annotated.get("tag") != tag):
            _fail("Annotated Git tag identity differs.")
        obj = annotated.get("object")
    _fail("Git tag nesting exceeds its bound.")


async def discover_release(
    transport: Transport, selector: str, *, max_metadata_bytes: int, network_seconds: float
) -> Release:
    """Resolve a stable release once, without trusting its target_commitish field."""
    if type(max_metadata_bytes) is not int or max_metadata_bytes <= 0:
        _fail("Release metadata byte bound must be positive.")
    if type(network_seconds) not in (int, float) or not 0 < network_seconds < float("inf"):
        _fail("Network duration must be finite and positive.")
    if selector == "latest":
        url = f"{_API}/releases/latest"
    else:
        tag, _ = _tag(selector)
        url = f"{_API}/releases/tags/{tag}"
    metadata = await fetch_metadata(transport, url, max_metadata_bytes, network_seconds)
    tag, version = _tag(metadata.get("tag_name"))
    if selector not in ("latest", tag):
        _fail("Release metadata tag does not match the requested tag.")
    if metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        _fail("Only published stable releases are supported.")
    asset_url, artifact = _asset(metadata, tag, version)
    commit = await _commit(transport, tag, max_metadata_bytes, network_seconds)
    return Release(tag, version, asset_url, artifact, SourceIdentity(commit, "refs/heads/main"))
