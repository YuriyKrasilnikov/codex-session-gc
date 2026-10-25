# Copyright (c) 2026
# ruff: noqa: S101, PLR2004

"""Cheap mixed physical display keeps stored lower bounds separate from exact totals."""

from __future__ import annotations

import os
import sqlite3
from compression.zstd import compress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
from uuid import UUID

import pytest

from codex_session_gc.cli.mcp_server import _entry
from codex_session_gc.domain import HistoryMode, ThreadId
from codex_session_gc.inventory.catalogue import read_session_catalogue
from codex_session_gc.inventory.query import CatalogueIndex, CatalogueQuery
from tests.current_storage_fixture import _A, _B, _C
from tests.current_storage_fixture import _catalogue_home as _home


def _path(home: Path, owner: str, rollout: str | None = None) -> Path:
    suffix = owner if rollout is None else f"{owner}_{rollout}"
    return home / "sessions" / f"rollout-2026-06-01T00-00-00-{suffix}.jsonl"


def test_mixed_compressed_revert_display_uses_distinct_owned_stored_bytes(tmp_path: Path) -> None:
    """A raw plain SQL pointer resolves its observed compressed alias and owned old histories."""
    database = _home(tmp_path)
    current = _path(tmp_path, _A)
    compressed = current.with_name(current.name + ".zst")
    compressed.write_bytes(compress(current.read_bytes()))
    current.unlink()
    reverted = _path(tmp_path, _A, str(UUID(int=9)))
    reverted.write_bytes(b"retained immutable history" * 7)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET history_mode='paginated' WHERE id=?", (_A,))
    catalogue = read_session_catalogue(tmp_path)
    rows = {str(row.id.value): row for row in catalogue.entries}
    own = compressed.stat().st_size + reverted.stat().st_size
    assert catalogue.physical_complete
    assert rows[_A].stored_bytes == rows[_A].known_stored_bytes == own
    assert rows[_A].reported_history_mode is HistoryMode.PAGINATED
    assert {item.representation.value for item in rows[_A].physical_rollouts} == {"plain", "zstd"}
    group = CatalogueIndex(catalogue).query(CatalogueQuery(roots=(ThreadId.parse(_A),))).groups[0]
    assert group.total_stored_bytes == own + rows[_B].known_stored_bytes
    assert group.total_known_stored_bytes == group.total_stored_bytes
    assert rows[_C].id not in {entry.id for entry in group.entries}
    public = _entry(rows[_A])
    assert public["stored_bytes"] == own
    assert public["physical_lineage"] == "unknown"
    assert public["shared_retained_bytes"] is None
    assert public["conditional_reclaim_bytes"] is None
    assert {item["representation"] for item in public["physical_rollouts"]} == {"plain", "zstd"}


def test_incomplete_namespace_preserves_known_amount_and_proven_minimum(tmp_path: Path) -> None:
    """Unknown membership keeps known lower bounds, proves minima and never proves maxima."""
    _home(tmp_path)
    (tmp_path / "sessions" / "unidentified-nonempty").write_bytes(b"unknown owner")
    observed = read_session_catalogue(tmp_path)
    assert not observed.physical_complete
    assert all(entry.stored_bytes is None for entry in observed.entries)
    assert all(entry.known_stored_bytes == len(b"not history JSON") for entry in observed.entries)
    index = CatalogueIndex(observed)
    assert index.query(CatalogueQuery(min_bytes=len(b"not history JSON"))).groups
    assert not index.query(CatalogueQuery(min_bytes=len(b"not history JSON") + 1)).groups
    assert not index.query(CatalogueQuery(max_bytes=10**9)).groups
    assert not index.query(CatalogueQuery(min_bytes=0, max_bytes=10**9)).groups
    assert index.query(CatalogueQuery()).groups


