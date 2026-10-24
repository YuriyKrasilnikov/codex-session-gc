# Copyright (c) 2026
# ruff: noqa: EM101, TRY003

"""Short read-only transactions against live SQLite WAL databases."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class DirectReadError(ValueError):
    """The database path or physical identity cannot support this observation."""


def _metadata(path: Path) -> os.stat_result:
    if not path.is_absolute() or ".." in path.parts:
        raise DirectReadError("A database requires an explicit absolute path.")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise DirectReadError("A database must be a regular file.")
            return metadata
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


@contextmanager
def read_transaction(
    source: Path, *, maximum_bytes: int | None = None, deadline: float | None = None
) -> Iterator[sqlite3.Connection]:
    """Detach consumer-selected rows before returning to filesystem work.

    SQL rows are internally consistent within the transaction while concurrent
    WAL writes remain supported. The consumer owns SQL, schema and resource
    policy. Safe path opening and endpoint samples reject observed symlinks,
    invalid files and changed physical identities. These checks do not establish
    uninterrupted namespace identity or native ownership of the database.
    """
    before = _metadata(source)
    if maximum_bytes is not None and before.st_size > maximum_bytes:
        raise DirectReadError("The database exceeds its explicit size bound.")
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=0)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if deadline is not None:
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("BEGIN")
        try:
            yield connection
        finally:
            connection.rollback()
    after = _metadata(source)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise DirectReadError("A database physical identity changed during observation.")
