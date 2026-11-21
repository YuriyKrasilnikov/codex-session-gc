# Copyright (c) 2026

"""Observe the finite managed physical namespace without retaining raw history headers."""

from __future__ import annotations

import os
import stat
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from codex_session_gc.domain import (
    Attachment,
    CurrentRolloutPointer,
    FileStatIdentity,
    HistoryMode,
    HistoryPrefix,
    InvalidRecordError,
    InventoryCompleteness,
    LogicalForkBoundary,
    PhysicalIdentity,
    PhysicalInventory,
    PhysicalRepresentation,
    RolloutId,
    RolloutMetadata,
    ThreadId,
)
from codex_session_gc.domain.physical import ownership_from_path
from codex_session_gc.execution.closed_work_request import (
    CollectionBudget,
    HeaderObservation,
    PreparationRefused,
    _absolute,
    _capture_header_descriptor,
    _decode_header,
    _empty_descriptor,
    _verify_header_binding,
)
from codex_session_gc.execution.timing import Count, count
from codex_session_gc.planning.physical_lineage import normalize_history_providers

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping
    from concurrent.futures import Future

    from codex_session_gc.execution.closed_work_request import _CapturedHeader


_PATH_CHECK_WINDOW = 4


@dataclass(frozen=True, slots=True)
class _Entry:
    path: Path
    parent_fd: int


@dataclass(frozen=True, slots=True)
class _PendingHeader:
    captured: _CapturedHeader
    representation: PhysicalRepresentation
    original: ExitStack
    check: Future[None]


