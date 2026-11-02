# Copyright (c) 2026

"""Project source changes onto explicitly supplied compatibility boundaries."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NoReturn

from codex_session_gc.installation.process import OwnedProcessError, ProcessFailure
from tools.compatibility import cargo, git

if TYPE_CHECKING:
    from pathlib import Path

_MAX_BOUNDARIES = 1024 * 1024
_MAX_CHANGED_BLOBS = 128
_MAX_CHANGED_BYTES = 32 * 1024 * 1024
_BLOB_TIMEOUT = 120.0
_MAX_TEXT = 512
_MAX_NAME = 128
_CONTROL = 32
_BASE_GAPS = (
    "cargo_dependency_closure_not_established",
    "macro_build_include_and_caller_coverage_not_established",
    "changed_blob_contents_not_verified",
)


class BoundaryError(ValueError):
    """Provided boundary declarations are malformed or bound to other source."""


@dataclass(frozen=True, slots=True)
class Boundary:
    """A named caller-provided boundary with path scopes and witness references."""

    name: str
    paths: tuple[str, ...]
    witnesses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Declarations:
    """Source-bound declarations that remain subject to independent review."""

    boundaries: tuple[Boundary, ...]
    outside: tuple[str, ...]


def _fail(message: str) -> NoReturn:
    raise BoundaryError(message)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate declaration key")
        value[key] = item
    return value


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _path(value: object) -> bool:
    if not isinstance(value, str) or not value or "://" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in (".", "..") for part in path.parts)
    )


def _strings(value: object, *, paths: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _fail("declaration values must be strings")
    result: list[str] = []
    for item in value:
        if paths:
            valid = _path(item)
        else:
            valid = (
                bool(item)
                and len(item) <= _MAX_TEXT
                and "://" not in item
                and not any(ord(character) < _CONTROL for character in item)
            )
        if not valid or item in result:
            _fail("declaration contains an invalid or duplicate value")
        result.append(item)
    return tuple(result)


def read_declarations(
    path: Path, baseline: str, profile_sha256: str, native_inputs_sha256: str
) -> Declarations:
    """Validate bounded supplied declarations against the exact baseline identities."""
    if path.is_symlink() or not path.is_file():
        _fail("boundary declaration is not a regular file")
    with path.open("rb") as source:
        raw = source.read(_MAX_BOUNDARIES + 1)
    if not raw or len(raw) > _MAX_BOUNDARIES:
        _fail("boundary declaration exceeds its bound")
    try:
        value: object = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeDecodeError) as error:
        message = "boundary declaration is invalid JSON"
        raise BoundaryError(message) from error
    if not isinstance(value, dict) or set(value) != {
        "format",
        "baseline_commit",
        "baseline_profile_sha256",
        "baseline_native_inputs_sha256",
        "boundaries",
        "outside",
    }:
        _fail("boundary declaration fields differ")
    if (
        value.get("format") != "codex-session-gc-compatibility-boundaries-v1"
        or value.get("baseline_commit") != baseline
        or value.get("baseline_profile_sha256") != profile_sha256
        or value.get("baseline_native_inputs_sha256") != native_inputs_sha256
        or not _digest(profile_sha256)
        or not _digest(native_inputs_sha256)
    ):
        _fail("boundary declaration identity differs")
    return _declarations(value)


def _declarations(value: dict[str, object]) -> Declarations:
    entries = value.get("boundaries")
    if not isinstance(entries, list):
        _fail("boundaries must be a list")
    result: list[Boundary] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "paths", "witnesses"}:
            _fail("boundary entry is invalid")
        name = entry.get("name")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > _MAX_NAME
            or "://" in name
            or any(ord(c) < _CONTROL for c in name)
        ):
            _fail("boundary name is invalid")
        paths = _strings(entry.get("paths"), paths=True)
        witnesses = _strings(entry.get("witnesses"), paths=False)
        if not paths or not witnesses or any(item.name == name for item in result):
            _fail("boundary requires unique name, paths and witnesses")
        result.append(Boundary(name, paths, witnesses))
    return Declarations(tuple(result), _strings(value.get("outside"), paths=True))


def _contains(path: str, scope: str) -> bool:
    return path == scope or path.startswith(scope + "/")


def _project(
    changes: tuple[git.Change, ...], declarations: Declarations | None
) -> tuple[list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    gaps: list[str] = []
    for change in changes:
        matched = (
            []
            if declarations is None
            else [
                boundary
                for boundary in declarations.boundaries
                if any(_contains(change.path, scope) for scope in boundary.paths)
            ]
        )
        outside = declarations is not None and any(
            _contains(change.path, scope) for scope in declarations.outside
        )
        if not matched and not outside:
            gaps.append("unclassified_changed_path")
        if matched and outside:
            gaps.append("conflicting_boundary_declaration")
        if "160000" in (change.old_mode, change.new_mode):
            gaps.append("submodule_content_not_compared")
        rows.append(
            {
                **asdict(change),
                "boundaries": [
                    {"name": boundary.name, "witnesses": list(boundary.witnesses)}
                    for boundary in matched
                ],
                "declared_outside": outside,
            }
        )
    return rows, sorted(set(gaps))


@dataclass(frozen=True, slots=True)
class ObservationLimits:
    """Explicit finite bounds for immutable source observation."""

    cargo: cargo.CargoLimits = field(default_factory=cargo.CargoLimits)
    objects: int = _MAX_CHANGED_BLOBS
    object_bytes: int = _MAX_CHANGED_BYTES
    seconds: float = _BLOB_TIMEOUT

    def __post_init__(self) -> None:
        """Reject invalid and excessive observation limits."""
        max_objects = 100_000
        max_bytes = 1_073_741_824
        max_seconds = 3600
        if (
            not isinstance(self.cargo, cargo.CargoLimits)
            or type(self.objects) is not int
            or not 0 < self.objects <= max_objects
            or type(self.object_bytes) is not int
            or not 0 < self.object_bytes <= max_bytes
            or type(self.seconds) not in (int, float)
            or not 0 < self.seconds <= max_seconds
        ):
            message = "Invalid source observation limits."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Request:
    """Explicit local source and optional source-bound declaration inputs."""

    executable: Path
    repository: Path
    baseline: str | None
    target: str | None
    declarations_path: Path | None
    profile_sha256: str | None
    native_inputs_sha256: str | None
    workspace: str | None = None
    cargo_roots: tuple[str, ...] = ()
    helper_sha256: str | None = None
    limits: ObservationLimits = field(default_factory=ObservationLimits)


async def _cargo_report(
    request: Request,
    commits: tuple[str, str],
    changes: tuple[git.Change, ...],
    rows: list[dict[str, object]],
    observation: git.ObservationContext,
) -> tuple[dict[str, object], tuple[str, ...]]:
    if request.workspace is None or not request.cargo_roots:
        return {}, ("cargo_root_inputs_missing",)
    old, new = commits
    old_envelope = await cargo.derive(
        request.executable,
        request.repository,
        old,
        cargo.CargoInputs(
            request.workspace, request.cargo_roots, request.limits.cargo, observation
        ),
    )
    new_envelope = await cargo.derive(
        request.executable,
        request.repository,
        new,
        cargo.CargoInputs(
            request.workspace, request.cargo_roots, request.limits.cargo, observation
        ),
    )
    result: dict[str, object] = {
        "cargo": {"baseline": asdict(old_envelope), "target": asdict(new_envelope)}
    }
    closure_objects = {node.object_id for node in (*old_envelope.nodes, *new_envelope.nodes)}
    closure_observation = await _verify_objects(request, closure_objects, commits, observation)
    closure_checked = bool(closure_objects) and closure_observation.complete
    result["closure_blobs_checked"] = closure_checked
    result["closure_blob_observation"] = asdict(closure_observation)
    old_objects = {node.path: node.object_id for node in old_envelope.nodes}
    new_objects = {node.path: node.object_id for node in new_envelope.nodes}
    old_partial = bool(old_envelope.gaps)
    new_partial = bool(new_envelope.gaps)
    if old_partial or new_partial:
        result["captured_cargo_envelope_objects_equal"] = None
    else:
        result["captured_cargo_envelope_objects_equal"] = old_objects == new_objects
    old_manifests = {fact.path: fact for fact in old_envelope.manifests}
    new_manifests = {fact.path: fact for fact in new_envelope.manifests}
    for change, row in zip(changes, rows, strict=True):
        row["cargo_envelope"] = {
            "baseline": True if change.path in old_objects else None if old_partial else False,
            "target": True if change.path in new_objects else None if new_partial else False,
        }
        old_fact = old_manifests.get(change.path)
        new_fact = new_manifests.get(change.path)
        if (
            old_fact is not None
            and new_fact is not None
            and old_fact.version != new_fact.version
            and old_fact.without_version_sha256 == new_fact.without_version_sha256
        ):
            row["metadata_fact"] = "parsed_package_version_only_not_semantic_equivalence"
    extra = () if closure_checked else ("closure_blob_missing_or_budget_exceeded",)
    return result, (*old_envelope.gaps, *new_envelope.gaps, *extra)


@dataclass(frozen=True, slots=True)
class BlobObservation:
    """The exact Git blobs verified before a bounded observation stopped."""

    object_ids: tuple[str, ...]
    complete: bool
    failure: str | None


def _object_sizes(entries: tuple[git.TreeEntry, ...]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for entry in entries:
        if entry.kind == "blob" and entry.size is not None:
            previous = sizes.setdefault(entry.object_id, entry.size)
            if previous != entry.size:
                message = "object_size_mismatch"
                raise git.SourceError(message)
    return sizes


async def _verify_sequence(
    request: Request,
    objects: set[str],
    sizes: dict[str, int],
    checked: list[str],
    observation: git.ObservationContext,
) -> BlobObservation | None:
    total = 0
    batch: list[tuple[str, int]] = []
    batch_bytes = 0
    max_batch_objects = 64
    max_batch_bytes = 4 * 1024 * 1024
    for object_id in sorted(objects):
        size = sizes.get(object_id)
        if size is None:
            return BlobObservation(
                tuple(checked), complete=False, failure="object_metadata_missing"
            )
        if total + size > request.limits.object_bytes:
            return BlobObservation(tuple(checked), complete=False, failure="object_byte_limit")
        if batch and (len(batch) >= max_batch_objects or batch_bytes + size > max_batch_bytes):
            result = await _verify_batch(batch, checked, observation)
            if result is not None:
                return result
            batch = []
            batch_bytes = 0
        batch.append((object_id, size))
        batch_bytes += size
        total += size
    if batch:
        return await _verify_batch(batch, checked, observation)
    return None


async def _verify_objects(
    request: Request,
    objects: set[str],
    commits: tuple[str, str],
    observation: git.ObservationContext,
) -> BlobObservation:
    if len(objects) > request.limits.objects:
        return BlobObservation((), complete=False, failure="object_count_limit")
    checked: list[str] = []
    try:
        async with asyncio.timeout(request.limits.seconds):
            entries = (
                *await observation.tree(commits[0]),
                *await observation.tree(commits[1]),
            )
            result = await _verify_sequence(
                request, objects, _object_sizes(entries), checked, observation
            )
            if result is not None:
                return result
    except git.SourceError as error:
        cause = error.__cause__
        failure = (
            "object_timeout"
            if isinstance(cause, OwnedProcessError) and cause.reason is ProcessFailure.TIMEOUT
            else "object_unavailable_or_over_limit"
        )
        return BlobObservation(tuple(checked), complete=False, failure=failure)
    except TimeoutError:
        return BlobObservation(tuple(checked), complete=False, failure="object_timeout")
    return BlobObservation(tuple(checked), complete=True, failure=None)


async def _verify_batch(
    batch: list[tuple[str, int]], checked: list[str], observation: git.ObservationContext
) -> BlobObservation | None:
    bodies = await observation.read(tuple(batch))
    for (object_id, _), raw in zip(batch, bodies, strict=True):
        checked.append(object_id)
        if raw.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            return BlobObservation(
                tuple(checked), complete=False, failure="git_lfs_content_unavailable"
            )
    return None


async def _verify_changed_blobs(
    request: Request,
    changes: tuple[git.Change, ...],
    commits: tuple[str, str],
    observation: git.ObservationContext,
) -> BlobObservation:
    if any("160000" in (change.old_mode, change.new_mode) for change in changes):
        return BlobObservation((), complete=False, failure="submodule_object_not_observed")
    objects = {
        object_id
        for change in changes
        for object_id in (change.old_object, change.new_object)
        if object_id is not None
    }
    return await _verify_objects(request, objects, commits, observation)


async def assess(request: Request) -> dict[str, object]:
    """Report observed source changes and explicit limits, without qualifying a helper."""
    report: dict[str, object] = {
        "format": "codex-session-gc-compatibility-report-v1",
        "status": "unknown",
        "source_mode": "immutable_git_objects_worktree_not_read",
        "baseline": None,
        "target": None,
        "changes": None,
        "helper_inputs_unchanged": "unknown",
        "execution_contract_change": "unreviewed",
        "acceptance": "requires_separate_review",
        "gaps": list(_BASE_GAPS),
    }
    gaps = list(_BASE_GAPS)
    if request.baseline is None or request.target is None:
        report["gaps"] = [*gaps, "source_ref_missing"]
        return report
    try:
        old, new, changes, shallow = await git.compare(
            request.executable, request.repository, request.baseline, request.target
        )
    except git.SourceError as error:
        report["gaps"] = [*gaps, str(error)]
        return report
    report["baseline"] = {"requested": request.baseline, "commit": old}
    report["target"] = {"requested": request.target, "commit": new}
    if shallow:
        gaps.append("shallow_repository")
    observation = git.ObservationContext(
        request.executable,
        request.repository,
        cache_bytes=request.limits.object_bytes,
        seconds=request.limits.seconds,
    )
    commits = (old, new)
    blob_observation = await _verify_changed_blobs(request, changes, commits, observation)
    report["changed_blobs_checked"] = blob_observation.complete
    report["changed_blob_observation"] = asdict(blob_observation)
    if blob_observation.complete:
        gaps.remove("changed_blob_contents_not_verified")
    else:
        gaps.append("changed_blob_missing_or_budget_exceeded")
    declarations = None
    if (
        request.declarations_path is None
        or request.profile_sha256 is None
        or request.native_inputs_sha256 is None
    ):
        gaps.append("baseline_boundary_declarations_missing")
    else:
        try:
            declarations = read_declarations(
                request.declarations_path, old, request.profile_sha256, request.native_inputs_sha256
            )
        except (BoundaryError, OSError) as error:
            gaps.append("baseline_boundary_declarations_invalid")
            report["declaration_error"] = (
                str(error)
                if isinstance(error, BoundaryError)
                else "unreadable boundary declaration"
            )
        else:
            report["boundary_authority"] = "caller_supplied_declarations_not_independently_verified"
            report["execution_identity_authority"] = "caller_supplied_not_independently_observed"
            report["baseline_profile_sha256"] = request.profile_sha256
            report["baseline_native_inputs_sha256"] = request.native_inputs_sha256
            if request.helper_sha256 is not None:
                if _digest(request.helper_sha256):
                    report["baseline_helper_sha256"] = request.helper_sha256
                else:
                    gaps.append("baseline_helper_identity_invalid")
    rows, projection_gaps = _project(changes, declarations)
    cargo_report, cargo_gaps = await _cargo_report(request, commits, changes, rows, observation)
    report.update(cargo_report)
    gaps.extend(cargo_gaps)
    report["changes"] = rows
    report["gaps"] = sorted(set(gaps + projection_gaps))
    return report
