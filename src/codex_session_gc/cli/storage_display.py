# Copyright (c) 2026

"""CLI presentation of detached storage observations."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

from codex_session_gc.inventory.storage import PAGE_LIMIT, observe_storage
from codex_session_gc.inventory.storage_diagnostics import observe_diagnostics

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


def _identity(subject: dict[str, Any] | None) -> str:
    if subject is None:
        return "unresolved endpoint"
    roles = [subject["kind"]]
    if subject["object_id"] is not None:
        roles.append(subject["object_id"])
    if subject["thread_id"] is not None:
        roles.append(f"thread {subject['thread_id']}")
    if subject["rollout_id"] is not None:
        roles.append(f"rollout {subject['rollout_id']}")
    return " · ".join(roles)


def diagnostic_subject_line(subject: dict[str, Any]) -> str:
    """Describe a physical, SQL-only or dangling observation from detached fields."""
    facts = []
    if subject["original_association"] is not None:
        facts.append(f"at scan {subject['original_association']}")
    facts.extend((f"SQL owner {subject['sql_owner']}", f"pointer {subject['current_pointer']}"))
    return " · ".join([_identity(subject), *facts, *subject["reasons"]])


def diagnostic_evidence_line(evidence: dict[str, Any]) -> str:
    """Describe the origin and both roles of a direct or derived relationship."""
    source = _identity(evidence["source_subject"])
    target = _identity(evidence["target_subject"])
    detail = " · ".join(value for key in ("reason", "status") if (value := evidence[key]))
    if provider := evidence.get("provider_rollout_id"):
        target = f"{target} · provider rollout {provider}"
    relation = f"{evidence.get('interpretation', evidence['kind'])} ({evidence['origin']})"
    return f"{relation}  {source} → {target}" + (f" · {detail}" if detail else "")


def _diagnostic_terminal(result: dict[str, Any], page: dict[str, Any]) -> str:
    coverage = ", ".join(f"{name}={state}" for name, state in result["coverage"].items())
    lines = [f"Coverage · {coverage}"]
    if status := page.get("status"):
        lines.append(str(status))
    subject = page.get("subject")
    if subject is not None:
        lines.append(diagnostic_subject_line(subject))
    render = diagnostic_evidence_line if subject is not None else diagnostic_subject_line
    lines.extend(render(item) for item in page["items"])
    lines.append(f"{page['total_matching']} matching observations")
    if page["next_offset"] is not None:
        lines.append(f"Next page · --offset {page['next_offset']}")
    return "\n".join(lines)


def run_storage_diagnostics(arguments: Namespace) -> int:
    """Observe once and display a page of problems or a typed UUID lookup."""
    if arguments.offset < 0 or not 1 <= arguments.limit <= PAGE_LIMIT:
        sys.stderr.write("storage-diagnostics: invalid page arguments\n")
        return 2
    try:
        home = arguments.home.expanduser().resolve()
        observation = observe_diagnostics(
            home, observe_storage(home), providers=arguments.providers
        )
        bounds = {"offset": arguments.offset, "limit": arguments.limit}
        if arguments.thread is None and arguments.rollout is None:
            page = observation.subjects(reason="problems", **bounds)
        else:
            page = observation.lookup(
                thread_id=arguments.thread, rollout_id=arguments.rollout, **bounds
            )
    except (OSError, ValueError) as error:
        sys.stderr.write(f"storage-diagnostics: observation unavailable ({type(error).__name__})\n")
        return 2
    result = observation.summary()
    text = (
        json.dumps({**result, "page": page}, sort_keys=True, ensure_ascii=True)
        if arguments.json
        else _diagnostic_terminal(result, page)
    )
    sys.stdout.write(text + "\n")
    return 0
