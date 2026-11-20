# Copyright (c) 2026
"""The qualified SQLite engine used for all application databases."""

from contextlib import closing
from functools import cache
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

import pysqlite3 as _sqlite
from pysqlite3 import Connection, DatabaseError, Error, OperationalError, Row

__all__ = [
    "Connection",
    "DatabaseError",
    "Error",
    "OperationalError",
    "Row",
    "connect",
    "ensure_qualified",
]

_EXPECTED_SOURCE = (
    "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76"
)


@cache
def ensure_qualified() -> None:
    """Check the active engine before creating or opening a database."""
    with closing(_sqlite.connect(":memory:")) as probe:
        row = probe.execute("SELECT sqlite_version(), sqlite_source_id()").fetchone()
    if row != ("3.54.1", _EXPECTED_SOURCE):
        msg = f"Unsupported SQLite engine: {row!r}; expected 3.54.1, {_EXPECTED_SOURCE}."
        raise ValueError(msg)


def connect(
    database: str | Path,
    *,
    uri: bool = False,
    timeout: float = 5.0,
    isolation_level: Literal["", "DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = "",
) -> Connection:
    """Verify the engine before SQLite opens any application database."""
    ensure_qualified()
    return _sqlite.connect(database, uri=uri, timeout=timeout, isolation_level=isolation_level)