def test_large_known_incomplete_work_sorts_above_small_exact_work(tmp_path: Path) -> None:
    """Sorting ranks observed bytes while explicitly retaining uncertainty about true totals."""
    database = _home(tmp_path)
    _path(tmp_path, _C).write_bytes(b"x" * 100_000)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE threads SET rollout_path=? WHERE id=?",
            (str(tmp_path / "sessions" / "missing"), _C),
        )
    groups = CatalogueIndex(read_session_catalogue(tmp_path)).query(CatalogueQuery()).groups
    assert str(groups[0].root.id.value) == _C
    assert groups[0].total_stored_bytes is None
    assert groups[0].total_known_stored_bytes == 100_000
    assert groups[1].total_stored_bytes is not None


def test_descriptor_alias_is_unattributed_without_losing_other_known_objects(
    tmp_path: Path,
) -> None:
    """An ambiguous inode contributes to no owner's subtotal, including through group sums."""
    _home(tmp_path)
    extra = _path(tmp_path, _A, str(UUID(int=9)))
    extra.write_bytes(b"unambiguous bytes")
    alias = _path(tmp_path, _C, str(UUID(int=10)))
    os.link(_path(tmp_path, _A), alias)
    catalogue = read_session_catalogue(tmp_path)
    rows = {str(row.id.value): row for row in catalogue.entries}
    assert rows[_A].stored_bytes is None
    assert rows[_A].known_stored_bytes == len(b"unambiguous bytes")
    assert rows[_C].stored_bytes is None
    assert rows[_C].known_stored_bytes == len(b"not history JSON")
    assert "ambiguous_physical_identity" in catalogue.diagnostics
    groups = CatalogueIndex(catalogue).query(CatalogueQuery()).groups
    assert sum(group.total_known_stored_bytes for group in groups) == len(
        b"unambiguous bytes"
    ) + 2 * len(b"not history JSON")


def test_complete_cleanup_preview_counts_owned_formats_and_shared_provider_once(
    tmp_path: Path,
) -> None:
    """Two selected revert histories share one retained provider without counting it as reclaim."""
    import json  # noqa: PLC0415

    from codex_session_gc.cli.console_display import preview_lines  # noqa: PLC0415
    from codex_session_gc.cli.console_tasks import preview_selection  # noqa: PLC0415
    from tests.current_storage_fixture import _CUTOFF, _fixture, _id  # noqa: PLC0415

    facts, _, _ = _fixture(tmp_path)
    home = tmp_path / "home"
    current = next(item.path for item in facts.rollouts if item.thread_id == _id(2))
    provider = next(item.path for item in facts.rollouts if item.thread_id == _id(3))
    envelope = json.loads(current.read_text())
    prefix = {"thread_id": str(_id(3).value), "end_ordinal_exclusive": 1, "end_byte_offset": 17}
    envelope["payload"].update(history_mode="paginated", history_base=prefix)
    encoded = (json.dumps(envelope) + "\n").encode()
    compressed = current.with_name(current.name + ".zst")
    compressed.write_bytes(compress(encoded))
    current.unlink()
    old = current.with_name(
        current.name.replace(f"{_id(2).value}.jsonl", f"{_id(2).value}_{_id(9).value}.jsonl")
    )
    old.write_bytes(encoded)
    with sqlite3.connect(home / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET history_mode='paginated' WHERE id=?", (str(_id(2).value),)
        )
    result = preview_selection(home, _CUTOFF, frozenset({_id(1)}), frozenset({_id(2)}))
    owned = compressed.stat().st_size + old.stat().st_size
    assert result["sizes"] == [owned]
    assert result["known_stored_bytes"] == result["conditional_reclaim_bytes"] == owned
    assert result["shared_retained_bytes"] == provider.stat().st_size
    assert {
        item["representation"]
        for target in result["physical_manifest"]
        for item in target["rollouts"]
    } == {"plain", "zstd"}
    text = "\n".join(preview_lines(result))
    assert "paginated" in text
    assert "zstd" in text
    assert "plain" in text
    assert "not guaranteed disk reclaim" in text
    assert provider.exists()
    assert compressed.exists()
    assert old.exists()


