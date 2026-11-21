# Copyright (c) 2026

"""Prepare the exact source support record as an attestation subject."""

from __future__ import annotations

import argparse
import hashlib
import json
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


def _read_subject(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_RECORD_BYTES:
            message = "Support source is not a bounded regular file."
            raise ValueError(message)
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(MAX_RECORD_BYTES + 1)
        after = os.fstat(fd)
        current = path.stat(follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or _identity(before) != _identity(current)
            or len(raw) != before.st_size
        ):
            message = "Support source changed during reading."
            raise ValueError(message)
    finally:
        os.close(fd)
    return raw


def _write(output: Path, raw: bytes) -> tuple[str, int]:
    if not output.is_absolute():
        message = "Support output must be an absolute path."
        raise ValueError(message)
    target = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(target, "wb") as destination:
        destination.write(raw)
        destination.flush()
        os.fsync(destination.fileno())
    return hashlib.sha256(raw).hexdigest(), len(raw)


def prepare_support_record(output: Path) -> tuple[str, int]:
    """Validate and copy exact source bytes once to an absent absolute output."""
    raw = _read_subject(_SOURCE)
    parse_record(raw)
    return _write(output, raw)


def prepare_badge(subject: Path, output: Path) -> tuple[str, int]:
    """Display the Codex CLI target version from the selected release support record."""
    record = parse_record(_read_subject(subject))
    if record.target.repository != "openai/codex":
        message = "The Codex CLI badge requires an OpenAI Codex target."
        raise ValueError(message)
    target = record.target.source.ref.removeprefix("refs/tags/rust-v")
    badge = {
        "schemaVersion": 1,
        "label": "Codex CLI",
        "message": target,
        "color": "blue",
    }
    return _write(output, (json.dumps(badge, ensure_ascii=False) + "\n").encode())


def main() -> None:
    """Write one selected subject and report its exact content identity."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--badge-from", type=Path, help="Render a badge for this support record")
    args = parser.parse_args()
    digest, size = (
        prepare_support_record(args.output)
        if args.badge_from is None
        else prepare_badge(args.badge_from, args.output)
    )
    sys.stdout.write(f"sha256={digest} size={size}\n")


if __name__ == "__main__":
    main()
