# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Send an explicit Keep change to the same qualified owner as native cleanup."""

from __future__ import annotations

import json
import os
import subprocess
from enum import StrEnum
from typing import TYPE_CHECKING

from codex_session_gc.execution.closed_work_apply import validate_helper_descriptor
from codex_session_gc.execution.closed_work_profile import load_closed_work_contract
from codex_session_gc.execution.keep_store import read_kept_threads

if TYPE_CHECKING:
    from pathlib import Path

    from codex_session_gc.domain import ThreadId


class KeepAction(StrEnum):
    """The two independently authorized changes to one thread."""

    MARK = "keep_set"
    CLEAR = "keep_clear"


def _unknown(storage: Path, identity: tuple[int, int], identifier: ThreadId) -> dict[str, object]:
    observed: bool | None = None
    try:
        before = storage.stat()
        if (before.st_dev, before.st_ino) == identity:
            marks = read_kept_threads(storage)
            after = storage.stat()
            if (after.st_dev, after.st_ino) == identity:
                observed = identifier in marks
    except OSError, ValueError:
        observed = None
    return {
        "status": "unknown",
        "detail": "Native reply unavailable. Keep status is a separate current observation.",
        "thread_id": str(identifier.value),
        "observed_marked": observed,
    }


def change_keep(
    storage: Path,
    helper: Path,
    identifier: ThreadId,
    action: KeepAction,
    *,
    expected_home: tuple[int, int] | None = None,
) -> dict[str, object]:
    """Verify and launch the native actor once. Observe Keep status after a lost reply."""
    canonical = storage.resolve(strict=True)
    metadata = canonical.stat()
    identity = (metadata.st_dev, metadata.st_ino)
    if expected_home is not None and identity != expected_home:
        raise ValueError("The selected home has changed. Reopen it before changing Keep.")
    request = json.dumps(
        {
            "operation": action.value,
            "codex_home": str(canonical),
            "sqlite_home": str(canonical),
            "home_device": identity[0],
            "home_inode": identity[1],
            "thread_id": str(identifier.value),
        }
    ).encode()
    descriptor = os.open(helper, os.O_NOFOLLOW | os.O_RDONLY | os.O_NONBLOCK)
    try:
        validate_helper_descriptor(descriptor, load_closed_work_contract())
        try:
            result = subprocess.run(  # noqa: S603
                [f"/proc/self/fd/{descriptor}"],
                pass_fds=(descriptor,),
                input=request,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return _unknown(canonical, identity, identifier)
    finally:
        os.close(descriptor)
    try:
        reply = json.loads(result.stdout)
    except UnicodeError, ValueError:
        reply = None
    expected = {0: "confirmed", 1: "refused"}.get(result.returncode)
    if (
        expected is None
        or not isinstance(reply, dict)
        or reply.get("status") != expected
        or not isinstance(reply.get("detail"), str)
    ):
        return _unknown(canonical, identity, identifier)
    return {
        "status": expected,
        "detail": reply["detail"],
        "thread_id": str(identifier.value),
    }
