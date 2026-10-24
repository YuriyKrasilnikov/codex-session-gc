# Copyright (c) 2026
# ruff: noqa: S101, PLR2004

"""Descriptor behavior of bounded metadata reads during real body appends."""

from __future__ import annotations

import os
from compression.zstd import compress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from codex_session_gc.domain import PhysicalRepresentation
from codex_session_gc.execution.bounded_header import (
    HeaderReadError,
    HeaderReadLimits,
    read_bounded_header,
)


@pytest.mark.parametrize("representation", tuple(PhysicalRepresentation))
@pytest.mark.parametrize("selected", [False, True])
def test_body_append_obeys_physical_consumer_role(
    tmp_path: Path, representation: PhysicalRepresentation, *, selected: bool
) -> None:
    """Retained header observation permits appends; selected effects require exact stat."""
    path = tmp_path / "history"
    line = b'{"type":"session_meta","payload":{}}\n'
    path.write_bytes(compress(line) if representation is PhysicalRepresentation.ZSTD else line)
    descriptor = os.open(path, os.O_RDONLY)
    appended = False
    charged = 0

    def charge(count: int) -> None:
        nonlocal appended, charged
        charged += count
        if not appended:
            with path.open("ab") as output:
                output.write(
                    compress(b"body\n")
                    if representation is PhysicalRepresentation.ZSTD
                    else b"body\n"
                )
            appended = True

    try:
        limits = HeaderReadLimits(4096, 16384, selected)
        if selected:
            with pytest.raises(HeaderReadError, match="changed"):
                read_bounded_header(descriptor, representation, limits=limits, charge=charge)
        else:
            result = read_bounded_header(descriptor, representation, limits=limits, charge=charge)
            assert result.line == line
            assert result.physical_bytes_read == charged
            assert result.stat.stored_bytes == path.stat().st_size
        assert os.fstat(descriptor).st_ino == path.stat().st_ino
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 0
        assert charged <= 16384
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("representation", tuple(PhysicalRepresentation))
def test_prefix_limits_do_not_require_complete_tail(
    tmp_path: Path, representation: PhysicalRepresentation
) -> None:
    """A complete first line suffices within caller bounds despite an unread invalid tail."""
    path = tmp_path / "history"
    line = b"metadata\n"
    path.write_bytes(
        (compress(line) if representation is PhysicalRepresentation.ZSTD else line)
        + b"invalid tail"
    )
    descriptor = os.open(path, os.O_RDONLY)
    try:
        observed = read_bounded_header(
            descriptor,
            representation,
            limits=HeaderReadLimits(len(line), 4096, require_stable_extent=True),
            charge=lambda _: None,
        )
        assert observed.line == line
        with pytest.raises(HeaderReadError):
            read_bounded_header(
                descriptor,
                representation,
                limits=HeaderReadLimits(len(line) - 1, 4096, require_stable_extent=True),
                charge=lambda _: None,
            )
    finally:
        os.close(descriptor)
