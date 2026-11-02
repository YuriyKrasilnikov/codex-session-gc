# Copyright (c) 2026

"""Witness stock 0.166 attached-fork publication and native writer-lock exclusion.

The complete JSON-RPC exchange is written only to the explicitly named private
qualification trace. All runtime homes and processes are owned by this probe.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def absolute_path(value: str) -> Path:
    """Require an explicit absolute path for every probe input and output."""
    path = Path(value)
    if not path.is_absolute():
        message = "Path must be absolute"
        raise argparse.ArgumentTypeError(message)
    return path


def absolute_file(value: str) -> Path:
    """Reject a missing input before importing the fixture harness."""
    path = absolute_path(value)
    if not path.is_file():
        message = "Input must be an existing file"
        raise argparse.ArgumentTypeError(message)
    return path


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--stock", type=absolute_file, required=True)
parser.add_argument("--fixture", type=absolute_file, required=True)
parser.add_argument("--native", type=absolute_file, required=True)
parser.add_argument("--trace", type=absolute_path, required=True)
arguments = parser.parse_args()

ROOT = Path(__file__).resolve().parents[3]
STOCK = arguments.stock
FIXTURE = arguments.fixture
NATIVE = arguments.native
STOCK_SHA256 = "b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8"
FIXTURE_SHA256 = "a08fa74e22b90ff9b181a460c65e825647cf11014cb0ab552bdc2835364d11d3"
NATIVE_SHA256 = "a21f923664a98c096758eca54c91f2d79b507dd8576793f1b4fe214b86313e4d"

# Existing fixture harness owns and reaps registered subprocesses on interpreter exit.
os.environ["GC_NATIVE_BINARY"] = str(NATIVE)
os.environ["GC_NATIVE_FIXTURE"] = str(FIXTURE)
spec = importlib.util.spec_from_file_location(
    "closed_work_batch", ROOT / "native/helper/tests/closed_work_batch.py"
)
if spec is None or spec.loader is None:
    message = "Native fixture harness is unavailable"
    raise RuntimeError(message)
batch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(batch)
s = batch.s


def require(condition: object, message: str) -> None:
    """Fail the witness rather than interpreting missing evidence as a skip."""
    if not condition:
        raise RuntimeError(message)


def digest(path: Path) -> str:
    """Stream one executable identity."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def finish(process: subprocess.Popen[bytes], *, send_exit: bool = False) -> None:
    """Close a child and terminate its owned process group if it outlives the deadline."""
    if process.stdin is not None and not process.stdin.closed:
        try:
            if send_exit:
                process.stdin.write(b"exit\n")
                process.stdin.flush()
            process.stdin.close()
        except BrokenPipeError, OSError:
            pass
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=12)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


