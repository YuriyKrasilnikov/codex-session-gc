# Copyright (c) 2026

"""Doctor and MCP share explicit support selection without new path authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
import threading
import uuid
from typing import TYPE_CHECKING

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from codex_session_gc import diagnostics
from codex_session_gc.artifacts import ArtifactIdentity
from codex_session_gc.cli.main import main
from codex_session_gc.cli.mcp_capability import McpCapability
from codex_session_gc.cli.mcp_server import CatalogueService, SupportScope
from codex_session_gc.compatibility.observation import ImportedSupport
from codex_session_gc.compatibility.store import promote_support
from codex_session_gc.installation.model import InstallLayout
from codex_session_gc.installation.provenance import PREDICATE_TYPE, verify_artifact
from tests.test_support_intake import _fixture

if TYPE_CHECKING:
    from pathlib import Path

pytest_plugins = ["tests.test_doctor"]


def _require(condition: object, message: str) -> None:
    if not condition:
        pytest.fail(message)


def _data(result: CallToolResult) -> dict[str, object]:
    if result.is_error or not result.content or not isinstance(result.content[0], TextContent):
        pytest.fail("MCP request did not return a text result")
    value: object = json.loads(result.content[0].text)
    if not isinstance(value, dict):
        pytest.fail("MCP result is not an object")
    return value


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        pytest.fail("Expected a structured object")
    return value


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    values = {}
    for path in root.rglob("*"):
        if path.is_file():
            info = path.stat()
            values[str(path.relative_to(root))] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
    return values


def _record_assertions(report: dict[str, object], digest: str) -> None:
    support = report["source_support"]
    if not isinstance(support, dict):
        pytest.fail("Support observation is absent")
    if support.get("record_sha256") != digest or support.get("claim_verified") is not False:
        pytest.fail("Diagnosis did not retain the selected unverified support record")
    if support.get("authorizes_operation") is not False:
        pytest.fail("Support claim gained cleanup authority")


def test_doctor_and_mcp_select_explicit_records_without_restart(
    tmp_path: Path,
    environment: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New imported bytes are visible on the next call under a fixed MCP scope."""
    home, helper = environment
    layout, identifier, artifact, identity, source, verifier = _fixture(tmp_path)

    async def promote(expected: ArtifactIdentity, asset_id: int) -> ImportedSupport:
        with await verify_artifact(artifact, expected, source, trusted_gh=verifier) as lease:
            receipt = promote_support(layout, identifier, lease, asset_id)
        return ImportedSupport(layout, identifier, expected.sha256, receipt.context_sha256)

    async def scenario() -> None:
        first = await promote(identity, 123)
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "codex_session_gc.cli.mcp_server",
                "--home",
                str(home),
                "--helper",
                str(helper),
                "--read-only",
                "--support-install-dir",
                str(layout.base),
                "--support-installation-id",
                str(identifier),
            ],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool = next(item for item in tools.tools if item.name == "compatibility_status")
            _require(
                set(tool.input_schema.get("properties", {})) == {"record_sha256", "context_sha256"},
                "MCP exposed filesystem authority to a client",
            )
            original = _data(await session.call_tool("compatibility_status", {}))
            _require(
                _mapping(original["source_support"])["origin"] == "bundled_static_app_data",
                "Bundled default changed",
            )
            first_report = _data(
                await session.call_tool(
                    "compatibility_status",
                    {
                        "record_sha256": first.record_sha256,
                        "context_sha256": first.context_sha256,
                    },
                )
            )
            _record_assertions(first_report, first.record_sha256)
            record = json.loads(artifact.read_bytes())
            record["execution"]["helper_sha256"] = hashlib.sha256(helper.read_bytes()).hexdigest()
            record["report_sha256"] = "9" * 64
            raw = json.dumps(record).encode()
            artifact.write_bytes(raw)
            second_identity = ArtifactIdentity(hashlib.sha256(raw).hexdigest(), len(raw))
            proof = [
                {
                    "verificationResult": {
                        "statement": {
                            "predicateType": PREDICATE_TYPE,
                            "subject": [{"digest": {"sha256": second_identity.sha256}}],
                        }
                    }
                }
            ]
            verifier.write_text(f"#!{sys.executable}\nprint({json.dumps(proof)!r})\n")
            second = await promote(second_identity, 124)
            before = _snapshot(layout.base)
            second_report = _data(
                await session.call_tool(
                    "compatibility_status",
                    {
                        "record_sha256": second.record_sha256,
                        "context_sha256": second.context_sha256,
                    },
                )
            )
            _record_assertions(second_report, second.record_sha256)
            for field in ("profile", "helper", "databases"):
                _require(first_report[field] == second_report[field], "Local observations changed")
            missing = _data(
                await session.call_tool(
                    "compatibility_status",
                    {
                        "record_sha256": "f" * 64,
                        "context_sha256": second.context_sha256,
                    },
                )
            )
            _require(
                _mapping(missing["source_support"])["availability"] == "unknown",
                "Unexpected fallback",
            )
            _require(
                missing["helper"] == first_report["helper"], "Local helper observation missing"
            )
            partial = await session.call_tool("compatibility_status", {"record_sha256": "f" * 64})
            _require(partial.is_error, "Incomplete selection was accepted")
            _require(_snapshot(layout.base) == before, "Read-only MCP changed installation files")
        args = [
            "doctor",
            "--home",
            str(home),
            "--helper",
            str(helper),
            "--json",
            "--support-install-dir",
            str(layout.base),
            "--support-installation-id",
            str(identifier),
            "--support-record-sha256",
            second.record_sha256,
            "--support-context-sha256",
            second.context_sha256,
        ]

        def no_network(_socket: socket.socket, _address: object) -> None:
            pytest.fail("Doctor attempted to open a network connection")

        monkeypatch.setattr(socket.socket, "connect", no_network)
        main(args)
        doctor = json.loads(capsys.readouterr().out)
        _record_assertions(doctor, second.record_sha256)
        _require(
            doctor["source_support"] == second_report["source_support"],
            "Doctor and MCP selected different observations",
        )
        _require(_snapshot(layout.base) == before, "Doctor changed installation files")

    asyncio.run(asyncio.wait_for(scenario(), timeout=90))


