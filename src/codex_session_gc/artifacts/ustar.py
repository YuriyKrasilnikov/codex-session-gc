# Copyright (c) 2026

"""Bounded physical USTAR parsing without archive extension processing."""

from __future__ import annotations

import tarfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import gzip


class UstarError(ValueError):
    """The compressed stream is not a supported canonical USTAR archive."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise UstarError(message)


class UstarReader:
    """Read physical USTAR headers before touching any member body."""

    def __init__(self, source: gzip.GzipFile) -> None:
        """Read from the caller-owned decompressed stream."""
        self.source = source
        self.position = 0
        self.remaining = 0
        self.member_size = 0
        self.finished = False

    def _exact(self, size: int) -> bytes:
        data = self.source.read(size)
        _require(len(data) == size, "Truncated USTAR stream.")
        self.position += size
        return data

    def read(self, size: int) -> bytes:
        """Read at most the remaining bytes of the current member."""
        _require(type(size) is int and size >= 0, "USTAR read size must be nonnegative.")
        count = min(size, self.remaining)
        data = self._exact(count)
        self.remaining -= len(data)
        return data

    def next(self) -> tarfile.TarInfo | None:
        """Validate the next physical header without processing extension bodies."""
        _require(self.remaining == 0, "Prior archive member was not consumed.")
        if self.finished:
            return None
        if self.member_size:
            padding = (-self.member_size) % 512
            _require(not any(self._exact(padding)), "Archive padding is not zero.")
            self.member_size = 0
        header = self._exact(512)
        if not any(header):
            _require(not any(self._exact(512)), "Archive terminator is incomplete.")
            padding = (-self.position) % 10240
            _require(not any(self._exact(padding)), "Archive trailer is not zero.")
            _require(not self.source.read(1), "Unexpected trailing archive data.")
            self.finished = True
            return None
        _require(header[257:265] == b"ustar\x0000", "Only USTAR headers are supported.")
        _require(
            header[156:157] in (b"0", b"\0", b"5"), "Archive extensions and links are forbidden."
        )
        for start, end in ((100, 108), (108, 116), (116, 124), (124, 136), (136, 148), (148, 156)):
            field = header[start:end].strip(b"\0 ")
            _require(
                not field or all(byte in b"01234567" for byte in field), "Non-octal archive field."
            )
        try:
            member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        except (tarfile.HeaderError, UnicodeDecodeError) as error:
            message = "USTAR header is invalid."
            raise UstarError(message) from error
        _require(member.size >= 0 and (member.isfile() or member.size == 0), "Invalid member size.")
        self.remaining = self.member_size = member.size
        return member
