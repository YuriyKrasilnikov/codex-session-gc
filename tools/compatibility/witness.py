# Copyright (c) 2026

"""Run explicitly selected, source-bound compatibility witnesses in owned homes."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn
from xml.parsers import expat

from codex_session_gc.artifacts import ArtifactIdentity
from codex_session_gc.compatibility.record import ExecutionIdentity
from codex_session_gc.installation.process import (
    OwnedProcessError,
    ProcessLimits,
    StorageBudget,
    run_owned,
)
from tools import ci_native

_MAX_SPEC = 1024 * 1024
_MAX_FILE = 16 * 1024 * 1024
_MAX_SOURCE = 128 * 1024 * 1024
_MAX_FILES = 512
_MAX_CASES = 4096
_MAX_ARGS = 128
_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_XML = 1024 * 1024
_MAX_SECONDS = 21600
_MAX_STORAGE = 2**63 - 1
_MAX_TEXT = 2048
_MAX_XML_DEPTH = 32
_MAX_COUNT_DIGITS = 6
_CONTROL = 32
_NATIVE_STATUSES = frozenset({"confirmed", "refused", "unknown"})


class WitnessError(ValueError):
    """A selected witness cannot produce complete bound evidence."""


class ReportFormat(StrEnum):
    """An explicitly selected witness result representation."""

    NATIVE_JSONL = "native_jsonl"
    NATIVE_OUTCOME_JSONL = "native_outcome_jsonl"
    PYTEST_JUNIT = "pytest_junit"


def _fail(message: str) -> NoReturn:
    raise WitnessError(message)


def _digest(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        _fail("Invalid SHA-256 identity.")
    return value


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_TEXT
        or any(ord(character) < _CONTROL for character in value)
    ):
        _fail("Invalid witness text.")
    return value


def _relative(value: object) -> str:
    text = _text(value)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != text
        or any(part in (".", "..") for part in path.parts)
        or "\\" in text
        or "://" in text
    ):
        _fail("Invalid source path.")
    return text


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("Duplicate JSON field.")
        result[key] = value
    return result


def _fields(value: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        _fail("Unexpected witness fields.")
    return value


def _constant(_value: str) -> NoReturn:
    _fail("Non-JSON constants are forbidden.")


def _json(raw: bytes, limit: int) -> object:
    if not raw or len(raw) > limit:
        _fail("JSON input exceeds its bound.")
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        message = "Invalid witness JSON."
        raise WitnessError(message) from error


def _artifact(value: object) -> ArtifactIdentity:
    data = _fields(value, {"sha256", "size"})
    size = data["size"]
    if type(size) is not int:
        _fail("Invalid source byte count.")
    try:
        return ArtifactIdentity(_digest(data["sha256"]), size)
    except ValueError as error:
        message = "Invalid artifact identity."
        raise WitnessError(message) from error


@dataclass(frozen=True, slots=True)
class SourceFile:
    """A reviewed root-relative file and exact bytes."""

    path: str
    identity: ArtifactIdentity

    def __post_init__(self) -> None:
        """Validate the file identity and path."""
        _relative(self.path)
        if not isinstance(self.identity, ArtifactIdentity) or self.identity.size > _MAX_FILE:
            _fail("Invalid source file identity.")


@dataclass(frozen=True, slots=True)
class ExpectedCase:
    """An explicitly expected case and observed status."""

    name: str
    status: str

    def __post_init__(self) -> None:
        """Reject missing or skipped cases."""
        _text(self.name)
        _text(self.status)
        if self.status not in _NATIVE_STATUSES | {"passed"}:
            _fail("Witness case status is not a supported terminal outcome.")


@dataclass(frozen=True, slots=True)
class WitnessSpec:
    """A bounded declaration of one selected witness; review is a claim."""

    name: str
    scope: str
    review_source: str
    script: str
    sources: tuple[SourceFile, ...]
    source_envelope_sha256: str
    execution: ExecutionIdentity
    fixture: ArtifactIdentity | None
    arguments: tuple[str, ...]
    native_cases: str | None
    report_format: ReportFormat
    expected_cases: tuple[ExpectedCase, ...]
    seconds: int
    output_bytes: int
    scratch_bytes: int
    free_floor_bytes: int

    def __post_init__(self) -> None:
        """Validate scope, resources and every declared input."""
        _text(self.name)
        _text(self.scope)
        _text(self.review_source)
        _relative(self.script)
        _digest(self.source_envelope_sha256)
        if (
            not isinstance(self.execution, ExecutionIdentity)
            or (self.fixture is not None and not isinstance(self.fixture, ArtifactIdentity))
            or not isinstance(self.report_format, ReportFormat)
            or type(self.seconds) is not int
            or not 0 < self.seconds <= _MAX_SECONDS
            or type(self.output_bytes) is not int
            or not 0 < self.output_bytes <= _MAX_OUTPUT
            or type(self.scratch_bytes) is not int
            or not 0 < self.scratch_bytes <= _MAX_STORAGE
            or type(self.free_floor_bytes) is not int
            or not 0 <= self.free_floor_bytes <= _MAX_STORAGE
        ):
            _fail("Invalid witness execution limits or identity.")
        if (
            not isinstance(self.sources, tuple)
            or not self.sources
            or len(self.sources) > _MAX_FILES
            or any(not isinstance(item, SourceFile) for item in self.sources)
            or len({item.path for item in self.sources}) != len(self.sources)
            or self.script not in {item.path for item in self.sources}
            or sum(item.identity.size for item in self.sources) > _MAX_SOURCE
        ):
            _fail("Invalid source envelope.")
        if source_envelope_sha256(self.sources) != self.source_envelope_sha256:
            _fail("Source envelope digest differs.")
        if (
            not isinstance(self.expected_cases, tuple)
            or not self.expected_cases
            or len(self.expected_cases) > _MAX_CASES
            or any(not isinstance(item, ExpectedCase) for item in self.expected_cases)
            or len({item.name for item in self.expected_cases}) != len(self.expected_cases)
        ):
            _fail("Invalid expected case set.")
        _check_case_format(self.report_format, self.expected_cases)
        if not isinstance(self.arguments, tuple) or len(self.arguments) > _MAX_ARGS:
            _fail("Invalid witness arguments.")
        for argument in self.arguments:
            _argument(argument)
        report_count = sum(argument.count("{report}") for argument in self.arguments)
        if (self.report_format is ReportFormat.PYTEST_JUNIT and report_count != 1) or (
            self.report_format is not ReportFormat.PYTEST_JUNIT and report_count != 0
        ):
            _fail("Report placeholder does not match selected format.")
        if self.native_cases is not None:
            text = _text(self.native_cases)
            if re.fullmatch(r"[A-Za-z0-9_,.-]+", text) is None:
                _fail("Invalid native case selection.")


def _check_case_format(report_format: ReportFormat, cases: tuple[ExpectedCase, ...]) -> None:
    allowed = {"passed"} if report_format is ReportFormat.PYTEST_JUNIT else _NATIVE_STATUSES
    if any(item.status not in allowed for item in cases):
        _fail("Expected case status does not match the selected format.")


def _argument(value: object) -> str:
    text = _text(value)
    option = text.split("=", 1)[0].casefold()
    if option in {"--home", "--codex-home", "--home-dir", "--user-home"}:
        _fail("Witness argument cannot select a home.")
    if "xfail_strict" in text.casefold():
        _fail("Witness argument cannot weaken pytest expected-failure handling.")
    candidate = text.split("=", 1)[-1]
    cleaned = (
        candidate.replace("{source}", "SOURCE")
        .replace("{tmp}", "TMP")
        .replace("{report}", "REPORT")
    )
    if (
        "{" in cleaned
        or "}" in cleaned
        or "\\" in cleaned
        or "://" in cleaned
        or cleaned.startswith(("/", "~"))
        or ".." in PurePosixPath(cleaned).parts
    ):
        _fail("Witness argument escapes its declared inputs.")
    return text


def source_envelope_sha256(sources: tuple[SourceFile, ...]) -> str:
    """Hash the explicitly declared file envelope, without claiming completeness."""
    value = [
        {"path": item.path, "sha256": item.identity.sha256, "size": item.identity.size}
        for item in sorted(sources, key=lambda item: item.path)
    ]
    return hashlib.sha256(
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ).hexdigest()


def parse_spec(raw: bytes) -> WitnessSpec:
    """Parse one strict witness declaration without verifying its review claim."""
    value = _fields(
        _json(raw, _MAX_SPEC),
        {
            "format",
            "name",
            "scope",
            "review_source",
            "script",
            "sources",
            "source_envelope_sha256",
            "execution",
            "fixture",
            "arguments",
            "native_cases",
            "report_format",
            "expected_cases",
            "seconds",
            "output_bytes",
            "scratch_bytes",
            "free_floor_bytes",
        },
    )
    if value["format"] != "codex-session-gc-witness-v1":
        _fail("Unsupported witness format.")
    source_items = value["sources"]
    case_items = value["expected_cases"]
    arguments = value["arguments"]
    if (
        not isinstance(source_items, list)
        or not isinstance(case_items, list)
        or not isinstance(arguments, list)
    ):
        _fail("Witness collections must be arrays.")
    sources = []
    for item in source_items:
        data = _fields(item, {"path", "sha256", "size"})
        sources.append(
            SourceFile(
                _relative(data["path"]), _artifact({"sha256": data["sha256"], "size": data["size"]})
            )
        )
    cases = []
    for item in case_items:
        data = _fields(item, {"name", "status"})
        cases.append(ExpectedCase(_text(data["name"]), _text(data["status"])))
    execution = _fields(
        value["execution"], {"helper_sha256", "profile_sha256", "native_inputs_sha256"}
    )
    raw_format = value["report_format"]
    if not isinstance(raw_format, str):
        _fail("Unsupported witness report format.")
    try:
        output_format = ReportFormat(raw_format)
    except (ValueError, TypeError) as error:
        message = "Unsupported witness report format."
        raise WitnessError(message) from error
    seconds, output_bytes = value["seconds"], value["output_bytes"]
    scratch, free_floor = value["scratch_bytes"], value["free_floor_bytes"]
    if (
        type(seconds) is not int
        or type(output_bytes) is not int
        or type(scratch) is not int
        or type(free_floor) is not int
    ):
        _fail("Invalid witness execution limits.")
    native_cases = value["native_cases"]
    return WitnessSpec(
        _text(value["name"]),
        _text(value["scope"]),
        _text(value["review_source"]),
        _relative(value["script"]),
        tuple(sources),
        _digest(value["source_envelope_sha256"]),
        ExecutionIdentity(
            _digest(execution["helper_sha256"]),
            _digest(execution["profile_sha256"]),
            _digest(execution["native_inputs_sha256"]),
        ),
        None if value["fixture"] is None else _artifact(value["fixture"]),
        tuple(_argument(item) for item in arguments),
        None if native_cases is None else _text(native_cases),
        output_format,
        tuple(cases),
        seconds,
        output_bytes,
        scratch,
        free_floor,
    )


def witness_spec_sha256(spec: WitnessSpec) -> str:
    """Hash the canonical validated witness declaration."""
    encoded = json.dumps(
        {"format": "codex-session-gc-witness-v1", "spec": asdict(spec)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class WitnessInputs:
    """Explicit qualified helper and optional fixture artifact paths."""

    helper: Path
    helper_identity: ArtifactIdentity
    fixture: Path | None = None


@dataclass(frozen=True, slots=True)
class WitnessReceipt:
    """Observed cases and exact input bindings, without a support decision."""

    name: str
    scope: str
    review_source: str
    spec_sha256: str
    source_envelope_sha256: str
    sources: tuple[SourceFile, ...]
    execution: ExecutionIdentity
    helper: ArtifactIdentity
    fixture: ArtifactIdentity | None
    report_format: ReportFormat
    report_sha256: str
    cases: tuple[ExpectedCase, ...]

    @property
    def claim_verified(self) -> Literal[False]:
        """The supplied review and source envelope are not authenticated here."""
        return False

    @property
    def source_coverage_verified(self) -> Literal[False]:
        """Declared source files are not proof of complete dependency coverage."""
        return False

    @property
    def authorizes_operation(self) -> Literal[False]:
        """A witness receipt does not authorize cleanup or support acceptance."""
        return False


def _file(root: Path, name: str) -> Path:
    relative = _relative(name)
    path = root / relative
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            _fail("Source envelope contains a symbolic link.")
    if not path.is_file():
        _fail("Source envelope file is unavailable.")
    return path


def _hash_file(path: Path, identity: ArtifactIdentity, *, maximum: int | None = None) -> None:
    if path.is_symlink() or not path.is_file() or (maximum is not None and identity.size > maximum):
        _fail("Input file is unavailable or exceeds its bound.")
    before = path.stat()
    if before.st_size != identity.size:
        _fail("Input size differs.")
    hashed = hashlib.sha256()
    remaining = identity.size
    with path.open("rb") as source:
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                _fail("Input bytes are truncated.")
            hashed.update(chunk)
            remaining -= len(chunk)
        if source.read(1):
            _fail("Input bytes exceed their declared size.")
    after = path.stat()
    if hashed.hexdigest() != identity.sha256 or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        _fail("Input bytes changed or differ.")


def _native_preflight(root: Path, execution: ExecutionIdentity) -> None:
    profile = _file(root, ci_native.PROFILE.as_posix())
    provenance = _file(root, ci_native.PROVENANCE.as_posix())
    if profile.stat().st_size > _MAX_FILE or provenance.stat().st_size > _MAX_SPEC:
        _fail("Qualification metadata exceeds its bound.")
    with provenance.open("rb") as stream:
        data = _json(stream.read(_MAX_SPEC + 1), _MAX_SPEC)
    if not isinstance(data, dict) or not isinstance(data.get("current_delivery_inputs"), dict):
        _fail("Native input manifest is unavailable.")
    current = data["current_delivery_inputs"]
    manifest = current.get("manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("inputs"), list):
        _fail("Native input manifest is invalid.")
    _native_entries(root, manifest["inputs"])
    try:
        ci_native.verify_material_source(
            root,
            helper_sha256=execution.helper_sha256,
            profile_sha256=execution.profile_sha256,
            native_inputs_sha256=execution.native_inputs_sha256,
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        message = "Execution source does not match the witness."
        raise WitnessError(message) from error


def _native_entries(root: Path, entries: list[object]) -> None:
    if len(entries) > _MAX_FILES:
        _fail("Native input manifest exceeds its bound.")
    total = 0
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            _fail("Native input entry is invalid.")
        size = item["bytes"]
        if type(size) is not int or size < 0 or size > _MAX_FILE:
            _fail("Native input exceeds its bound.")
        total += size
        if total > _MAX_SOURCE:
            _fail("Native input envelope exceeds its bound.")
        path = _file(root, _relative(item["path"]))
        if path.stat().st_size != size:
            _fail("Native input size differs.")


def _inputs(root: Path, spec: WitnessSpec, inputs: WitnessInputs) -> None:
    if (
        not isinstance(inputs.helper_identity, ArtifactIdentity)
        or inputs.helper_identity.sha256 != spec.execution.helper_sha256
    ):
        _fail("Helper identity differs from the witness.")
    _hash_file(inputs.helper, inputs.helper_identity)
    if (spec.fixture is None) != (inputs.fixture is None):
        _fail("Fixture artifact identity is incomplete.")
    if spec.fixture is not None and inputs.fixture is not None:
        _hash_file(inputs.fixture, spec.fixture)
    for source in spec.sources:
        _hash_file(_file(root, source.path), source.identity, maximum=_MAX_FILE)
    _native_preflight(root, spec.execution)


def _native_cases(raw: bytes, spec: WitnessSpec) -> tuple[ExpectedCase, ...]:
    if not raw.endswith(b"\n"):
        _fail("Witness output is incomplete.")
    lines = raw.splitlines()
    if len(lines) != len(spec.expected_cases):
        _fail("Witness case count differs.")
    actual = []
    for line in lines:
        value = _json(line, _MAX_SPEC)
        if not isinstance(value, dict):
            _fail("Native witness case is invalid.")
        status: object = value.get("status")
        if spec.report_format is ReportFormat.NATIVE_OUTCOME_JSONL:
            outcome = value.get("outcome")
            if not isinstance(outcome, dict):
                _fail("Native witness outcome is invalid.")
            status = outcome.get("status")
        actual.append(ExpectedCase(_text(value.get("case")), _text(status)))
    result = tuple(actual)
    if result != spec.expected_cases:
        _fail("Witness cases or statuses differ.")
    return result


class _JUnit:
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.actual: list[ExpectedCase] = []
        self.suite_expected: int | None = None
        self.suite_start = 0
        self.suite_count = 0
        self.event_count = 0

    def start(self, name: str, attributes: dict[str, str]) -> None:
        self.event_count += 1
        if self.event_count > _MAX_CASES * 8 or len(self.stack) > _MAX_XML_DEPTH:
            _fail("JUnit structure exceeds its bound.")
        parent = self.stack[-1] if self.stack else None
        if name == "testsuites":
            if parent is not None:
                _fail("JUnit suite root is invalid.")
        elif name == "testsuite":
            self.start_suite(parent, attributes)
        elif name == "testcase":
            self.start_case(parent, attributes)
        elif name in {"failure", "error", "skipped"}:
            _fail("JUnit case did not pass.")
        elif name not in {"properties", "property", "system-out", "system-err"}:
            _fail("JUnit element is unsupported.")
        self.stack.append(name)

    def start_suite(self, parent: str | None, attributes: dict[str, str]) -> None:
        if parent not in (None, "testsuites") or self.suite_expected is not None:
            _fail("JUnit suite nesting is invalid.")
        if any(attributes.get(key) != "0" for key in ("errors", "failures", "skipped")):
            _fail("JUnit suite contains failures or skips.")
        count = attributes.get("tests", "")
        if not count.isascii() or not count.isdecimal() or len(count) > _MAX_COUNT_DIGITS:
            _fail("JUnit suite count is invalid.")
        self.suite_expected = int(count)
        self.suite_start = len(self.actual)
        self.suite_count += 1

    def start_case(self, parent: str | None, attributes: dict[str, str]) -> None:
        if parent != "testsuite" or self.suite_expected is None:
            _fail("JUnit case is outside a suite.")
        name = f"{_text(attributes.get('classname'))}::{_text(attributes.get('name'))}"
        self.actual.append(ExpectedCase(name, "passed"))
        if len(self.actual) > _MAX_CASES:
            _fail("JUnit case count exceeds its bound.")

    def end(self, name: str) -> None:
        if not self.stack or self.stack.pop() != name:
            _fail("JUnit structure is invalid.")
        if name == "testsuite":
            if (
                self.suite_expected is None
                or len(self.actual) - self.suite_start != self.suite_expected
            ):
                _fail("JUnit suite is incomplete.")
            self.suite_expected = None


def _forbidden_xml(*_args: object) -> NoReturn:
    _fail("JUnit declarations and entities are forbidden.")


def _junit(raw: bytes, spec: WitnessSpec) -> tuple[ExpectedCase, ...]:
    if not raw or len(raw) > min(spec.output_bytes, _MAX_XML):
        _fail("JUnit report exceeds its bound.")
    state = _JUnit()
    parser = expat.ParserCreate()
    parser.StartElementHandler = state.start
    parser.EndElementHandler = state.end
    parser.StartDoctypeDeclHandler = _forbidden_xml
    parser.EntityDeclHandler = _forbidden_xml
    parser.ExternalEntityRefHandler = _forbidden_xml
    try:
        complete = True
        parser.Parse(raw, complete)
    except expat.ExpatError as error:
        message = "JUnit report is malformed."
        raise WitnessError(message) from error
    result = tuple(state.actual)
    if not state.suite_count or state.stack or result != spec.expected_cases:
        _fail("JUnit cases differ.")
    return result


def normalize_report(spec: WitnessSpec, raw: bytes) -> tuple[ExpectedCase, ...]:
    """Check a bounded report's declared cases without authenticating its origin."""
    if not isinstance(spec, WitnessSpec) or not isinstance(raw, bytes):
        _fail("Witness report input is invalid.")
    if len(raw) > spec.output_bytes:
        _fail("Witness report exceeds its bound.")
    if spec.report_format is ReportFormat.PYTEST_JUNIT:
        return _junit(raw, spec)
    return _native_cases(raw, spec)


