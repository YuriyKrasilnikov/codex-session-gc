# Copyright (c) 2026
# ruff: noqa: PLR2004, S606

"""Block a native command until its owning parent grants this exact launch."""

from __future__ import annotations

import os
import selectors
import sys
import time
from typing import Final

_GRANT: Final = b"codex-session-gc:grant-v1\n"
_GRANT_TIMEOUT_SECONDS: Final = 5.0


def main() -> int:
    """Wait for one exact grant, then replace this process with the requested command."""
    if len(sys.argv) < 2:
        return 64
    if not _read_grant(sys.stdin.fileno()):
        return 65
    os.execve(sys.argv[1], sys.argv[1:], os.environ)
    return 127  # type: ignore[unreachable]


def _read_grant(descriptor: int) -> bool:
    deadline = time.monotonic() + _GRANT_TIMEOUT_SECONDS
    received = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while len(received) < len(_GRANT):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return False
            chunk = os.read(descriptor, len(_GRANT) - len(received))
            if not chunk:
                return False
            received.extend(chunk)
    return bytes(received) == _GRANT


if __name__ == "__main__":
    raise SystemExit(main())
