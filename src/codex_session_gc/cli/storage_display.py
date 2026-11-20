# Copyright (c) 2026

"""CLI presentation of detached storage observations."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

from codex_session_gc.inventory.storage import PAGE_LIMIT, observe_storage

if TYPE_CHECKING:
    from argparse import Namespace


def _bytes(data: dict[str, int]) -> str:
    return f"{data['known_bytes']:,} bytes ({data['unknown_objects']} unknown objects)"


def _terminal(result: dict[str, Any]) -> str:
    total = result["totals"]
    lines = [
        (
            f"Filesystem {result['fs_coverage']}  "
            f"{result['fs_started_at']} → {result['fs_finished_at']}"
        ),
        (
            f"SQL {result['sql_coverage']}  {result['sql_started_at'] or '—'} → "
            f"{result['sql_finished_at'] or '—'}"
        ),
        f"Allocated {_bytes(total['allocated_bytes'])}",
        f"Logical {_bytes(total['logical_file_bytes'])}",
        "",
    ]
    for item in result["categories"]:
        allocated = _bytes(item["allocated_bytes"])
        logical = _bytes(item["logical_file_bytes"])
        lines.append(f"{item['category']}  allocated {allocated}, logical {logical}")
    for reason, count in sorted(result["counts_by_reason"].items()):
        lines.append(f"  {reason}: {count}")
    if page := result.get("objects"):
        lines.append(f"\nHistory names: {page['total_matching']} matching")
        lines.extend(f"  {item['association']}  {item['display_path']}" for item in page["items"])
    return "\n".join(lines)


def run_storage(arguments: Namespace) -> int:
    """Show one summary and optionally a bounded page of history names."""
    if arguments.objects is None and (arguments.offset is not None or arguments.limit is not None):
        sys.stderr.write("storage: --offset and --limit require --objects\n")
        return 2
    offset = arguments.offset if arguments.offset is not None else 0
    limit = arguments.limit if arguments.limit is not None else PAGE_LIMIT
    if offset < 0 or not 1 <= limit <= PAGE_LIMIT:
        sys.stderr.write("storage: invalid page arguments\n")
        return 2
    try:
        snapshot = observe_storage(arguments.home.expanduser())
        result = snapshot.summary()
        if arguments.objects is not None:
            result["objects"] = snapshot.page(
                association=arguments.objects, offset=offset, limit=limit
            )
    except (OSError, ValueError) as error:
        sys.stderr.write(f"storage: observation unavailable ({type(error).__name__})\n")
        return 2
    text = (
        json.dumps(result, sort_keys=True, ensure_ascii=True)
        if arguments.json
        else _terminal(result)
    )
    sys.stdout.write(text + "\n")
    return 0
