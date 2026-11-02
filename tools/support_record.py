# Copyright (c) 2026

"""Prepare the exact source support record as an attestation subject."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
from pathlib import Path

from codex_session_gc.compatibility.record import MAX_RECORD_BYTES, parse_record

_SOURCE = Path(__file__).resolve().parents[1] / "src/codex_session_gc/compatibility/support.json"


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
    )


def prepare_support_record(output: Path) -> tuple[str, int]:
    """Validate and copy exact source bytes once to an absent absolute output."""
    if not output.is_absolute():
        message = "Support output must be an absolute path."
        raise ValueError(message)
    fd = os.open(_SOURCE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_RECORD_BYTES:
            message = "Support source is not a bounded regular file."
            raise ValueError(message)
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(MAX_RECORD_BYTES + 1)
        after = os.fstat(fd)
        current = _SOURCE.stat(follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or _identity(before) != _identity(current)
            or len(raw) != before.st_size
        ):
            message = "Support source changed during reading."
            raise ValueError(message)
    finally:
        os.close(fd)
    parse_record(raw)
    target = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(target, "wb") as destination:
        destination.write(raw)
        destination.flush()
        os.fsync(destination.fileno())
    return hashlib.sha256(raw).hexdigest(), len(raw)


def main() -> None:
    """Write one selected subject and report its exact content identity."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest, size = prepare_support_record(args.output)
    sys.stdout.write(f"sha256={digest} size={size}\n")


if __name__ == "__main__":
    main()
