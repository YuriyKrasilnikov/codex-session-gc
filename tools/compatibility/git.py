# Copyright (c) 2026

"""Bounded Git object comparisons without inspecting the working tree."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codex_session_gc.installation.process import OwnedProcessError, ProcessLimits, run_owned

if TYPE_CHECKING:
    from pathlib import Path

_LIMIT = ProcessLimits(seconds=30.0, output_bytes=8 * 1024 * 1024)
_ZERO = "0" * 40


class SourceError(ValueError):
    """A source object could not be compared completely."""


@dataclass(frozen=True, slots=True)
class Change:
    """One path and its old and new Git object identities."""

    path: str
    status: str
    old_mode: str
    new_mode: str
    old_object: str | None
    new_object: str | None


def _ref(value: str) -> bool:
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return True
    if re.fullmatch(r"refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]*", value) is None:
        return False
    return not any(part in value for part in ("..", "//", "@{")) and not value.endswith(
        ("/", ".", ".lock")
    )


async def _git(
    executable: Path, repository: Path, arguments: list[str], *, limits: ProcessLimits = _LIMIT
) -> bytes:
    if not executable.is_absolute():
        message = "trusted Git executable is unavailable"
        raise SourceError(message)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
    }
    command = [
        str(executable),
        "--no-pager",
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        *arguments,
    ]
    try:
        return await run_owned(command, cwd=repository, environment=environment, limits=limits)
    except OwnedProcessError as error:
        message = f"Git comparison failed: {error.reason.value}"
        raise SourceError(message) from error


async def resolve(executable: Path, repository: Path, ref: str) -> str:
    """Resolve a full commit ID or qualified branch/tag ref to an immutable commit."""
    if not _ref(ref):
        message = "source ref must be a full commit ID or qualified branch/tag"
        raise SourceError(message)
    raw = await _git(
        executable, repository, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"]
    )
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        message = "invalid Git object identity"
        raise SourceError(message) from error
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        message = "unsupported or malformed Git object identity"
        raise SourceError(message)
    return value


def _changes(raw: bytes) -> tuple[Change, ...]:
    if not raw:
        return ()
    fields = raw.split(b"\0")
    if fields[-1] != b"" or len(fields) % 2 != 1:
        message = "malformed Git diff output"
        raise SourceError(message)
    result: list[Change] = []
    for index in range(0, len(fields) - 1, 2):
        try:
            header = fields[index].decode("ascii")
            path = fields[index + 1].decode("utf-8")
        except UnicodeDecodeError as error:
            message = "Git diff contains an unsupported path or identity"
            raise SourceError(message) from error
        match = re.fullmatch(
            r":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([ADMT])", header
        )
        if match is None or not path or path.startswith("/"):
            message = "malformed or unsupported Git diff entry"
            raise SourceError(message)
        old_mode, new_mode, old_id, new_id, status = match.groups()
        result.append(
            Change(
                path,
                status,
                old_mode,
                new_mode,
                None if old_id == _ZERO else old_id,
                None if new_id == _ZERO else new_id,
            )
        )
    return tuple(result)


async def compare(
    executable: Path, repository: Path, baseline: str, target: str
) -> tuple[str, str, tuple[Change, ...], bool]:
    """Resolve commits and compare their trees, refusing missing or unsupported objects."""
    old = await resolve(executable, repository, baseline)
    new = await resolve(executable, repository, target)
    shallow = await _git(executable, repository, ["rev-parse", "--is-shallow-repository"])
    if shallow not in (b"true\n", b"false\n"):
        message = "invalid shallow repository status"
        raise SourceError(message)
    raw = await _git(
        executable,
        repository,
        [
            "diff-tree",
            "--no-commit-id",
            "--raw",
            "--no-abbrev",
            "--no-renames",
            "--ignore-submodules=none",
            "--no-ext-diff",
            "--no-textconv",
            "-r",
            "-z",
            old,
            new,
            "--",
        ],
    )
    return old, new, _changes(raw), shallow == b"true\n"


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One immutable path and its Git object identity."""

    path: str
    mode: str
    kind: str
    object_id: str
    size: int | None = None