async def _run_witness(
    spec: WitnessSpec, root: Path, inputs: WitnessInputs, trusted_python: Path
) -> WitnessReceipt:
    if (
        not isinstance(spec, WitnessSpec)
        or not trusted_python.is_absolute()
        or not root.is_absolute()
    ):
        _fail("Witness, source root or trusted Python is invalid.")
    deadline = time.monotonic() + spec.seconds
    _inputs(root, spec, inputs)
    if time.monotonic() >= deadline:
        _fail("Witness deadline exceeded during input observation.")
    with tempfile.TemporaryDirectory(prefix="codex-gc-witness-") as temporary:
        owned = Path(temporary)
        home = owned / "home"
        home.mkdir()
        report_path = owned / "report.xml"
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "CODEX_HOME": str(home),
            "TMPDIR": str(owned),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONPATH": str(root / "src"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTEST_ADDOPTS": "-o xfail_strict=true",
            "GC_NATIVE_BINARY": str(inputs.helper.resolve()),
            "GC_MCP_NATIVE_HELPER": str(inputs.helper.resolve()),
            "GC_NATIVE_FIXTURE": str(inputs.fixture.resolve())
            if inputs.fixture is not None
            else str(owned / "absent-fixture"),
        }
        if spec.native_cases is not None:
            env["GC_NATIVE_CASES"] = spec.native_cases
        args = [
            argument.replace("{source}", str(root))
            .replace("{tmp}", str(owned))
            .replace("{report}", str(report_path))
            for argument in spec.arguments
        ]
        try:
            storage = StorageBudget.capture(owned, spec.scratch_bytes, spec.free_floor_bytes)
            output = await run_owned(
                [str(trusted_python), "-P", str(_file(root, spec.script)), *args],
                cwd=owned,
                environment=env,
                limits=ProcessLimits(
                    seconds=max(0.0, deadline - time.monotonic()),
                    output_bytes=spec.output_bytes,
                    storage=storage,
                ),
            )
        except OwnedProcessError as error:
            message = f"Witness process refused: {error.reason.value}"
            raise WitnessError(message) from error
        if spec.report_format is ReportFormat.PYTEST_JUNIT:
            if report_path.is_symlink() or not report_path.is_file():
                _fail("JUnit report is absent.")
            with report_path.open("rb") as stream:
                report = stream.read(min(spec.output_bytes, _MAX_XML) + 1)
        else:
            report = output
        cases = normalize_report(spec, report)
        _inputs(root, spec, inputs)
        if time.monotonic() >= deadline:
            _fail("Witness deadline exceeded during input observation.")
        return WitnessReceipt(
            spec.name,
            spec.scope,
            spec.review_source,
            witness_spec_sha256(spec),
            spec.source_envelope_sha256,
            spec.sources,
            spec.execution,
            inputs.helper_identity,
            spec.fixture,
            spec.report_format,
            hashlib.sha256(report).hexdigest(),
            cases,
        )


async def run_witness(
    spec: WitnessSpec, root: Path, inputs: WitnessInputs, trusted_python: Path
) -> WitnessReceipt:
    """Run one explicitly selected witness in an isolated temporary home."""
    try:
        return await _run_witness(spec, root, inputs, trusted_python)
    except WitnessError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        message = "Witness inputs or result could not be observed."
        raise WitnessError(message) from error
