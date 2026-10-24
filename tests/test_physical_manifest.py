# Copyright (c) 2026
# ruff: noqa: S101

"""Exact current manifest round trips and refuses widened saved declarations."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.domain import InvalidRecordError, RolloutId
from codex_session_gc.execution.physical_manifest import (
    decode_targets,
    history_cache_keys,
    manifest_bytes,
    validate_reviewed_manifest,
)
from tests.current_storage_fixture import _fixture, _id, _prepare

if TYPE_CHECKING:
    from pathlib import Path


def test_reviewed_manifest_round_trip_and_cache_union(tmp_path: Path) -> None:
    """Review bytes preserve all exact saved physical fields and initial logical keys."""
    facts, selection, helper = _fixture(tmp_path)
    targets = json.loads(_prepare(tmp_path, facts, selection, helper).data)["targets"]
    frozen = manifest_bytes(targets)
    validate_reviewed_manifest(frozen, (_id(1), _id(2)))
    assert manifest_bytes(decode_targets(targets)) == frozen
    assert history_cache_keys(decode_targets(targets)) == tuple(str(_id(i).value) for i in (1, 2))
    with pytest.raises(ValueError, match=r"."):
        validate_reviewed_manifest(frozen, (_id(1),))
    with pytest.raises(TypeError, match=r"."):
        validate_reviewed_manifest(bytearray(frozen), (_id(1), _id(2)))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "boolean_stat",
        "short_hash",
        "boolean_hash",
        "unknown_mode",
        "duplicate",
        "pointer",
        "prefix_bool",
    ],
)
def test_saved_manifest_rejects_ambiguous_physical_values(tmp_path: Path, mutation: str) -> None:
    """No field dropping, bool-as-int, unknown mode or pointer reinterpretation is allowed."""
    facts, selection, helper = _fixture(tmp_path)
    targets = json.loads(_prepare(tmp_path, facts, selection, helper).data)["targets"]
    physical = targets[0]["rollouts"][0]
    if mutation == "extra":
        physical["extra"] = None
    elif mutation == "boolean_stat":
        physical["physical_identity"]["size"] = True
    elif mutation == "short_hash":
        physical["header_sha256"] = [0]
    elif mutation == "boolean_hash":
        physical["header_sha256"][0] = True
    elif mutation == "unknown_mode":
        physical["history_mode"] = "unknown"
    elif mutation == "duplicate":
        targets[0]["rollouts"].append(deepcopy(physical))
    elif mutation == "pointer":
        targets[0]["current_rollout_id"] = str(_id(9).value)
    else:
        physical["history_prefix"] = {
            "provider_rollout_id": str(_id(9).value),
            "end_ordinal_exclusive": True,
            "end_byte_offset": 0,
        }
    with pytest.raises(ValueError, match=r"."):
        decode_targets(targets)


def test_compressed_historical_revert_owns_its_metadata_and_cache_key(tmp_path: Path) -> None:
    """Every selected-owner representation is bound despite historical provider/role changes."""
    from compression.zstd import compress  # noqa: PLC0415

    from tests.current_storage_fixture import _refresh_physical  # noqa: PLC0415

    facts, selection, helper = _fixture(tmp_path)
    home = tmp_path / "home"
    archive = home / "archived_sessions"
    archive.mkdir()
    path = archive / f"rollout-2026-05-01T00-00-00-{_id(1).value}_{_id(9).value}.jsonl.zst"
    historical = {
        "type": "session_meta",
        "payload": {
            "id": str(_id(1).value),
            "source": "exec",
            "model_provider": "old-provider",
            "agent_role": "old-role",
            "history_mode": "legacy",
        },
    }
    path.write_bytes(compress((json.dumps(historical) + "\n").encode()))
    facts = _refresh_physical(tmp_path, facts)
    targets = json.loads(_prepare(tmp_path, facts, selection, helper).data)["targets"]
    owner = targets[0]
    assert owner["provider"] == "openai"
    assert [item["rollout_id"] for item in owner["rollouts"]] == [str(_id(i).value) for i in (1, 9)]
    assert owner["rollouts"][1]["representation"] == "zstd"
    assert owner["rollouts"][1]["rollout_path"] == str(path)
    assert history_cache_keys(decode_targets(targets)) == tuple(
        str(_id(i).value) for i in (1, 2, 9)
    )


def test_raw_sql_plain_pointer_resolves_only_observed_compressed_same_identity(
    tmp_path: Path,
) -> None:
    """Compression changes physical storage while preserving the raw SQL pointer and ID."""
    from compression.zstd import compress  # noqa: PLC0415

    from tests.current_storage_fixture import _refresh_physical  # noqa: PLC0415

    facts, selection, helper = _fixture(tmp_path)
    original = next(item.path for item in facts.rollouts if item.thread_id == _id(1))
    compressed = original.with_name(original.name + ".zst")
    compressed.write_bytes(compress(original.read_bytes()))
    original.unlink()
    facts = _refresh_physical(tmp_path, facts)
    targets = json.loads(_prepare(tmp_path, facts, selection, helper).data)["targets"]
    assert targets[0]["current_rollout_path"] == str(original)
    assert targets[0]["current_rollout_id"] == str(_id(1).value)
    assert targets[0]["rollouts"][0]["rollout_path"] == str(compressed)


def test_missing_initial_physical_keeps_selected_logical_history_cache_key(tmp_path: Path) -> None:
    """Revert storage may replace the initial file without dropping its cache namespace."""
    import sqlite3  # noqa: PLC0415

    from tests.current_storage_fixture import _refresh_physical  # noqa: PLC0415

    facts, selection, helper = _fixture(tmp_path)
    initial = next(item.path for item in facts.rollouts if item.thread_id == _id(1))
    reverted = initial.with_name(
        initial.name.replace(f"{_id(1).value}.jsonl", f"{_id(1).value}_{_id(9).value}.jsonl")
    )
    initial.rename(reverted)
    with sqlite3.connect(tmp_path / "home" / "state_5.sqlite") as connection:
        connection.execute(
            "UPDATE threads SET rollout_path=? WHERE id=?", (str(reverted), str(_id(1).value))
        )
    facts = _refresh_physical(tmp_path, facts)
    facts = replace(
        facts,
        rollouts=tuple(
            replace(item, path=reverted, rollout_id=RolloutId(_id(9).value))
            if item.thread_id == _id(1)
            else item
            for item in facts.rollouts
        ),
    )
    targets = json.loads(_prepare(tmp_path, facts, selection, helper).data)["targets"]
    assert targets[0]["current_rollout_id"] == str(_id(9).value)
    assert history_cache_keys(decode_targets(targets)) == tuple(
        str(_id(i).value) for i in (1, 2, 9)
    )


@pytest.mark.parametrize("content", [b"unpublished decoded history", b""])
def test_materialization_temporary_object_never_enters_selected_manifest(
    tmp_path: Path,
    content: bytes,
) -> None:
    """Nonempty unpublished objects refuse; the empty unrelated exception preserves bytes."""
    from tests.current_storage_fixture import _refresh_physical  # noqa: PLC0415

    facts, selection, helper = _fixture(tmp_path)
    temporary = tmp_path / "home/sessions/.rollout-materialize-controlled"
    temporary.write_bytes(content)
    if content:
        with pytest.raises(InvalidRecordError, match=r"."):
            _refresh_physical(tmp_path, facts)
    else:
        observed = _refresh_physical(tmp_path, facts)
        targets = json.loads(_prepare(tmp_path, observed, selection, helper).data)["targets"]
        assert all(
            item["rollout_path"] != str(temporary)
            for target in targets
            for item in target["rollouts"]
        )
    assert temporary.read_bytes() == content