class Rpc:
    """Bounded line-oriented client for one owned app-server process."""

    def __init__(self, home: Path, trace: list[dict[str, object]]) -> None:
        """Start one owned server in the disposable Codex home."""
        self.trace = trace
        self.pending = bytearray()
        self.sequence = 0
        self.received_bytes = 0
        environment = {
            key: os.environ[key]
            for key in ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if key in os.environ
        }
        environment.update(
            CODEX_HOME=str(home), HOME=str(home), OTEL_SDK_DISABLED="true", DO_NOT_TRACK="1"
        )
        self.process = s.spawn(
            [str(STOCK), "app-server", "--stdio"],
            cwd=home,
            env=environment,
            stdin=-1,
            stdout=-1,
            stderr=-3,
            start_new_session=True,
        )
        self.reader = selectors.DefaultSelector()
        self.reader.register(self.process.stdout, selectors.EVENT_READ)

    def send(self, message: dict[str, object]) -> None:
        """Send one bounded JSON-RPC object to the owned server."""
        self.trace.append({"direction": "send", "message": message})
        self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        self.process.stdin.flush()

    def request(
        self, method: str, params: dict[str, object], seconds: float = 25
    ) -> dict[str, Any]:
        """Read the matching response while retaining unrelated server notifications."""
        self.sequence += 1
        identity = self.sequence
        self.send({"id": identity, "method": method, "params": params})
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while b"\n" in self.pending:
                line, _, rest = self.pending.partition(b"\n")
                self.pending = bytearray(rest)
                if not line:
                    continue
                self.received_bytes += len(line)
                require(
                    self.received_bytes <= 16 * 1024 * 1024, "Server trace exceeded its byte bound"
                )
                message = json.loads(line)
                require(isinstance(message, dict), "Server returned a non-object message")
                self.trace.append({"direction": "receive", "message": message})
                max_notifications = 10000
                require(
                    len(self.trace) <= max_notifications,
                    "Server exceeded the notification count bound",
                )
                if message.get("id") == identity:
                    return message
            require(len(self.pending) <= 4 * 1024 * 1024, "Server line exceeded the read bound")
            events = self.reader.select(max(0, deadline - time.monotonic()))
            if not events:
                break
            block = os.read(self.process.stdout.fileno(), 65536)
            require(bool(block), "App-server closed before its response")
            self.pending.extend(block)
        message = f"App-server request {method} timed out"
        raise TimeoutError(message)

    def initialize(self) -> None:
        """Negotiate the official experimental attached-fork capability."""
        response = self.request(
            "initialize",
            {
                "clientInfo": {"name": "stock-witness", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        require("result" in response, "App-server initialization failed")
        self.send({"method": "initialized", "params": {}})

    def close(self) -> None:
        """Stop the owned server and release its pipe resources."""
        self.reader.close()
        finish(self.process)
        require(self.process.returncode == 0, "App-server did not exit cleanly")


def header(path: Path) -> dict[str, object]:
    """Read the first bounded canonical session metadata record."""
    with path.open("rb") as source:
        first = source.readline(1024 * 1024 + 1)
    require(len(first) <= 1024 * 1024 and first.endswith(b"\n"), "Rollout metadata is incomplete")
    envelope = json.loads(first)
    require(
        envelope.get("type") == "session_meta" and isinstance(envelope.get("payload"), dict),
        "Rollout first record is not session metadata",
    )
    return envelope["payload"]


def rollout_paths(home: Path) -> set[Path]:
    """Collect physical files in the owned active and archived namespaces."""
    return {
        path
        for name in ("sessions", "archived_sessions")
        for path in (home / name).rglob("*.jsonl")
    }


def wait_rollout(home: Path, thread_id: str, seconds: float = 25) -> Path:
    """Require a nonempty stock-produced root rollout after turn start."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        matches = [
            path
            for path in rollout_paths(home)
            if path.name.endswith(f"-{thread_id}.jsonl") and path.stat().st_size
        ]
        if len(matches) == 1:
            return matches[0]
        time.sleep(0.02)
    message = "The stock producer did not materialize its root rollout"
    raise TimeoutError(message)


def wait_guard(process: subprocess.Popen[bytes]) -> None:
    """Read the guard's readiness marker under a fixed deadline."""
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    deadline = time.monotonic() + 15
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while time.monotonic() < deadline and b"\n" not in data:
            if not selector.select(deadline - time.monotonic()):
                break
            chunk = os.read(fd, 1024)
            require(bool(chunk), "Native guard closed before readiness")
            data.extend(chunk)
            max_ready_bytes = 1024
            require(len(data) <= max_ready_bytes, "Native guard readiness exceeded its bound")
    require(data == b"ready\n", "Native parent guard was not acquired")


def attached_rows(home: Path, parent: str) -> tuple[str, ...]:
    """Read only IDs in the producer's attached-child projection."""
    with sqlite3.connect(f"file:{home / 'state_5.sqlite'}?mode=ro", uri=True) as database:
        return tuple(
            row[0]
            for row in database.execute(
                "SELECT id FROM threads WHERE attached_thread_parent_id=? ORDER BY id", (parent,)
            )
        )


def seed_parent(home: Path, trace: list[dict[str, object]]) -> tuple[str, dict[str, object]]:
    """Materialize stock root history before attempting attached creation."""
    first = Rpc(home, trace)
    try:
        first.initialize()
        started = first.request(
            "thread/start",
            {
                "model": "fixture",
                "modelProvider": "fixture",
                "cwd": str(home),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "historyMode": "paginated",
                "persistExtendedHistory": True,
                "ephemeral": False,
            },
        )
        require("result" in started, "Stock thread creation failed")
        parent = started["result"]["thread"]["id"]
        fork = {
            "threadId": parent,
            "attachToSource": True,
            "ephemeral": False,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "model": "fixture",
            "modelProvider": "fixture",
        }
        unpublished = first.request("thread/fork", fork)
        require(
            "error" in unpublished and not rollout_paths(home),
            "Unpublished source was unexpectedly forkable",
        )
        turn = first.request(
            "turn/start",
            {
                "threadId": parent,
                "input": [{"type": "text", "text": "Disposable local storage qualification."}],
            },
        )
        require("result" in turn, "Stock turn could not start")
        root = wait_rollout(home, parent)
        root_header = header(root)
        require(root_header.get("id") == parent, "Root rollout identity differs")
    finally:
        first.close()
    return parent, fork


def check_attached_fork(
    home: Path, parent: str, fork: dict[str, object], trace: list[dict[str, object]]
) -> None:
    """Observe refusal under ownership and publication after release."""
    guard = s.spawn(
        [str(FIXTURE), "hold-guard", str(home), parent],
        stdin=-1,
        stdout=-1,
        stderr=-3,
        start_new_session=True,
    )
    second = None
    try:
        wait_guard(guard)
        second = Rpc(home, trace)
        second.initialize()
        before = rollout_paths(home)
        before_rows = attached_rows(home, parent)
        refused = second.request("thread/fork", fork)
        require(
            "error" in refused,
            "Stock attached fork did not refuse the native-held writer guard",
        )
        failure = refused["error"]
        require(
            isinstance(failure, dict)
            and isinstance(failure.get("message"), str)
            and "already has an active writer" in failure["message"]
            and parent in failure["message"],
            "Stock fork refused for a reason other than parent writer ownership",
        )
        require(
            rollout_paths(home) == before,
            "A child rollout appeared while parent guard was held",
        )
        require(
            attached_rows(home, parent) == before_rows,
            "An attached SQL child appeared while the parent guard was held",
        )
        finish(guard, send_exit=True)
        require(guard.returncode == 0, "Native guard did not release cleanly")
        allowed = second.request("thread/fork", fork)
        require("result" in allowed, "Stock attached fork did not succeed after guard release")
        child = allowed["result"]["thread"]["id"]
        require(child != parent, "Stock fork did not create a distinct child")
        child_path = wait_rollout(home, child)
        child_header = header(child_path)
        require(
            child_header.get("id") == child
            and child_header.get("attached_thread") is True
            and child_header.get("forked_from_id") == parent,
            "Canonical attached metadata differs",
        )
        with sqlite3.connect(f"file:{home / 'state_5.sqlite'}?mode=ro", uri=True) as database:
            row = database.execute(
                "SELECT attached_thread_parent_id FROM threads WHERE id=?", (child,)
            ).fetchone()
        require(row == (parent,), "Attached SQL projection differs from canonical metadata")
        trace.append(
            {
                "stock_parent": parent,
                "stock_child": child,
                "child_header": child_header,
                "attached_sql_parent": row[0],
                "guarded_fork_error": refused.get("error"),
            }
        )
    finally:
        try:
            if second is not None:
                second.close()
        finally:
            if guard.poll() is None:
                finish(guard, send_exit=True)


def run(trace: list[dict[str, object]]) -> dict[str, object]:
    """Exercise producer exclusion while native holds the parent's writer guard."""
    identities = {"stock": digest(STOCK), "fixture": digest(FIXTURE), "native": digest(NATIVE)}
    require(
        identities == {"stock": STOCK_SHA256, "fixture": FIXTURE_SHA256, "native": NATIVE_SHA256},
        "A required binary identity differs",
    )
    version = s.spawn(
        [str(STOCK), "--version"], stdin=-3, stdout=-1, stderr=-3, start_new_session=True
    )
    try:
        stdout, _ = version.communicate(timeout=10)
        require(
            version.returncode == 0 and stdout.strip() == b"codex-cli 0.166.0",
            "Stock executable version differs",
        )
    finally:
        finish(version)
    with tempfile.TemporaryDirectory(prefix="gc-stock-attached-") as temporary:
        home = Path(temporary)
        (home / "config.toml").write_text(
            'model = "fixture"\nmodel_provider = "fixture"\n'
            '[model_providers.fixture]\nname = "fixture"\nbase_url = "http://127.0.0.1:9/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            "request_max_retries = 0\nstream_max_retries = 0\n"
            "[analytics]\nenabled = false\n",
            encoding="utf-8",
        )
        parent, fork = seed_parent(home, trace)
        check_attached_fork(home, parent, fork, trace)
        return {
            "status": "passed",
            "stock_version": "0.166.0",
            "binary_sha256": identities,
            "guarded_attached_fork": "refused_without_publication",
            "after_release": "published_and_projected",
        }


def main() -> None:
    """Write raw private qualification evidence and a concise public-safe summary."""
    trace: list[dict[str, object]] = []
    outcome: dict[str, object] = {"status": "failed"}
    try:
        outcome = run(trace)
    finally:
        fd = os.open(arguments.trace, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump({"outcome": outcome, "trace": trace}, output, indent=2)
            output.write("\n")
    sys.stdout.write(json.dumps(outcome, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
