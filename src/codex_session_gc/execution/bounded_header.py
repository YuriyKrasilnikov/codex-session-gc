# Copyright (c) 2026

"""One bounded standard stream over a borrowed regular descriptor."""

from __future__ import annotations

import io
import os
import stat
from compression.zstd import DecompressionParameter, ZstdError, ZstdFile
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Buffer, Callable

from codex_session_gc.domain import FileStatIdentity, PhysicalRepresentation

HEADER_LIMIT = 1 << 20
PHYSICAL_LIMIT = 2 << 20
READ_CHUNK = 4096
WINDOW_LOG_MAX = 21


class HeaderReadError(ValueError):
    """A complete, stable metadata line was not observed within its bounds."""


def stat_identity(value: os.stat_result) -> FileStatIdentity:
    """Project actual descriptor stat without treating stored bytes as decoded offsets."""
    return FileStatIdentity(
        value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
    )


@dataclass(frozen=True, slots=True)
class DecodedHeader:
    """The bounded transient line, stable stat and exact underlying read count."""

    line: bytes
    stat: FileStatIdentity
    physical_bytes_read: int


@dataclass(frozen=True, slots=True)
class HeaderReadLimits:
    """Caller bounds and whether this physical object is a selected effect."""

    maximum_header_bytes: int
    maximum_physical_bytes: int
    require_stable_extent: bool

    def __post_init__(self) -> None:
        """Reject ambiguous caller values rather than coerce unknown sizes or roles."""
        if (
            any(
                type(value) is not int or value < 1
                for value in (self.maximum_header_bytes, self.maximum_physical_bytes)
            )
            or type(self.require_stable_extent) is not bool
        ):
            message = "Header observation needs positive explicit limits and a typed role."
            raise ValueError(message)


class _BorrowedInput(io.RawIOBase):
    def __init__(self, descriptor: int, limit: int, charge: Callable[[int], None]) -> None:
        self.descriptor = descriptor
        self.limit = limit
        self.charge = charge
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        view = memoryview(buffer)
        remaining = self.limit - self.bytes_read
        if remaining <= 0:
            message = "The physical metadata input bound was exhausted."
            raise HeaderReadError(message)
        data = os.pread(self.descriptor, min(len(view), READ_CHUNK, remaining), self.bytes_read)
        self.bytes_read += len(data)
        self.charge(len(data))
        view[: len(data)] = data
        return len(data)


def read_bounded_header(
    descriptor: int,
    representation: PhysicalRepresentation,
    *,
    limits: HeaderReadLimits,
    charge: Callable[[int], None],
) -> DecodedHeader:
    """Borrow an FD, cap reads before buffering and decode only through the first newline.

    The caller owns descriptor/path admission and lifetime. Neither concatenated
    frame support nor a successful prefix establishes whole-tail integrity.
    """
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        message = "Metadata requires an already-open regular file."
        raise HeaderReadError(message)
    if type(representation) is not PhysicalRepresentation:
        message = "The physical representation must be explicit."
        raise TypeError(message)
    bound = min(limits.maximum_header_bytes, HEADER_LIMIT)
    source = _BorrowedInput(descriptor, min(limits.maximum_physical_bytes, PHYSICAL_LIMIT), charge)
    stream = _stream(source, representation)
    line = _read_line(stream, bound)
    after = os.fstat(descriptor)
    if stat_identity(before) != stat_identity(after):
        if limits.require_stable_extent or (before.st_dev, before.st_ino) != (
            after.st_dev,
            after.st_ino,
        ):
            message = "The selected physical history changed during metadata observation."
            raise HeaderReadError(message)
        # Unrelated appenders may change body extent/timestamps. Recheck only the
        # immutable metadata prefix, within the original combined input budget.
        verification = _BorrowedInput(descriptor, source.limit - source.bytes_read, charge)
        verify_stream = _stream(verification, representation)
        verified = _read_line(verify_stream, bound)
        source.bytes_read += verification.bytes_read
        latest = os.fstat(descriptor)
        if verified != line or (after.st_dev, after.st_ino) != (latest.st_dev, latest.st_ino):
            message = "The retained metadata header changed during observation."
            raise HeaderReadError(message)
        after = latest
    return DecodedHeader(line, stat_identity(after), source.bytes_read)


def _read_line(stream: io.BufferedReader | ZstdFile, bound: int) -> bytes:
    line = bytearray()
    try:
        with stream:
            while len(line) < bound:
                chunk = stream.read1(min(READ_CHUNK, bound - len(line)))
                if not chunk:
                    break
                part, newline, _ = chunk.partition(b"\n")
                line.extend(part)
                if newline:
                    line.extend(b"\n")
                    break
    except (ZstdError, EOFError) as error:
        message = "The bounded metadata stream is truncated or exceeds decoder policy."
        raise HeaderReadError(message) from error
    if not line.endswith(b"\n"):
        message = "A complete decoded metadata line was not observed within its bound."
        raise HeaderReadError(message)
    return bytes(line)


def _stream(
    source: _BorrowedInput, representation: PhysicalRepresentation
) -> io.BufferedReader | ZstdFile:
    if representation is PhysicalRepresentation.ZSTD:
        return ZstdFile(
            source, "rb", options={DecompressionParameter.window_log_max: WINDOW_LOG_MAX}
        )
    return io.BufferedReader(source, buffer_size=READ_CHUNK)
