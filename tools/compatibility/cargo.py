# Copyright (c) 2026

"""Conservative Cargo source envelopes over bounded immutable Git objects."""

from __future__ import annotations

import asyncio
import datetime
import fnmatch
import hashlib
import json
import posixpath
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from tools.compatibility import git

if TYPE_CHECKING:
    from pathlib import Path

_CONTROL = 32
_MAX_TARGET = 512
_MAX_VERSION = 128
_MAX_NODES = 4096
_MAX_EDGES = 8192
_MAX_PATTERNS = 256
_MAX_FEATURES = 4096
_MAX_FEATURE_VALUES = 256
_MAX_TOTAL_FEATURE_VALUES = 16384
_MAX_NAME = 256
_MAX_READS = 512
_MAX_BYTES = 32 * 1024 * 1024
_TIMEOUT_SECONDS = 120.0
_MAX_REQUEST_SECONDS = 3600
_INCLUDE = re.compile(r"\binclude(?:_str|_bytes)?!\s*\((.*?)\)", re.DOTALL)
_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')
WORKSPACE_MEMBERSHIP_UNRESOLVED = "workspace_membership_unresolved"


@dataclass(frozen=True, slots=True)
class CargoLimits:
    """Finite resource bounds for one source-envelope derivation."""

    nodes: int = _MAX_NODES
    edges: int = _MAX_EDGES
    reads: int = _MAX_READS
    bytes: int = _MAX_BYTES
    seconds: float = _TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Reject invalid or excessive requested bounds."""
        if (
            any(
                type(value) is not int or not 0 < value <= maximum
                for value, maximum in (
                    (self.nodes, 100_000),
                    (self.edges, 200_000),
                    (self.reads, 100_000),
                    (self.bytes, 1_073_741_824),
                )
            )
            or type(self.seconds) not in (int, float)
            or not 0 < self.seconds <= _MAX_REQUEST_SECONDS
        ):
            message = "Invalid Cargo derivation limits."
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class CargoInputs:
    """Workspace roots and explicit derivation bounds."""

    workspace: str
    roots: tuple[str, ...]
    limits: CargoLimits
    observation: git.ObservationContext | None = None


@dataclass(frozen=True, slots=True)
class CargoNode:
    """A source object retained in the conservative envelope."""

    path: str
    object_id: str
    role: str


@dataclass(frozen=True, slots=True)
class CargoEdge:
    """A conservatively traversed local or unresolved dependency."""

    source: str
    destination: str | None
    dependency: str
    kind: str
    target: str | None
    optional: bool
    workspace_inherited: bool
    declared_features: tuple[str, ...]
    workspace_features: tuple[str, ...]
    default_features: bool | None
    workspace_default_features: bool | None


@dataclass(frozen=True, slots=True)
class CargoFeature:
    """A feature declaration without claiming its activation."""

    manifest: str
    name: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CargoManifestFact:
    """Parsed package-version evidence linked to a source object."""

    path: str
    object_id: str
    version: str
    without_version_sha256: str


@dataclass(frozen=True, slots=True)
class CargoEnvelope:
    """Object-bound source evidence with explicit coverage gaps."""

    commit: str
    nodes: tuple[CargoNode, ...]
    edges: tuple[CargoEdge, ...]
    features: tuple[CargoFeature, ...]
    manifests: tuple[CargoManifestFact, ...]
    workspace_member_candidates: tuple[str, ...]
    gaps: tuple[str, ...]


class _Derivation:
    def __init__(
        self,
        commit: str,
        entries: tuple[git.TreeEntry, ...],
        limits: CargoLimits,
        observation: git.ObservationContext,
    ) -> None:
        self.limits = limits
        self.observation = observation
        self.commit = commit
        self.entries = {entry.path: entry for entry in entries}
        self.nodes: dict[str, CargoNode] = {}
        self.edges: list[CargoEdge] = []
        self.features: list[CargoFeature] = []
        self.feature_values = 0
        self.manifests: list[CargoManifestFact] = []
        self.gaps: set[str] = {
            "macro_codegen_and_caller_coverage_unresolved",
            "feature_activation_not_evaluated",
        }
        self.members: set[str] = set()
        self.visited: set[str] = set()
        self.scanned: set[str] = set()
        self.cache: dict[str, bytes] = {}
        self.reads = 0
        self.bytes = 0
        self.workspace = ""
        self.workspace_deps: dict[str, object] = {}
        self.pending_crates: list[str] = []
        self.pending_includes: list[str] = []

    def gap(self, code: str) -> None:
        self.gaps.add(code)

    def add(self, path: str, role: str) -> bool:
        if self.alias_parent(path):
            self.gap("symlink_or_submodule_source")
            return False
        entry = self.entries.get(path)
        if entry is None:
            self.gap("referenced_source_missing")
            return False
        if entry.kind != "blob" or entry.mode == "120000":
            self.gap("symlink_or_submodule_source")
            return False
        if path not in self.nodes and len(self.nodes) >= self.limits.nodes:
            self.gap("source_node_limit")
            return False
        if path not in self.nodes:
            self.nodes[path] = CargoNode(path, entry.object_id, role)
        return True

    def alias_parent(self, path: str) -> bool:
        return any(
            (entry := self.entries.get(str(parent))) is not None
            and (entry.mode == "120000" or entry.kind == "commit")
            for parent in PurePosixPath(path).parents
            if str(parent) != "."
        )

    async def read(self, path: str) -> bytes | None:
        if path in self.cache:
            return self.cache[path]
        if self.reads >= self.limits.reads or self.bytes >= self.limits.bytes:
            self.gap("source_read_budget")
            return None
        entry = self.entries[path]
        if entry.size is None:
            self.gap("source_blob_unavailable_or_over_limit")
            return None
        self.reads += 1
        try:
            (raw,) = await self.observation.read(((entry.object_id, entry.size),))
        except git.SourceError:
            self.gap("source_blob_unavailable_or_over_limit")
            return None
        self.bytes += len(raw)
        if self.bytes > self.limits.bytes:
            self.gap("source_read_budget")
            return None
        self.cache[path] = raw
        return raw

    async def toml(self, path: str, role: str) -> dict[str, object] | None:
        if not self.add(path, role):
            return None
        raw = await self.read(path)
        if raw is None:
            return None
        try:
            return tomllib.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, tomllib.TOMLDecodeError:
            self.gap("malformed_toml")
            return None

    def relative(self, base: str, value: object) -> str | None:
        if (
            not isinstance(value, str)
            or not value
            or "\\" in value
            or "://" in value
            or value.startswith("/")
            or any(ord(c) < _CONTROL for c in value)
        ):
            self.gap("invalid_or_escaped_source_path")
            return None
        result = posixpath.normpath(posixpath.join(base, value))
        if result == ".." or result.startswith("../"):
            self.gap("invalid_or_escaped_source_path")
            return None
        return "" if result == "." else result

    async def workspace_metadata(self, manifest: str) -> None:
        self.workspace = str(PurePosixPath(manifest).parent)
        if self.workspace == ".":
            self.workspace = ""
        data = await self.toml(manifest, "workspace_manifest")
        if data is None:
            return
        workspace = data.get("workspace")
        if not isinstance(workspace, dict):
            self.gap("workspace_table_missing")
            return
        dependencies = workspace.get("dependencies", {})
        if isinstance(dependencies, dict):
            self.workspace_deps = dependencies
        else:
            self.gap("workspace_dependencies_invalid")
        self.workspace_members(workspace.get("members", []))
        if "exclude" in workspace or "default-members" in workspace:
            self.gap("workspace_selection_unresolved")
        patches = data.get("patch", {})
        if isinstance(patches, dict):
            for table in patches.values():
                await self.dependencies(manifest, self.workspace, table, "patch", None)
        else:
            self.gap("cargo_patch_invalid")
        if "replace" in data:
            self.gap("cargo_replace_unresolved")
        await self.workspace_controls()

    def workspace_members(self, patterns: object) -> None:
        if not isinstance(patterns, list):
            self.gap("workspace_members_invalid")
            return
        prefix = self.workspace + "/" if self.workspace else ""
        candidates = self.workspace_candidates(prefix)
        if len(patterns) > _MAX_PATTERNS:
            self.gap("workspace_pattern_limit")
            patterns = patterns[:_MAX_PATTERNS]
        for pattern in patterns:
            if (
                not isinstance(pattern, str)
                or len(pattern) > _MAX_TARGET
                or "://" in pattern
                or pattern.startswith("/")
                or ".." in PurePosixPath(pattern).parts
            ):
                self.gap("workspace_member_pattern_invalid")
                continue
            matched = [name for name in candidates if fnmatch.fnmatchcase(name, pattern)]
            if not matched:
                self.gap("workspace_member_pattern_unresolved")
            for name in matched:
                if len(self.members) >= self.limits.nodes:
                    self.gap("workspace_member_limit")
                    return
                self.members.add(prefix + name + "/Cargo.toml")

    def workspace_candidates(self, prefix: str) -> list[str]:
        candidates: list[str] = []
        for path in self.entries:
            if path.startswith(prefix) and path.endswith("/Cargo.toml"):
                if len(candidates) >= self.limits.nodes:
                    self.gap("workspace_member_limit")
                    break
                candidates.append(path[len(prefix) :].removesuffix("/Cargo.toml"))
        return candidates

    async def workspace_controls(self) -> None:
        prefix = self.workspace + "/" if self.workspace else ""
        await self.lock_file(prefix + "Cargo.lock")
        await self.toolchains()
        for name in (".cargo/config", ".cargo/config.toml"):
            path = prefix + name
            if path in self.entries:
                await self.toml(path, "cargo_configuration")
                self.gap("cargo_configuration_effects_unresolved")

    async def lock_file(self, lock: str) -> None:
        if lock in self.entries:
            lock_data = await self.toml(lock, "cargo_lock")
            if lock_data is not None:
                packages = lock_data.get("package", [])
                if not isinstance(packages, list):
                    self.gap("cargo_lock_packages_invalid")
                elif any(isinstance(package, dict) and "source" in package for package in packages):
                    self.gap("locked_external_source_unresolved")
        else:
            self.gap("cargo_lock_missing")

    async def toolchains(self) -> None:
        toolchains = ("rust-toolchain.toml", "rust-toolchain")
        found = False
        for parent in (self.workspace, *[str(p) for p in PurePosixPath(self.workspace).parents]):
            directory = "" if parent == "." else parent
            for name in toolchains:
                path = posixpath.join(directory, name)
                if path in self.entries:
                    found = True
                    if name.endswith(".toml"):
                        await self.toml(path, "toolchain")
                    else:
                        self.add(path, "toolchain")
        if not found:
            self.gap("toolchain_not_declared")

    async def dependencies(
        self, source: str, base: str, table: object, kind: str, target: str | None
    ) -> None:
        if not isinstance(table, dict):
            self.gap("dependency_table_invalid")
            return
        for name, declaration in table.items():
            if len(self.edges) >= self.limits.edges:
                self.gap("dependency_edge_limit")
                return
            if (
                not isinstance(name, str)
                or len(name) > _MAX_NAME
                or re.fullmatch(r"[A-Za-z0-9_.-]+", name) is None
            ):
                self.gap("dependency_name_invalid")
                continue
            self.validate_dependency_flags(declaration)
            inherited = isinstance(declaration, dict) and declaration.get("workspace") is True
            if (
                inherited
                and source not in self.members
                and source != posixpath.join(self.workspace, "Cargo.toml")
            ):
                self.gap(WORKSPACE_MEMBERSHIP_UNRESOLVED)
            optional = isinstance(declaration, dict) and declaration.get("optional") is True
            actual = self.workspace_deps.get(name) if inherited else declaration
            if inherited and actual is None:
                self.gap("workspace_dependency_unresolved")
            if not isinstance(actual, (dict, str)):
                self.gap("dependency_declaration_invalid")
                continue
            declared_features, default_features = self.dependency_options(declaration)
            workspace_features, workspace_default = (
                self.dependency_options(actual) if inherited else ((), None)
            )
            dep_path = actual.get("path") if isinstance(actual, dict) else None
            if dep_path is None:
                self.edges.append(
                    CargoEdge(
                        source,
                        None,
                        name,
                        kind,
                        target,
                        optional,
                        inherited,
                        declared_features,
                        workspace_features,
                        default_features,
                        workspace_default,
                    )
                )
                self.gap("external_dependency_source_unresolved")
                continue
            location = self.relative(self.workspace if inherited else base, dep_path)
            if location is None:
                continue
            manifest = posixpath.join(location, "Cargo.toml")
            self.edges.append(
                CargoEdge(
                    source,
                    manifest,
                    name,
                    kind,
                    target,
                    optional,
                    inherited,
                    declared_features,
                    workspace_features,
                    default_features,
                    workspace_default,
                )
            )
            self.pending_crates.append(manifest)

    def validate_dependency_flags(self, declaration: object) -> None:
        if isinstance(declaration, dict) and any(
            key in declaration and type(declaration[key]) is not bool
            for key in ("workspace", "optional", "default-features")
        ):
            self.gap("dependency_flags_invalid")

    def dependency_options(self, declaration: object) -> tuple[tuple[str, ...], bool | None]:
        if not isinstance(declaration, dict):
            return (), None
        values = declaration.get("features", [])
        if (
            not isinstance(values, list)
            or len(values) > _MAX_FEATURE_VALUES
            or any(
                not isinstance(value, str)
                or not value
                or len(value) > _MAX_TARGET
                or "://" in value
                or any(ord(character) < _CONTROL for character in value)
                for value in values
            )
        ):
            self.gap("dependency_features_invalid_or_over_limit")
            return (), None
        if self.feature_values + len(values) > _MAX_TOTAL_FEATURE_VALUES:
            self.gap("dependency_feature_reference_limit")
            return (), None
        self.feature_values += len(values)
        default = declaration.get("default-features")
        if default is not None and type(default) is not bool:
            self.gap("dependency_flags_invalid")
            return tuple(values), None
        return tuple(values), default

    async def crate(self, manifest: str) -> None:
        if manifest in self.visited:
            return
        if len(self.visited) >= self.limits.reads:
            self.gap("crate_limit")
            return
        self.visited.add(manifest)
        data = await self.toml(manifest, "crate_manifest")
        if data is None:
            return
        base = str(PurePosixPath(manifest).parent)
        if base == ".":
            base = ""
        package = data.get("package")
        if not isinstance(package, dict):
            self.gap("crate_package_missing")
        self.manifest_facts(manifest, data)
        self.feature_facts(manifest, data.get("features", {}))
        self.crate_content(base, package)
        await self.crate_dependencies(manifest, base, data)
        self.target_sources(base, data)

    def manifest_facts(self, manifest: str, data: dict[str, object]) -> None:
        package = data.get("package")
        if not isinstance(package, dict):
            return
        version = package.get("version")
        if (
            not isinstance(version, str)
            or len(version) > _MAX_VERSION
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", version) is None
        ):
            self.gap("package_version_not_literal")
            return
        remaining = dict(data)
        package_without_version = dict(package)
        del package_without_version["version"]
        remaining["package"] = package_without_version
        try:
            encoded = json.dumps(
                _canonical(remaining), separators=(",", ":"), ensure_ascii=True
            ).encode()
        except TypeError, ValueError, RecursionError:
            self.gap("package_metadata_normalization_unavailable")
            return
        self.manifests.append(
            CargoManifestFact(
                manifest,
                self.entries[manifest].object_id,
                version,
                hashlib.sha256(encoded).hexdigest(),
            )
        )

    def feature_facts(self, manifest: str, features: object) -> None:
        if not isinstance(features, dict):
            self.gap("feature_declarations_invalid")
            return
        for name, values in features.items():
            if len(self.features) >= _MAX_FEATURES:
                self.gap("feature_declaration_limit")
                return
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", name) is None
                or not isinstance(values, list)
                or len(values) > _MAX_FEATURE_VALUES
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value) > _MAX_TARGET
                    or "://" in value
                    or any(ord(character) < _CONTROL for character in value)
                    for value in values
                )
            ):
                self.gap("feature_declarations_invalid_or_over_limit")
                continue
            if self.feature_values + len(values) > _MAX_TOTAL_FEATURE_VALUES:
                self.gap("feature_reference_limit")
                return
            self.feature_values += len(values)
            self.features.append(CargoFeature(manifest, name, tuple(values)))

    def crate_content(self, base: str, package: object) -> None:
        prefix = base + "/" if base else ""
        for path in sorted(self.entries):
            if path.startswith(prefix):
                if len(self.nodes) >= self.limits.nodes and path not in self.nodes:
                    self.gap("source_node_limit")
                    break
                self.add(path, "crate_content")
        if posixpath.join(base, "build.rs") in self.entries or (
            isinstance(package, dict) and isinstance(package.get("build"), str)
        ):
            self.gap("build_script_effects_unresolved")
            if isinstance(package, dict) and isinstance(package.get("build"), str):
                build_path = self.relative(base, package["build"])
                if build_path is not None:
                    if not build_path.startswith(prefix):
                        self.gap("build_script_outside_crate")
                    self.add(build_path, "build_script")

    async def crate_dependencies(self, manifest: str, base: str, data: dict[str, object]) -> None:
        kinds = (
            ("dependencies", "normal"),
            ("build-dependencies", "build"),
            ("dev-dependencies", "dev"),
        )
        for key, kind in kinds:
            if key in data:
                await self.dependencies(manifest, base, data[key], kind, None)
        targets = data.get("target", {})
        if not isinstance(targets, dict):
            self.gap("target_dependencies_invalid")
            return
        for target, sections in targets.items():
            if (
                not isinstance(target, str)
                or not isinstance(sections, dict)
                or len(target) > _MAX_TARGET
                or "://" in target
                or any(ord(character) < _CONTROL for character in target)
            ):
                self.gap("target_dependencies_invalid")
                continue
            for key, kind in kinds:
                if key in sections:
                    await self.dependencies(manifest, base, sections[key], kind, target)

    def target_sources(self, base: str, data: dict[str, object]) -> None:
        prefix = base + "/" if base else ""
        for key in ("lib", "bin", "example", "test", "bench"):
            targets_value = data.get(key, [])
            items = targets_value if isinstance(targets_value, list) else [targets_value]
            for item in items:
                if isinstance(item, dict) and "path" in item:
                    path = self.relative(base, item["path"])
                    if path is not None and not path.startswith(prefix):
                        self.gap("target_path_outside_crate")
                        self.add(path, "target_source")

    async def includes(self, path: str) -> None:
        if path in self.scanned:
            return
        self.scanned.add(path)
        raw = await self.read(path)
        if raw is None:
            return
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.gap("rust_source_encoding_unsupported")
            return
        for match in _INCLUDE.finditer(source):
            expression = match.group(1).strip()
            if _LITERAL.fullmatch(expression) is None:
                self.gap("dynamic_include_unresolved")
                continue
            try:
                value: object = json.loads(expression)
            except ValueError:
                self.gap("include_literal_unresolved")
                continue
            base = str(PurePosixPath(path).parent)
            location = self.relative("" if base == "." else base, value)
            if (
                location is not None
                and self.add(location, "included_source")
                and location.endswith(".rs")
            ):
                self.pending_includes.append(location)

    async def derive(self, workspace: str, roots: tuple[str, ...]) -> CargoEnvelope:
        if (
            not _manifest_path(workspace)
            or not roots
            or any(not _manifest_path(root) for root in roots)
        ):
            self.gap("cargo_roots_invalid_or_missing")
            return self.result()
        await self.workspace_metadata(workspace)
        self.pending_crates.extend(roots)
        while self.pending_crates:
            manifest = self.pending_crates.pop()
            await self.crate(manifest)
            if len(self.visited) >= self.limits.reads and self.pending_crates:
                self.gap("crate_limit")
                break
        self.pending_includes.extend(path for path in self.nodes if path.endswith(".rs"))
        while self.pending_includes:
            await self.includes(self.pending_includes.pop())
            if len(self.scanned) >= self.limits.nodes and self.pending_includes:
                self.gap("include_limit")
                break
        return self.result()

    def result(self) -> CargoEnvelope:
        return CargoEnvelope(
            self.commit,
            tuple(self.nodes[path] for path in sorted(self.nodes)),
            tuple(self.edges),
            tuple(self.features),
            tuple(self.manifests),
            tuple(sorted(self.members)),
            tuple(sorted(self.gaps)),
        )


def _canonical(value: object) -> object:
    if isinstance(value, dict):
        return ["table", [[key, _canonical(item)] for key, item in sorted(value.items())]]
    if isinstance(value, list):
        return ["array", [_canonical(item) for item in value]]
    if isinstance(value, bool):
        scalar = ["bool", value]
    elif isinstance(value, int):
        scalar = ["integer", str(value)]
    elif isinstance(value, float):
        scalar = ["float", value.hex()]
    elif isinstance(value, str):
        scalar = ["string", value]
    elif isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        scalar = [type(value).__name__, value.isoformat()]
    else:
        message = "Unsupported TOML value."
        raise TypeError(message)
    return scalar


def _manifest_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and path.as_posix() == value
        and all(part not in (".", "..") for part in path.parts)
        and path.name == "Cargo.toml"
    )


async def derive(
    executable: Path,
    repository: Path,
    commit: str,
    inputs: CargoInputs,
) -> CargoEnvelope:
    """Derive a conservative envelope, retaining any unresolved mechanism as a gap."""
    derivation: _Derivation | None = None
    observation = inputs.observation or git.ObservationContext(
        executable, repository, cache_bytes=inputs.limits.bytes, seconds=inputs.limits.seconds
    )
    try:
        async with asyncio.timeout(inputs.limits.seconds):
            entries = await observation.tree(commit)
            derivation = _Derivation(commit, entries, inputs.limits, observation)
            return await derivation.derive(inputs.workspace, inputs.roots)
    except git.SourceError:
        return CargoEnvelope(
            commit, (), (), (), (), (), ("git_tree_unavailable_or_budget_exceeded",)
        )
    except TimeoutError:
        if derivation is None:
            return CargoEnvelope(commit, (), (), (), (), (), ("git_tree_timeout",))
        derivation.gap("derivation_timeout")
        return derivation.result()
