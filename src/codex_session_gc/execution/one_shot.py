# Copyright (c) 2026
# ruff: noqa: EM101, TRY003, S603, PLR0913, PLR0917

"""Journal one bounded stdio request to an owned helper, without replay.

The caller qualifies the command and native operation. This adapter certifies
neither a native profile nor deletion permission. Deadlines bound cooperative
pipe waits; journal/filesystem calls and process creation are not kernel budgets.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import subprocess
import time
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from codex_session_gc.execution.process_identity import (
    OWNED_MARKER_ENV,
    observe_owned_child,
    owned_launcher_command,
)
from codex_session_gc.recovery import (
    LaunchIntent,
    LaunchRegistration,
    Payload,
    TerminalKind,
    UnitId,
    fence_launch,
    record_dispatch_intent,
    record_launch_closure,
    record_launch_intent,
    record_terminal_evidence,
    register_launch,
)

if TYPE_CHECKING:
    from codex_session_gc.execution.launch import LaunchBinding
    from codex_session_gc.execution.transport import ChildCommand

_GRANT = b"codex-session-gc:grant-v1\n"
_EXIT = {
    TerminalKind.CONFIRMED: 0,
    TerminalKind.REFUSED: 1,
    TerminalKind.PARTIAL: 2,
    TerminalKind.UNKNOWN: 3,
}


class OneShotError(Exception):
    """Pre-grant refusal: no request bytes were released to the helper."""


@dataclass(frozen=True, slots=True)
class OneShotResult:
    """Bounded observations, with mutation uncertainty separate from child exit."""

    status: TerminalKind
    request_digest: bytes
    reply: Payload | None
    stdout: bytes
    stderr: bytes
    returncode: int | None
    reaped: bool
    terminal_recorded: bool
    closure_recorded: bool
    detail: str


def dispatch_once(
    binding: LaunchBinding,
    command: ChildCommand,
    unit_id: UnitId,
    request: Payload,
    evidence: Payload,
    *,
    deadline_seconds: float = 5.0,
    maximum_request_bytes: int = 1_048_576,
    maximum_output_bytes: int = 1_048_576,
    cleanup_seconds: float = 1.0,
) -> OneShotResult:
    """Record launch/identity/intent, then grant exactly one payload and EOF.

    A pre-grant failure raises OneShotError and never sends native input. Once
    release is possible, any delivery, reply, journal or reap ambiguity returns
    UNKNOWN. Existing journal transitions prohibit resending the same unit.
    """
    _validate_bounds(deadline_seconds, cleanup_seconds, maximum_request_bytes, maximum_output_bytes)
    if len(request.data) > maximum_request_bytes:
        raise ValueError("The request exceeds its byte bound.")
    deadline = time.monotonic() + deadline_seconds
    environment = dict(command.environment)
    environment[OWNED_MARKER_ENV] = str(uuid4())
    launcher = owned_launcher_command(replace(command, environment=environment))
    try:
        record_launch_intent(
            binding.receipt,
            binding.attempt_id,
            LaunchIntent(binding.launch_id, binding.generation, binding.purpose, request),
        )
    except Exception as error:
        raise OneShotError("Launch intent was refused; no child was started.") from error
    process = None
    registered = False
    try:
        process = subprocess.Popen(
            [str(launcher.executable), *launcher.arguments],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(launcher.environment),
            pass_fds=launcher.inherited_descriptors,
            bufsize=0,
        )
        identity = observe_owned_child(process.pid, environment)
        if identity is None:
            raise OneShotError("The waiting child identity is unavailable.")  # noqa: TRY301
        register_launch(
            binding.receipt,
            binding.attempt_id,
            LaunchRegistration(binding.launch_id, binding.generation, identity.incarnation()),
        )
        registered = True
        record_dispatch_intent(
            binding.receipt, binding.attempt_id, unit_id, request, evidence, identity.incarnation()
        )
    except BaseException as error:
        reaped = process is None or _reap(process, cleanup_seconds)
        cleanup = _failure_evidence("pre_grant_refusal", b"", b"", None)
        try:
            if registered and reaped:
                record_launch_closure(
                    binding.receipt,
                    binding.attempt_id,
                    binding.launch_id,
                    binding.generation,
                    TerminalKind.REFUSED,
                    cleanup,
                )
            elif not registered:
                fence_launch(
                    binding.receipt,
                    binding.attempt_id,
                    binding.launch_id,
                    binding.generation,
                    cleanup,
                )
        except Exception as cleanup_error:  # noqa: BLE001
            message = f"Pre-grant refusal; journal cleanup failed: {cleanup_error}"
            raise OneShotError(message) from error
        if not isinstance(error, Exception):
            raise
        message = f"Pre-grant refusal; owned child reaped={reaped}: {error}"
        raise OneShotError(message) from error
    return _finish(
        binding, process, unit_id, request, deadline, maximum_output_bytes, cleanup_seconds
    )


def _finish(
    binding: LaunchBinding,
    process: subprocess.Popen[bytes],
    unit_id: UnitId,
    request: Payload,
    deadline: float,
    maximum_output_bytes: int,
    cleanup_seconds: float,
) -> OneShotResult:
    stdout, stderr = bytearray(), bytearray()
    status = TerminalKind.UNKNOWN
    reply = None
    detail = "Helper delivery or outcome is unavailable."
    interrupted: BaseException | None = None
    try:
        _exchange(process, _GRANT + request.data, deadline, maximum_output_bytes, stdout, stderr)
        status = _reply_status(bytes(stdout), request.digest, process.returncode)
        reply = Payload("application/vnd.codex-session-gc.native-outcome+json", 1, bytes(stdout))
        detail = "Digest and helper exit agree with the observed outcome."
    except BaseException as error:  # noqa: BLE001
        # Preserve durable uncertainty and reap before propagating interruption.
        detail = str(error)
        if not isinstance(error, Exception):
            interrupted = error
    finally:
        reaped = _reap(process, cleanup_seconds)
    closure_recorded = False
    if reaped:
        try:
            record_launch_closure(
                binding.receipt,
                binding.attempt_id,
                binding.launch_id,
                binding.generation,
                TerminalKind.CONFIRMED,
                _failure_evidence(
                    "owned_child_reaped", bytes(stdout), bytes(stderr), process.returncode
                ),
            )
            closure_recorded = True
        except Exception as error:  # noqa: BLE001
            status, detail = TerminalKind.UNKNOWN, f"Launch closure journal failed: {error}"
    else:
        status, detail = (
            TerminalKind.UNKNOWN,
            "The owned child could not be reaped within cleanup bounds.",
        )
    terminal_recorded = False
    terminal = (
        reply
        if reply is not None and closure_recorded
        else _failure_evidence(detail, bytes(stdout), bytes(stderr), process.returncode)
    )
    try:
        record_terminal_evidence(
            binding.receipt, binding.attempt_id, unit_id, request.digest, status, terminal
        )
        terminal_recorded = True
    except Exception as error:  # noqa: BLE001
        status, detail = TerminalKind.UNKNOWN, f"Terminal journal failed: {error}"
    if interrupted is not None:
        raise interrupted
    return OneShotResult(
        status,
        request.digest,
        reply,
        bytes(stdout),
        bytes(stderr),
        process.returncode,
        reaped,
        terminal_recorded,
        closure_recorded,
        detail,
    )


def _exchange(  # noqa: C901
    process: subprocess.Popen[bytes],
    data: bytes,
    deadline: float,
    cap: int,
    stdout: bytearray,
    stderr: bytearray,
) -> None:
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise ValueError("Owned stdio pipes are unavailable.")
    offset = 0
    with selectors.DefaultSelector() as selector:
        for stream, event in (
            (process.stdin, selectors.EVENT_WRITE),
            (process.stdout, selectors.EVENT_READ),
            (process.stderr, selectors.EVENT_READ),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("The helper I/O deadline expired.")
            ready = selector.select(remaining)
            if not ready:
                raise TimeoutError("The helper I/O deadline expired.")
            for key, event in ready:
                descriptor = key.fd
                if event & selectors.EVENT_WRITE:
                    written = os.write(descriptor, memoryview(data)[offset : offset + 65_536])
                    if written <= 0:
                        raise OSError("The helper input did not advance.")
                    offset += written
                    if offset == len(data):
                        selector.unregister(key.fileobj)
                        process.stdin.close()
                elif _read_output(
                    descriptor, cap, stdout, stderr, is_stdout=descriptor == process.stdout.fileno()
                ):
                    selector.unregister(key.fileobj)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The helper exit deadline expired.")
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("The helper exit deadline expired.") from error


def _reply_status(raw: bytes, digest: bytes, returncode: int | None) -> TerminalKind:
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("The helper outcome must be one JSON object.")  # noqa: TRY004
    raw_status = value.get("status")
    if not isinstance(raw_status, str):
        raise ValueError("The helper outcome status must be a string.")  # noqa: TRY004
    status = TerminalKind(raw_status)
    observed = value.get("request_sha256")
    if (
        not isinstance(observed, list)
        or len(observed) != len(digest)
        or any(type(item) is not int or not 0 <= item < 2**8 for item in observed)
        or bytes(observed) != digest
    ):
        raise ValueError("The helper outcome request digest does not match.")
    if returncode != _EXIT[status]:
        raise ValueError("The helper outcome and exit status disagree.")
    return status


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("The helper outcome has duplicate JSON keys.")
        value[key] = item
    return value


def _invalid_constant(value: str) -> object:
    message = f"Non-JSON constant in helper outcome: {value}"
    raise ValueError(message)


def _failure_evidence(detail: str, stdout: bytes, stderr: bytes, code: int | None) -> Payload:
    return Payload(
        "application/codex-session-gc.one-shot-observation+json",
        1,
        json.dumps(
            {
                "detail": detail,
                "returncode": code,
                "stdout_sha256": sha256(stdout).hexdigest(),
                "stderr_sha256": sha256(stderr).hexdigest(),
            },
            sort_keys=True,
        ).encode(),
    )


def _reap(process: subprocess.Popen[bytes], seconds: float) -> bool:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=seconds)
        return process.poll() is not None
    except OSError, subprocess.TimeoutExpired:
        return False
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _validate_bounds(deadline: float, cleanup: float, request: int, output: int) -> None:
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0
        for value in (deadline, cleanup)
    ):
        raise ValueError("Deadlines must be finite positive seconds.")
    if any(type(value) is not int or value < 1 for value in (request, output)):
        raise ValueError("Byte bounds must be positive integers.")


def _read_output(
    descriptor: int, cap: int, stdout: bytearray, stderr: bytearray, *, is_stdout: bool
) -> bool:
    """Drain one ready descriptor without retaining more than the combined cap."""
    remaining = cap - len(stdout) - len(stderr)
    chunk = os.read(descriptor, min(65_536, remaining + 1))
    if len(chunk) > remaining:
        raise ValueError("The helper output exceeds its byte bound.")
    (stdout if is_stdout else stderr).extend(chunk)
    return not chunk