def test_doctor_and_server_reject_partial_local_scope(tmp_path: Path) -> None:
    """Operators cannot accidentally select a partial imported-record identity."""
    with pytest.raises(SystemExit):
        main(["doctor", "--home", str(tmp_path), "--support-record-sha256", "a" * 64])
    with pytest.raises(SystemExit):
        main(["mcp", "--home", str(tmp_path), "--support-install-dir", str(tmp_path)])


def test_cancelled_mcp_read_releases_worker_after_read_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation does not claim to interrupt an already running filesystem read."""
    started = threading.Event()
    release = threading.Event()
    done = threading.Event()
    expected_arguments = 3

    def controlled(*args: object) -> object:
        if len(args) != expected_arguments:
            pytest.fail("Unexpected observation inputs")
        started.set()
        try:
            release.wait(timeout=5)
        finally:
            done.set()
        return None

    monkeypatch.setattr(diagnostics, "inspect_selected_compatibility", controlled)
    scope = SupportScope(InstallLayout(tmp_path), uuid.uuid4())
    service = CatalogueService(tmp_path, capability=McpCapability.READ_ONLY, support_scope=scope)

    async def scenario() -> None:
        task = asyncio.create_task(service.compatibility_status("a" * 64, "b" * 64))
        try:
            _require(await asyncio.to_thread(started.wait, 5), "Observation did not start")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            _require(not done.is_set(), "Cancellation claimed to finish the pending worker")
        finally:
            release.set()
        _require(await asyncio.to_thread(done.wait, 5), "Observation worker did not finish")

    asyncio.run(scenario())
