# Copyright (c) 2026

"""Read the optional local Keep registry without creating or changing it."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

from codex_session_gc import sqlite_engine as sqlite3
from codex_session_gc.domain import ThreadId
from codex_session_gc.sqlite_read import DirectReadError, read_transaction

if TYPE_CHECKING:
    from pathlib import Path

_SCHEMA = "CREATE TABLE kept_threads (thread_id TEXT PRIMARY KEY NOT NULL) STRICT"


class KeepStoreError(ValueError):
    """Keep storage exists but cannot be safely interpreted."""


def _identity(home: Path) -> tuple[int, int] | None:
    """Identify one regular, singly linked registry owned with its home."""
    if not home.is_absolute() or ".." in home.parts:
        message = "Keep storage requires an absolute home path."
        raise KeepStoreError(message)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in home.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        owner = os.fstat(fd).st_uid
        try:
            child = os.open(
                ".codex-session-gc", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
        except FileNotFoundError:
            return None
        os.close(fd)
        fd = child
        if os.fstat(fd).st_uid != owner:
            message = "Keep directory owner differs from the home owner."
            raise KeepStoreError(message)
        try:
            metadata = os.stat("keep.sqlite3", dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != owner:
            message = "Keep storage must be a singly linked file owned with the home."
            raise KeepStoreError(message)
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(fd)


def read_kept_threads(home: Path) -> frozenset[ThreadId]:
    """Read canonical marks; a genuinely absent store represents no marks."""
    try:
        identity = _identity(home)
        if identity is None:
            return frozenset()
        path = home / ".codex-session-gc" / "keep.sqlite3"
        with read_transaction(path) as connection:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                message = "Keep storage failed its integrity check."
                raise KeepStoreError(message)
            objects = connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name != 'sqlite_autoindex_kept_threads_1' ORDER BY type,name"
            ).fetchall()
            if objects != [("table", "kept_threads", "kept_threads", _SCHEMA)]:
                message = "Keep storage has an unsupported schema."
                raise KeepStoreError(message)
            rows = connection.execute("SELECT thread_id FROM kept_threads").fetchall()
            result = set()
            for (value,) in rows:
                identifier = ThreadId.parse(value)
                if not isinstance(value, str) or value != str(identifier.value):
                    message = "Keep storage contains a noncanonical thread ID."
                    raise KeepStoreError(message)
                result.add(identifier)
        if _identity(home) != identity:
            message = "Keep storage identity changed during observation."
            raise KeepStoreError(message)
        return frozenset(result)
    except (OSError, sqlite3.Error, DirectReadError, TypeError, ValueError) as error:
        if isinstance(error, KeepStoreError):
            raise
        message = "Keep storage is unreadable or malformed."
        raise KeepStoreError(message) from error
