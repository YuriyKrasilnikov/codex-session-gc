# Copyright (c) 2026

"""Validated identities for release artifacts and their source."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """A precise Git commit and fully qualified ref."""

    commit: str
    ref: str

    def __post_init__(self) -> None:
        """Reject invalid identity values."""
        if re.fullmatch(r"[0-9a-f]{40}", self.commit) is None:
            msg = "commit must be a lowercase 40-digit Git object ID"
            raise ValueError(msg)
        if not self.ref.startswith("refs/") or any(c.isspace() for c in self.ref):
            msg = "ref must be a fully qualified Git ref"
            raise ValueError(msg)
        if re.fullmatch(r"refs/(?:tags|heads)/[A-Za-z0-9][A-Za-z0-9._/-]*", self.ref) is None:
            msg = "ref must name a branch or tag"
            raise ValueError(msg)
        if any(x in self.ref for x in ("..", "//", "@{")) or self.ref.endswith(("/", ".", ".lock")):
            msg = "ref is invalid"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """The expected bytes of one artifact."""

    sha256: str
    size: int

    def __post_init__(self) -> None:
        """Reject invalid identity values."""
        if re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            msg = "sha256 must be a lowercase 64-digit digest"
            raise ValueError(msg)
        if type(self.size) is not int or self.size < 0:
            msg = "size must be a nonnegative integer"
            raise ValueError(msg)