async def tree(executable: Path, repository: Path, commit: str) -> tuple[TreeEntry, ...]:
    """Enumerate a bounded immutable tree without opening worktree files."""
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        message = "invalid immutable commit identity"
        raise SourceError(message)
    raw = await _git(executable, repository, ["ls-tree", "-r", "-l", "-z", "--full-tree", commit])
    fields = raw.split(b"\0")
    if fields[-1] != b"":
        message = "malformed Git tree output"
        raise SourceError(message)
    entries: list[TreeEntry] = []
    for field in fields[:-1]:
        try:
            metadata, path_bytes = field.split(b"\t", 1)
            mode, kind, object_id, size_text = metadata.decode("ascii").split()
            path = path_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            message = "unsupported Git tree entry"
            raise SourceError(message) from error
        if (
            re.fullmatch(r"[0-7]{6}", mode) is None
            or kind not in {"blob", "commit"}
            or re.fullmatch(r"[0-9a-f]{40}", object_id) is None
            or not path
            or path.startswith("/")
            or (kind == "blob" and (not size_text.isascii() or not size_text.isdecimal()))
            or (kind == "commit" and size_text != "-")
        ):
            message = "malformed Git tree entry"
            raise SourceError(message)
        entries.append(
            TreeEntry(path, mode, kind, object_id, int(size_text) if kind == "blob" else None)
        )
    return tuple(entries)


async def blobs(
    executable: Path, repository: Path, objects: tuple[tuple[str, int], ...], *, seconds: float
) -> tuple[bytes, ...]:
    """Read exact known-size blobs in order, verifying each Git identity."""
    if not objects or any(
        re.fullmatch(r"[0-9a-f]{40}", object_id) is None or type(size) is not int or size < 0
        for object_id, size in objects
    ):
        message = "invalid blob batch"
        raise SourceError(message)
    total = sum(size for _, size in objects)
    raw = await _git(
        executable,
        repository,
        ["show", "--no-textconv", "--format=", *(item[0] for item in objects)],
        limits=ProcessLimits(seconds=seconds, output_bytes=max(1, total)),
    )
    if len(raw) != total:
        message = "blob batch length mismatch"
        raise SourceError(message)
    result: list[bytes] = []
    offset = 0
    for object_id, size in objects:
        content = raw[offset : offset + size]
        digest = hashlib.new("sha1", usedforsecurity=False)
        digest.update(f"blob {size}\0".encode())
        digest.update(content)
        if digest.hexdigest() != object_id:
            message = "blob batch identity mismatch"
            raise SourceError(message)
        result.append(content)
        offset += size
    return tuple(result)


class ObservationContext:
    """Hold verified immutable observations for one bounded comparison."""

    def __init__(
        self, executable: Path, repository: Path, *, cache_bytes: int, seconds: float
    ) -> None:
        """Bind the local source and maximum retained blob bytes."""
        self.executable = executable
        self.repository = repository
        self.cache_bytes = cache_bytes
        self.seconds = seconds
        self._trees: dict[str, tuple[TreeEntry, ...]] = {}
        self._blobs: dict[str, bytes] = {}
        self._used = 0

    async def tree(self, commit: str) -> tuple[TreeEntry, ...]:
        """Reuse only the tree observed for the same immutable commit."""
        if commit not in self._trees:
            self._trees[commit] = await tree(self.executable, self.repository, commit)
        return self._trees[commit]

    async def read(self, objects: tuple[tuple[str, int], ...]) -> tuple[bytes, ...]:
        """Return exact verified blobs, refusing to exceed the invocation cache."""
        missing: dict[str, int] = {}
        for object_id, size in objects:
            if object_id in self._blobs:
                if len(self._blobs[object_id]) != size:
                    message = "cached blob size differs"
                    raise SourceError(message)
            elif object_id in missing and missing[object_id] != size:
                message = "inconsistent blob size"
                raise SourceError(message)
            else:
                missing[object_id] = size
        if self._used + sum(missing.values()) > self.cache_bytes:
            message = "blob observation cache limit exceeded"
            raise SourceError(message)
        if missing:
            requested = tuple(missing.items())
            observed = await blobs(
                self.executable, self.repository, requested, seconds=self.seconds
            )
            for (object_id, size), content in zip(requested, observed, strict=True):
                self._blobs[object_id] = content
                self._used += size
        return tuple(self._blobs[object_id] for object_id, _ in objects)
