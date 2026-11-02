# Copyright (c) 2026

"""Hold one bounded executable observation for a single diagnostic call."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from codex_session_gc.installation.read_budget import ReadBudget


class InvalidHelperError(ValueError):
    """The requested helper is not a regular executable."""


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True, slots=True)
class HelperObservation:
    """A digest tied to an open descriptor and its observed executable path."""

    path: Path
    descriptor: int
    identity: tuple[int, int, int, int, int]
    digest: bytes

    def validate(self, budget: ReadBudget) -> None:
        """Refuse a changed descriptor, path or executable under the shared budget."""
        budget.check(self.identity[2])
        current = os.fstat(self.descriptor)
        path_info = self.path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or not current.st_mode & 0o111
            or _identity(current) != self.identity
            or _identity(path_info) != self.identity
        ):
            msg = "Helper changed during observation; retry with a stable release file."
            raise OSError(msg)
        budget.check()

    def digest_for(self, path: Path, descriptor: int, budget: ReadBudget) -> str | None:
        """Return the digest only when both open files and paths identify the same file."""
        self.validate(budget)
        other = os.fstat(descriptor)
        path_info = path.stat(follow_symlinks=False)
        if _identity(other) != _identity(path_info):
            msg = "Installed helper changed during validation."
            raise OSError(msg)
        if (other.st_dev, other.st_ino) != self.identity[:2]:
            return None
        if _identity(other) != self.identity:
            msg = "Helper changed during observation; retry with a stable release file."
            raise OSError(msg)
        budget.check(other.st_size)
        return self.digest.hex()


@contextmanager
def observe_helper(path: Path, budget: ReadBudget) -> Iterator[HelperObservation]:
    """Hash a bounded executable and retain its descriptor until the caller exits."""
    budget.check()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not before.st_mode & 0o111:
            msg = "Use the regular executable from the matching release bundle."
            raise InvalidHelperError(msg)
        budget.check(before.st_size)
        digest = hashlib.sha256()
        offset = 0
        while block := os.pread(descriptor, min(1024 * 1024, before.st_size - offset + 1), offset):
            digest.update(block)
            offset += len(block)
            budget.check(offset)
            if offset > before.st_size:
                msg = "Helper changed during observation; retry with a stable release file."
                raise OSError(msg)
        if offset != before.st_size:
            msg = "Helper changed during observation; retry with a stable release file."
            raise OSError(msg)
        observation = HelperObservation(path, descriptor, _identity(before), digest.digest())
        observation.validate(budget)
        yield observation
    finally:
        os.close(descriptor)