@contextmanager
def _leaf(entry: _Entry) -> Iterator[int]:
    """Own one regular file opened under a live traversal descriptor."""
    _absolute(entry.path)
    descriptor = os.open(
        entry.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=entry.parent_fd
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            message = "Only regular native input files are supported."
            raise PreparationRefused(message)
        yield descriptor
    finally:
        os.close(descriptor)


def _open_directory(path: Path) -> int:
    """Open every component of a managed namespace without following aliases."""
    _absolute(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        result = descriptor
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _files(home: Path) -> Generator[_Entry]:
    for name in ("sessions", "archived_sessions"):
        root = home / name
        try:
            descriptor = _open_directory(root)
        except FileNotFoundError:
            continue
        yield from _directory(descriptor, root)


def _directory(descriptor: int, path: Path) -> Generator[_Entry]:
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if entry.is_symlink():
                    message = "A managed physical namespace contains an unresolved alias."
                    raise PreparationRefused(message)
                child = path / entry.name
                if entry.is_dir(follow_symlinks=False):
                    nested = os.open(
                        entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                    )
                    yield from _directory(nested, child)
                elif entry.is_file(follow_symlinks=False):
                    yield _Entry(child, descriptor)
                else:
                    message = "A managed physical namespace contains an unsupported object."
                    raise PreparationRefused(message)
    finally:
        os.close(descriptor)


def _metadata(header: HeaderObservation, representation: PhysicalRepresentation) -> RolloutMetadata:
    ownership = ownership_from_path(header.path, representation)
    metadata = header.metadata
    if metadata.get("id") != str(ownership.thread_id.value):
        message = "Physical header owner disagrees with its canonical filename."
        raise PreparationRefused(message)
    prefix = metadata.get("history_base")
    if prefix is not None:
        if not isinstance(prefix, dict) or set(prefix) != {
            "thread_id",
            "end_ordinal_exclusive",
            "end_byte_offset",
        }:
            message = "A physical history prefix is malformed."
            raise PreparationRefused(message)
        prefix = HistoryPrefix(
            RolloutId.parse(prefix["thread_id"]),
            prefix["end_ordinal_exclusive"],
            prefix["end_byte_offset"],
        )
    fork = metadata.get("forked_from_id")
    coordinate = metadata.get("forked_from_ordinal_exclusive")
    if coordinate is not None and type(coordinate) is not int:
        message = "The logical fork coordinate must be an explicit u64 or absent."
        raise PreparationRefused(message)
    attached = metadata.get("attached_thread", False)
    if type(attached) is not bool:
        message = "Attachment flag must be boolean or absent."
        raise PreparationRefused(message)
    attachment = None
    if attached:
        try:
            attachment = Attachment(ownership.thread_id, ThreadId.parse(fork))
        except (TypeError, ValueError) as error:
            message = "An attached thread requires a distinct fork source."
            raise PreparationRefused(message) from error
    raw_session = metadata.get("session_id", str(ownership.thread_id.value))
    source = metadata.get("source", "vscode")
    non_root = (
        isinstance(source, dict)
        and len(source) == 1
        and next(iter(source)) in ("internal", "subagent")
    )
    valid_source = isinstance(source, str) or (
        isinstance(source, dict)
        and len(source) == 1
        and next(iter(source)) in ("internal", "subagent", "custom")
    )
    try:
        session_id = ThreadId.parse(raw_session)
    except TypeError, ValueError:
        session_id = None
    ambiguous = (
        not valid_source
        or session_id is None
        or (non_root and ("session_id" not in metadata or session_id == ownership.thread_id))
    )
    return RolloutMetadata(
        ownership=ownership,
        history_mode=HistoryMode(str(metadata.get("history_mode", "legacy"))),
        physical=PhysicalIdentity(
            header.path,
            representation,
            FileStatIdentity(*header.identity),
            header.decoded_header_sha256,
        ),
        history_prefix=prefix,
        logical_fork=None
        if fork is None
        else LogicalForkBoundary(ThreadId.parse(fork), coordinate),
        attachment=attachment,
        session_id=session_id,
        ambiguous_agent_session=ambiguous,
    )


def _current_matches(
    path: Path, representation: PhysicalRepresentation, row: Mapping[str, object] | None
) -> bool:
    if row is None:
        return False
    raw = Path(str(row["rollout_path"]))
    plain = path.with_suffix("") if representation is PhysicalRepresentation.ZSTD else path
    return raw in (plain, plain.with_name(plain.name + ".zst"))


def _join_path_checks(executor: ThreadPoolExecutor) -> KeyboardInterrupt | None:
    """Finish shutdown even if the caller is interrupted while joining workers."""
    interruption = None
    while True:
        try:
            executor.shutdown(wait=True, cancel_futures=True)
        except KeyboardInterrupt as error:
            if interruption is None:
                interruption = error
        else:
            return interruption


def _submit_path_check(executor: ThreadPoolExecutor, captured: _CapturedHeader) -> Future[None]:
    """Admit filesystem access only after the executor owns the started worker."""
    admission = Lock()
    admitted = False

    def check() -> None:
        with admission:
            accepted = admitted
        if accepted:
            _verify_header_binding(captured)

    # An interrupted Thread.start can leave work runnable before submit returns.
    # Hold it here so a worker untracked by shutdown cannot verify a released leaf.
    with admission:
        result = executor.submit(check)
        admitted = True
        return result


def _inventory_headers(  # noqa: C901, PLR0912, PLR0915
    home: Path,
    current_rows: Mapping[str, Mapping[str, object]],
    capture_owners: frozenset[ThreadId],
    budget: CollectionBudget,
    unresolved_empty_rollouts: list[Path],
) -> Generator[tuple[HeaderObservation, PhysicalRepresentation]]:
    """Capture in the caller and retire verified pathname checks in traversal order."""
    executor: ThreadPoolExecutor | None = None
    pending: deque[_PendingHeader] = deque()
    submitting: ExitStack | None = None
    seen = set()
    entries_seen = 0
    headers_read = 0
    initial_used = budget.used

    def retire() -> tuple[HeaderObservation, PhysicalRepresentation]:
        nonlocal headers_read
        item = pending[0]
        item.check.result()
        item.original.close()
        pending.popleft()
        header = _decode_header(item.captured)
        headers_read += 1
        return header, item.representation

    def capture(
        path: Path, descriptor: int
    ) -> tuple[_CapturedHeader, PhysicalRepresentation] | None:
        representation = (
            PhysicalRepresentation.ZSTD
            if path.name.endswith(".jsonl.zst")
            else PhysicalRepresentation.PLAIN
        )
        empty = _empty_descriptor(path, descriptor)
        try:
            ownership = ownership_from_path(path, representation)
        except InvalidRecordError:
            if empty:
                return None
            raise
        if ownership.rollout_id in seen:
            message = "One physical rollout has duplicate or competing representations."
            raise PreparationRefused(message)
        seen.add(ownership.rollout_id)
        selected = ownership.thread_id in capture_owners
        if empty:
            current = _current_matches(
                path,
                representation,
                current_rows.get(str(ownership.thread_id.value)),
            )
            if selected or current:
                message = "A selected or current physical history is empty."
                raise PreparationRefused(message)
            unresolved_empty_rollouts.append(path)
            return None
        captured = _capture_header_descriptor(
            path,
            descriptor,
            budget,
            require_stable_extent=selected,
            enforce_extent_limit=selected,
        )
        return captured, representation

    failed = False
    try:
        with closing(_files(home)) as entries:
            while True:
                if len(pending) == _PATH_CHECK_WINDOW:
                    yield retire()
                try:
                    entry = next(entries, None)
                    if entry is None:
                        break
                    entries_seen += 1
                    budget.check()
                    with ExitStack() as original:
                        descriptor = original.enter_context(_leaf(entry))
                        staged = capture(entry.path, descriptor)
                        if staged is None:
                            continue
                        captured, representation = staged
                        if executor is None:
                            executor = ThreadPoolExecutor(max_workers=_PATH_CHECK_WINDOW)
                        # Submission can enqueue work and then fail to start a thread.
                        # Keep the pin until shutdown even if no Future was returned.
                        submitting = original.pop_all()
                        check = _submit_path_check(executor, captured)
                        pending.append(_PendingHeader(captured, representation, submitting, check))
                        submitting = None
                except Exception:
                    # A later traversal, capture or submission error must wait for
                    # every earlier admitted pathname and metadata verdict.
                    while pending:
                        yield retire()
                    raise
            while pending:
                yield retire()
    except BaseException:
        failed = True
        raise
    finally:
        interruption = None
        try:
            if executor is not None:
                interruption = _join_path_checks(executor)
        finally:
            with ExitStack() as originals:
                if submitting is not None:
                    originals.callback(submitting.close)
                for item in pending:
                    originals.callback(item.original.close)
            count(Count.INVENTORY_ENTRIES, entries_seen)
            count(Count.HEADERS_READ, headers_read)
            count(Count.INVENTORY_BUDGET_BYTES, budget.used - initial_used)
        if interruption is not None and not failed:
            raise interruption


def observe_inventory(
    home: Path,
    current_rows: Mapping[str, Mapping[str, object]],
    capture_owners: frozenset[ThreadId],
    budget: CollectionBudget,
) -> tuple[PhysicalInventory, dict[Path, HeaderObservation]]:
    """Read every active/archive header, including orphan consumer owners.

    Definitely empty regular files preserve the qualified zero-file exception.
    Only headers needed by current SQL pointers are retained as small projections.
    """
    items = []
    unresolved_empty_rollouts: list[Path] = []
    headers = {}
    with closing(
        _inventory_headers(home, current_rows, capture_owners, budget, unresolved_empty_rollouts)
    ) as verified:
        for header, representation in verified:
            item = _metadata(header, representation)
            items.append(item)
            observed_owner = item.ownership.thread_id
            if observed_owner in capture_owners and _current_matches(
                header.path, representation, current_rows.get(str(observed_owner.value))
            ):
                headers[header.path] = header
            # Earlier path, envelope and metadata refusals outrank a deadline
            # reached while waiting for their pathname checks.
            budget.check()
    pointers = []
    for owner, row in current_rows.items():
        raw = Path(str(row["rollout_path"]))
        representation = (
            PhysicalRepresentation.ZSTD
            if raw.name.endswith(".jsonl.zst")
            else PhysicalRepresentation.PLAIN
        )
        ownership = ownership_from_path(raw, representation)
        if str(ownership.thread_id.value) != owner:
            message = "Current SQL pointer logical identity disagrees."
            raise PreparationRefused(message)
        pointers.append(CurrentRolloutPointer(ownership, raw))
    inventory = PhysicalInventory(
        tuple(items),
        tuple(pointers),
        InventoryCompleteness.COMPLETE,
        alias_coverage=True,
        unresolved_empty_rollouts=tuple(unresolved_empty_rollouts),
    )
    normalize_history_providers(inventory)
    budget.check()
    return inventory, headers
