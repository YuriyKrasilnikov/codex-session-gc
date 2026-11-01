# Copyright (c) 2026

"""Bounded HTTPS acquisition into caller-owned staging storage."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn, Protocol
from urllib.parse import urljoin, urlsplit

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from codex_session_gc.artifacts import ArtifactIdentity

_ALLOWED_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)
_CHUNK = 65536


class DownloadError(Exception):
    """Release metadata or asset bytes could not be acquired safely."""


class Transport(Protocol):
    """Fetch an HTTPS response into a bounded caller-owned byte sink."""

    async def get(
        self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
    ) -> None:
        """Transfer one response before the monotonic deadline."""
        ...


def _fail(message: str) -> NoReturn:
    raise DownloadError(message)


def _target(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        message = "Release URL is malformed."
        raise DownloadError(message) from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or not parsed.path.startswith("/")
        or any(char in url for char in "\r\n")
    ):
        _fail("Release URL is outside the trusted HTTPS hosts.")
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    try:
        target.encode("ascii")
    except UnicodeEncodeError as error:
        message = "Release URL must use ASCII encoding."
        raise DownloadError(message) from error
    if parsed.hostname is None:
        _fail("Release URL has no host.")
    return str(parsed.hostname), target


async def _headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    raw = await reader.readuntil(b"\r\n\r\n")
    if len(raw) > _CHUNK:
        _fail("HTTP response headers exceed the bound.")
    lines = raw[:-4].split(b"\r\n")
    try:
        status = lines[0].decode("ascii").split(" ", 2)
        code = int(status[1])
        if status[0] not in ("HTTP/1.0", "HTTP/1.1"):
            _fail("Unsupported HTTP response version.")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, value = line.split(b":", 1)
            key = name.decode("ascii").lower()
            if not key or any(char.isspace() for char in key):
                _fail("HTTP response headers are ambiguous.")
            if key in headers:
                if key in {"content-length", "transfer-encoding", "location", "content-encoding"}:
                    _fail("HTTP response framing is ambiguous.")
                continue
            headers[key] = value.decode("iso-8859-1").strip()
    except (ValueError, IndexError, UnicodeError) as error:
        message = "Malformed HTTP response headers."
        raise DownloadError(message) from error
    return code, headers


async def _fixed(
    reader: asyncio.StreamReader, length: str | None, limit: int, sink: Callable[[bytes], None]
) -> None:
    if length is None or not length.isascii() or not length.isdecimal():
        _fail("HTTP response must have an explicit length.")
    remaining = int(length)
    if remaining > limit:
        _fail("HTTP response exceeded its byte bound.")
    while remaining:
        data = await reader.readexactly(min(_CHUNK, remaining))
        sink(data)
        remaining -= len(data)


async def _chunked(reader: asyncio.StreamReader, limit: int, sink: Callable[[bytes], None]) -> None:
    count = 0
    max_chunk_header = 128
    while True:
        line = await reader.readline()
        if not line.endswith(b"\r\n") or len(line) > max_chunk_header:
            _fail("Malformed HTTP chunk header.")
        try:
            size = int(line[:-2].split(b";", 1)[0], 16)
        except ValueError as error:
            message = "Malformed HTTP chunk length."
            raise DownloadError(message) from error
        if size < 0 or count + size > limit:
            _fail("HTTP response exceeded its byte bound.")
        if size == 0:
            trailer = await reader.readuntil(b"\r\n")
            if trailer != b"\r\n":
                _fail("HTTP response trailers are unsupported.")
            return
        count += size
        remaining = size
        while remaining:
            data = await reader.readexactly(min(_CHUNK, remaining))
            sink(data)
            remaining -= len(data)
        if await reader.readexactly(2) != b"\r\n":
            _fail("Malformed HTTP chunk terminator.")


async def _body(
    reader: asyncio.StreamReader, headers: dict[str, str], limit: int, sink: Callable[[bytes], None]
) -> None:
    if headers.get("content-encoding", "identity").lower() != "identity":
        _fail("Encoded HTTP response is unsupported.")
    encoding, length = headers.get("transfer-encoding"), headers.get("content-length")
    if encoding is not None and length is not None:
        _fail("HTTP response framing is ambiguous.")
    if encoding is None:
        await _fixed(reader, length, limit, sink)
    elif encoding.lower() == "chunked":
        await _chunked(reader, limit, sink)
    else:
        _fail("HTTP response transfer encoding is unsupported.")


@dataclass(frozen=True, slots=True)
class HttpsTransport:
    """Direct TLS transport restricted to official GitHub release hosts."""

    async def get(
        self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
    ) -> None:
        """Follow bounded HTTPS redirects and stream only the final successful response."""
        if type(limit) is not int or limit <= 0:
            _fail("HTTP byte bound must be positive.")
        try:
            async with asyncio.timeout_at(deadline):
                await self._follow(url, limit, sink)
        except (
            TimeoutError,
            OSError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as error:
            message = "HTTPS transfer failed or exceeded its deadline."
            raise DownloadError(message) from error

    async def _follow(self, url: str, limit: int, sink: Callable[[bytes], None]) -> None:
        context = ssl.create_default_context()
        for _ in range(6):
            host, target = _target(url)
            reader, writer = await asyncio.open_connection(
                host,
                443,
                ssl=context,
                server_hostname=host,
                limit=_CHUNK + 1,
                ssl_shutdown_timeout=5,
            )
            try:
                writer.write(
                    (
                        f"GET {target} HTTP/1.1\r\nHost: {host}\r\n"
                        "User-Agent: codex-session-gc-installer\r\n"
                        "Accept: application/vnd.github+json\r\n"
                        "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
                    ).encode("ascii")
                )
                await writer.drain()
                code, headers = await _headers(reader)
                if code in (301, 302, 303, 307, 308):
                    location = headers.get("location")
                    if location is None:
                        _fail("HTTPS redirect has no location.")
                    url = urljoin(url, location)
                    continue
                success = 200
                if code != success:
                    _fail(f"HTTPS request returned status {code}.")
                await _body(reader, headers, limit, sink)
                return
            finally:
                writer.close()
                await writer.wait_closed()
        _fail("Too many HTTPS redirects.")


def _remove_owned(destination: Path, identity: os.stat_result) -> None:
    with contextlib.suppress(FileNotFoundError):
        current = destination.lstat()
        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            destination.unlink()


async def download_asset(
    transport: Transport,
    url: str,
    expected: ArtifactIdentity,
    destination: Path,
    *,
    deadline: float,
) -> None:
    """Write an absent 0600 file and check exact expected size and SHA-256."""
    _target(url)
    if not destination.is_absolute() or expected.size <= 0:
        _fail("Asset destination or size is invalid.")
    descriptor = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    identity = os.fstat(descriptor)
    complete = False
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as output:

            def write(data: bytes) -> None:
                nonlocal size
                size += len(data)
                if size > expected.size:
                    _fail("Asset exceeded its declared size.")
                digest.update(data)
                output.write(data)

            async with asyncio.timeout_at(deadline):
                await transport.get(url, limit=expected.size, deadline=deadline, sink=write)
            output.flush()
            os.fsync(output.fileno())
        if size != expected.size or digest.hexdigest() != expected.sha256:
            _fail("Asset size or digest differs from release metadata.")
        complete = True
    finally:
        if not complete:
            _remove_owned(destination, identity)