@pytest.mark.parametrize(
    ("representation", "pointer_encoding", "normalization"),
    [
        ("plain", "plain", "none"),
        ("plain", "zstd", "none"),
        ("zstd", "zstd", "none"),
        ("zstd", "plain", "none"),
        ("zstd", "plain", "dot"),
        ("zstd", "plain", "separators"),
        ("zstd", "plain", "trailing"),
    ],
)
def test_current_pointer_matches_owned_reverted_path_and_normalized_encoding_alias(
    tmp_path: Path,
    representation: str,
    pointer_encoding: str,
    normalization: str,
) -> None:
    """Raw SQL paths retain lexical normalization and either encoding of an observed ID."""
    database = _home(tmp_path)
    initial = _path(tmp_path, _A)
    plain = _path(tmp_path, _A, str(UUID(int=9)))
    observed = plain.with_name(plain.name + ".zst") if representation == "zstd" else plain
    content = b"new current rollout"
    observed.write_bytes(compress(content) if representation == "zstd" else content)
    pointer = plain.with_name(plain.name + ".zst") if pointer_encoding == "zstd" else plain
    declared = str(pointer)
    if normalization == "dot":
        declared = str(pointer.parent) + "/./" + pointer.name
    elif normalization == "separators":
        declared = str(pointer.parent) + "//" + pointer.name
    elif normalization == "trailing":
        declared += "/"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (declared, _A))
    catalogue = read_session_catalogue(tmp_path)
    row = next(entry for entry in catalogue.entries if str(entry.id.value) == _A)
    assert catalogue.physical_complete
    assert not catalogue.diagnostics
    assert (
        row.stored_bytes
        == row.known_stored_bytes
        == initial.stat().st_size + observed.stat().st_size
    )
    assert {str(item.ownership.rollout_id.value) for item in row.physical_rollouts} == {
        _A,
        str(UUID(int=9)),
    }


@pytest.mark.parametrize(
    "pointer_kind",
    [
        "wrong_owner",
        "wrong_rollout",
        "different_directory",
        "directory",
        "malformed_timestamp",
        "malformed_uuid",
        "relative",
        "missing",
        "parent_traversal",
        "outside",
    ],
)
def test_unobserved_current_pointer_keeps_known_subtotal_without_exact_total(
    tmp_path: Path, pointer_kind: str
) -> None:
    """A plausible pointer never substitutes for observed ownership or immutable membership."""
    database = _home(tmp_path)
    current = _path(tmp_path, _A)
    declared = {
        "wrong_owner": str(_path(tmp_path, _C)),
        "wrong_rollout": str(_path(tmp_path, _A, str(UUID(int=9)))),
        "different_directory": str(tmp_path / "archived_sessions" / current.name),
        "directory": str(current.parent),
        "malformed_timestamp": str(
            current.with_name(current.name.replace("2026-06-01", "2026-02-30"))
        ),
        "malformed_uuid": str(current.with_name(current.name.replace(_A, "not-a-uuid"))),
        "relative": str(current.relative_to(tmp_path)),
        "missing": str(current.with_name("missing.jsonl")),
        "parent_traversal": str(current.parent) + "/../sessions/" + current.name,
        "outside": str(tmp_path.parent / current.name),
    }[pointer_kind]
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (declared, _A))
    catalogue = read_session_catalogue(tmp_path)
    row = next(entry for entry in catalogue.entries if str(entry.id.value) == _A)
    assert not catalogue.physical_complete
    assert row.stored_bytes is None
    assert not row.physical_complete
    assert row.issue == "physical_history_incomplete"
    assert row.known_stored_bytes == current.stat().st_size
    assert tuple(item.path for item in row.physical_rollouts) == (current,)
    assert not catalogue.diagnostics
