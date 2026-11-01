# Copyright (c) 2026

"""Exercise release discovery and download without external network access."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pytest

from codex_session_gc.artifacts import ArtifactIdentity
from codex_session_gc.cli import main as cli
from codex_session_gc.installation import __main__ as entry
from codex_session_gc.installation import activation
from codex_session_gc.installation.__main__ import (
    Action,
    InstallerError,
    Limits,
    Request,
    Tools,
    execute,
)
from codex_session_gc.installation.activation import (
    ActivationError,
    ActivationStatus,
    activation_status,
)
from codex_session_gc.installation.discovery import DiscoveryError, discover_release
from codex_session_gc.installation.download import DownloadError, HttpsTransport, download_asset
from codex_session_gc.installation.model import InstallLayout
from codex_session_gc.installation.payload import PayloadRefusalError
from codex_session_gc.installation.provenance import PREDICATE_TYPE, VerificationOption
from tests import test_installation_payload as fixture
from tests import test_installation_transaction as transaction_fixture
from tools.build_installer import InstallerBuildError, build_installer

if TYPE_CHECKING:
    from collections.abc import Callable


TAG = "v1.2.3"
VERSION = "1.2.3"
COMMIT = "a" * 40
REPOSITORY = "YuriyKrasilnikov/codex-session-gc"
API = f"https://api.github.com/repos/{REPOSITORY}"
NAME = f"codex-session-gc-{VERSION}-linux-x86_64.tar.gz"
ASSET = f"https://github.com/{REPOSITORY}/releases/download/{TAG}/{NAME}"
CONTENT = b"owned synthetic artifact"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


class FakeTransport:
    """Return explicitly supplied response bytes and retain requested locations."""

    def __init__(self, responses: dict[str, bytes]) -> None:
        """Bind a finite response mapping."""
        self.responses = responses
        self.urls: list[str] = []

    async def get(
        self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
    ) -> None:
        """Emit a response in finite chunks."""
        _require(limit > 0 and deadline > 0, "transport bounds are invalid")
        self.urls.append(url)
        data = self.responses[url]
        for start in range(0, len(data), 5):
            sink(data[start : start + 5])


def _metadata() -> dict[str, object]:
    return {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "target_commitish": "untrusted mutable branch",
        "assets": [
            {
                "name": NAME,
                "browser_download_url": ASSET,
                "state": "uploaded",
                "size": len(CONTENT),
                "digest": f"sha256:{DIGEST}",
            }
        ],
    }


def _transport(metadata: dict[str, object], *, annotated: bool = False) -> FakeTransport:
    tag_object = (
        {"type": "tag", "sha": "b" * 40} if annotated else {"type": "commit", "sha": COMMIT}
    )
    values = {
        f"{API}/releases/tags/{TAG}": json.dumps(metadata).encode(),
        f"{API}/releases/latest": json.dumps(metadata).encode(),
        f"{API}/git/ref/tags/{TAG}": json.dumps(
            {"ref": f"refs/tags/{TAG}", "object": tag_object}
        ).encode(),
        f"{API}/git/tags/{'b' * 40}": json.dumps(
            {"sha": "b" * 40, "tag": TAG, "object": {"type": "commit", "sha": COMMIT}}
        ).encode(),
        ASSET: CONTENT,
    }
    return FakeTransport(values)


def _require(condition: object, message: str) -> None:
    if not condition:
        pytest.fail(message)


def test_release_tag_resolves_commit_without_mutable_target() -> None:
    """Resolve an annotated tag once and bind the verified workflow source ref."""
    transport = _transport(_metadata(), annotated=True)

    async def task() -> None:
        result = await discover_release(
            transport,
            "latest",
            max_metadata_bytes=65536,
            network_seconds=5,
        )
        _require(result.tag == TAG and result.version == VERSION, "release version differs")
        _require(
            result.source.commit == COMMIT and result.source.ref == "refs/heads/main",
            "source differs",
        )
        _require(result.artifact == ArtifactIdentity(DIGEST, len(CONTENT)), "artifact differs")
        _require(
            transport.urls.count(f"{API}/releases/latest") == 1, "latest was resolved repeatedly"
        )

    asyncio.run(task())


@pytest.mark.parametrize(
    "kind", ["draft", "prerelease", "digest", "url", "size", "duplicate", "tag"]
)
def test_malformed_release_never_admits_asset(kind: str) -> None:
    """Reject unsupported versions and ambiguous or invalid asset metadata."""
    metadata = _metadata()
    raw_assets = metadata["assets"]
    if not isinstance(raw_assets, list) or any(not isinstance(item, dict) for item in raw_assets):
        pytest.fail("Synthetic asset fixture has invalid structure")
    assets = cast("list[dict[str, object]]", raw_assets)
    asset = assets[0]
    if kind in ("draft", "prerelease"):
        metadata[kind] = True
    elif kind == "tag":
        metadata["tag_name"] = "v1.2.3-rc1"
    elif kind == "digest":
        asset["digest"] = None
    elif kind == "size":
        asset["size"] = 0
    elif kind == "url":
        asset["browser_download_url"] = "https://example.com/asset"
    else:
        assets.append(asset.copy())

    async def task() -> None:
        with pytest.raises(DiscoveryError):
            await discover_release(
                _transport(metadata),
                TAG,
                max_metadata_bytes=65536,
                network_seconds=5,
            )

    asyncio.run(task())


def test_owned_download_checks_bytes_and_mode(tmp_path: Path) -> None:
    """Write the expected artifact exclusively with private permissions."""
    target = tmp_path / "asset"

    async def task() -> None:
        await download_asset(
            _transport(_metadata()),
            ASSET,
            ArtifactIdentity(DIGEST, len(CONTENT)),
            target,
            deadline=asyncio.get_running_loop().time() + 5,
        )

    asyncio.run(task())
    _require(target.read_bytes() == CONTENT, "asset differs")
    private_mode = 0o600
    _require(target.stat().st_mode & 0o777 == private_mode, "asset permissions differ")


@pytest.mark.parametrize("content", [b"short", CONTENT + b"extra", b"x" * len(CONTENT)])
def test_failed_download_removes_only_owned_partial(tmp_path: Path, content: bytes) -> None:
    """Wrong, truncated or oversized bytes do not leave an owned staging artifact."""
    transport = _transport(_metadata())
    transport.responses[ASSET] = content
    target = tmp_path / "asset"

    async def task() -> None:
        with pytest.raises(DownloadError):
            await download_asset(
                transport,
                ASSET,
                ArtifactIdentity(DIGEST, len(CONTENT)),
                target,
                deadline=asyncio.get_running_loop().time() + 5,
            )

    asyncio.run(task())
    _require(not os.path.lexists(target), "partial asset was left behind")


def test_untrusted_asset_url_is_refused_before_transfer(tmp_path: Path) -> None:
    """Never send requests to a non-GitHub or non-HTTPS asset URL."""
    transport = _transport(_metadata())

    async def task() -> None:
        with pytest.raises(DownloadError):
            await download_asset(
                transport,
                "http://github.com/asset",
                ArtifactIdentity(DIGEST, len(CONTENT)),
                tmp_path / "asset",
                deadline=asyncio.get_running_loop().time() + 5,
            )

    asyncio.run(task())
    _require(not transport.urls, "untrusted URL was requested")


def test_interrupted_download_removes_owned_partial(tmp_path: Path) -> None:
    """A timed-out transfer removes its own partial asset."""

    class Stalled(FakeTransport):
        """Emit one partial chunk and remain suspended."""

        async def get(
            self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
        ) -> None:
            """Simulate a transfer interrupted after its first bytes."""
            _require(bool(url) and limit > 0 and deadline > 0, "invalid test request")
            sink(b"partial")
            await asyncio.Event().wait()

    target = tmp_path / "asset"

    async def task() -> None:
        with pytest.raises(TimeoutError):
            await download_asset(
                Stalled({}),
                ASSET,
                ArtifactIdentity(DIGEST, len(CONTENT)),
                target,
                deadline=asyncio.get_running_loop().time() + 0.05,
            )

    asyncio.run(task())
    _require(not os.path.lexists(target), "interrupted asset was left behind")


def test_existing_staging_file_is_never_replaced(tmp_path: Path) -> None:
    """An existing unknown destination is not overwritten or removed."""
    target = tmp_path / "asset"
    target.write_bytes(b"unmanaged")

    async def task() -> None:
        with pytest.raises(FileExistsError):
            await download_asset(
                _transport(_metadata()),
                ASSET,
                ArtifactIdentity(DIGEST, len(CONTENT)),
                target,
                deadline=asyncio.get_running_loop().time() + 5,
            )

    asyncio.run(task())
    _require(target.read_bytes() == b"unmanaged", "unmanaged asset changed")


def test_tls_transport_streams_framed_response_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A controlled socket adapter exercises HTTP framing and bounded chunk delivery."""
    requested: list[bytes] = []

    class Writer:
        """Record one synthetic request and complete shutdown."""

        def write(self, data: bytes) -> None:
            """Capture the request bytes."""
            requested.append(data)

        async def drain(self) -> None:
            """Complete the synthetic write."""

        def close(self) -> None:
            """Close the synthetic writer."""

        async def wait_closed(self) -> None:
            """Complete the synthetic close."""

    async def connect(
        host: str, port: int, **_kwargs: object
    ) -> tuple[asyncio.StreamReader, Writer]:
        https_port = 443
        _require(host == "api.github.com" and port == https_port, "unexpected TLS destination")
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
            b"Set-Cookie: a=1\r\nSet-Cookie: b=2\r\n\r\n"
            b"4\r\ntest\r\n0\r\n\r\n"
        )
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)

    async def task() -> None:
        output = bytearray()
        await HttpsTransport().get(
            f"{API}/releases/latest",
            limit=4,
            deadline=asyncio.get_running_loop().time() + 5,
            sink=output.extend,
        )
        _require(bytes(output) == b"test", "chunked response differs")

    asyncio.run(task())
    _require(len(requested) == 1 and requested[0].startswith(b"GET "), "request was not sent")


def test_tls_redirect_downgrade_is_refused_before_second_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server cannot redirect the installer to an insecure endpoint."""
    calls: list[str] = []

    class Writer:
        """Provide the transport writer interface without network IO."""

        def write(self, _data: bytes) -> None:
            """Accept the request."""

        async def drain(self) -> None:
            """Complete the write."""

        def close(self) -> None:
            """Close the writer."""

        async def wait_closed(self) -> None:
            """Complete the close."""

    async def connect(
        host: str, _port: int, **_kwargs: object
    ) -> tuple[asyncio.StreamReader, Writer]:
        calls.append(host)
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"HTTP/1.1 302 Found\r\nLocation: http://github.com/asset\r\nContent-Length: 0\r\n\r\n"
        )
        reader.feed_eof()
        return reader, Writer()

    monkeypatch.setattr(asyncio, "open_connection", connect)

    async def task() -> None:
        with pytest.raises(DownloadError, match="trusted HTTPS"):
            await HttpsTransport().get(
                f"{API}/releases/latest",
                limit=128,
                deadline=asyncio.get_running_loop().time() + 5,
                sink=lambda _data: None,
            )

    asyncio.run(task())
    _require(calls == ["api.github.com"], "redirect opened another connection")


@pytest.mark.parametrize("kind", ["wrong-ref", "wrong-tag", "missing-sha", "wrong-type"])
def test_untrusted_tag_resolution_refuses(kind: str) -> None:
    """Only the selected tag resolving to an exact Git commit can be trusted."""
    transport = _transport(_metadata(), annotated=True)
    ref_url = f"{API}/git/ref/tags/{TAG}"
    annotated_url = f"{API}/git/tags/{'b' * 40}"
    if kind == "wrong-ref":
        transport.responses[ref_url] = json.dumps(
            {"ref": "refs/tags/v9.9.9", "object": {"type": "commit", "sha": COMMIT}}
        ).encode()
    elif kind == "wrong-tag":
        transport.responses[annotated_url] = json.dumps(
            {"sha": "b" * 40, "tag": "v9.9.9", "object": {"type": "commit", "sha": COMMIT}}
        ).encode()
    elif kind == "missing-sha":
        transport.responses[annotated_url] = json.dumps(
            {"sha": "b" * 40, "tag": TAG, "object": {"type": "commit"}}
        ).encode()
    else:
        transport.responses[ref_url] = json.dumps(
            {"ref": f"refs/tags/{TAG}", "object": {"type": "tree", "sha": COMMIT}}
        ).encode()

    async def task() -> None:
        with pytest.raises(DiscoveryError):
            await discover_release(
                transport,
                TAG,
                max_metadata_bytes=65536,
                network_seconds=5,
            )

    asyncio.run(task())


def _frontend_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, version: str = "0.4.1"
) -> tuple[Request, FakeTransport, bytes]:
    """Create a controlled signed-bundle response and trusted executable doubles."""
    monkeypatch.setattr(fixture, "VERSION", version)
    monkeypatch.setattr(fixture, "ROOT", f"codex-session-gc-{version}-linux-x86_64")
    monkeypatch.setattr(fixture, "WHEEL", f"codex_session_gc-{version}-py3-none-any.whl")
    payload = vars(fixture)["_bundle"]()
    tag = f"v{version}"
    name = f"codex-session-gc-{version}-linux-x86_64.tar.gz"
    url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": name,
                "browser_download_url": url,
                "state": "uploaded",
                "size": len(payload),
                "digest": f"sha256:{digest}",
            }
        ],
    }
    responses = {
        f"{API}/releases/tags/{tag}": json.dumps(metadata).encode(),
        f"{API}/git/ref/tags/{tag}": json.dumps(
            {"ref": f"refs/tags/{tag}", "object": {"type": "commit", "sha": COMMIT}}
        ).encode(),
        url: payload,
    }
    transport = FakeTransport(responses)
    gh, uv = vars(transaction_fixture)["_commands"](tmp_path)
    proof = [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": PREDICATE_TYPE,
                    "subject": [{"digest": {"sha256": digest}}],
                }
            }
        }
    ]
    flags = " ".join(option.value for option in VerificationOption)
    gh.write_text(
        f"#!{sys.executable}\nimport sys, hashlib\n"
        f"proof = {json.dumps(proof)!r}\n"
        f"flags = {flags!r}\n"
        "if '--help' in sys.argv: print(flags)\n"
        f"elif hashlib.sha256(open(sys.argv[3], 'rb').read()).hexdigest() == {digest!r}:\n"
        "    print(proof)\n"
        "else: sys.exit(1)\n"
    )
    gh.chmod(0o700)
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text("preserve = true\n")
    receipts = tmp_path / "runtime-receipts"
    receipts.mkdir(exist_ok=True)
    request = Request(
        tag,
        Action.ACTIVATE,
        InstallLayout(tmp_path / "app"),
        tmp_path / "bin/codex-session-gc",
        None,
        home,
        receipts,
        Tools(gh, uv, Path(sys.executable).resolve()),
        Limits(reserve_bytes=1, environment_bytes=16 * 1024 * 1024, staging_reserve_bytes=1),
    )
    return request, transport, payload


def test_shared_frontend_installs_fake_release_without_config_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A controlled release flows through one descriptor into versioned activation."""
    request, transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    result = asyncio.run(execute(request, transport))
    _require(result["status"] == "active", "release was not activated")
    root = tmp_path / "app/releases/0.4.1"
    _require(
        (tmp_path / "bin/codex-session-gc").resolve() == root / "venv/bin/codex-session-gc",
        "link differs",
    )
    argv = result.get("mcp_argv")
    if not isinstance(argv, tuple) or not argv:
        pytest.fail("MCP executable arguments are unavailable")
    _require(argv[0] == str(root / "venv/bin/codex-session-gc"), "MCP executable is not versioned")
    _require(
        (tmp_path / "codex-home/config.toml").read_text() == "preserve = true\n",
        "Codex config changed",
    )


def test_old_gh_refuses_before_any_release_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verifier lacking the required attestation options performs no network acquisition."""
    request, transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    tools = request.tools
    if tools is None:
        pytest.fail("Synthetic trusted tools are unavailable")
    tools.gh.write_text(f"#!{sys.executable}\nprint('old gh without attestation')\n")
    with pytest.raises(InstallerError, match="attestation"):
        asyncio.run(execute(request, transport))
    _require(not transport.urls, "release metadata was requested before gh preflight")
    _require(not (tmp_path / "app").exists(), "installation directory was created")


def test_manual_link_requires_explicit_expected_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy launcher is untouched until its exact path is supplied."""
    request, transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    old = tmp_path / "app/manual/launcher"
    old.parent.mkdir(parents=True)
    old.write_text("preserve old")
    old.chmod(0o700)
    request.bin_link.parent.mkdir()
    request.bin_link.symlink_to(old)
    with pytest.raises(InstallerError, match="expected-current"):
        asyncio.run(execute(request, transport))
    _require(
        old.read_text() == "preserve old" and request.bin_link.resolve() == old,
        "legacy root changed",
    )
    _require(
        not any("/releases/download/" in url for url in transport.urls),
        "asset downloaded before refusal",
    )
    result = asyncio.run(execute(replace(request, expected_current=old), transport))
    _require(
        result["status"] == "active" and old.read_text() == "preserve old", "explicit update failed"
    )


def test_managed_active_link_can_update_without_manual_adoption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active receipt and unchanged link supply the next update's expected state."""
    first, first_transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    first_result = asyncio.run(execute(first, first_transport))
    old_root = Path(str(first_result["release"]))
    second, second_transport, _ = _frontend_inputs(tmp_path, monkeypatch, version="0.4.2")
    result = asyncio.run(execute(second, second_transport))
    _require(result["status"] == "active" and result["version"] == "0.4.2", "managed update failed")
    _require(old_root.is_dir(), "prior version root was removed")
    _require(
        second.bin_link.resolve() == tmp_path / "app/releases/0.4.2/venv/bin/codex-session-gc",
        "link differs",
    )


def test_explicit_stage_does_not_create_cli_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage-only is an explicit action and does not activate or emit MCP arguments."""
    request, transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    result = asyncio.run(execute(replace(request, action=Action.STAGE), transport))
    _require(result["status"] == "staged" and "mcp_argv" not in result, "stage outcome differs")
    _require(not os.path.lexists(request.bin_link), "stage created a CLI link")


def test_zipapp_is_deterministic_and_uses_shared_installer_source(tmp_path: Path) -> None:
    """Two assembled zipapps match and run the stdlib entry outside the checkout."""
    root = Path(__file__).resolve().parents[1]
    first, second = tmp_path / "one.pyz", tmp_path / "two.pyz"
    first_identity = build_installer(root, first)
    second_identity = build_installer(root, second)
    _require(
        first_identity == second_identity and first.read_bytes() == second.read_bytes(),
        "zipapp differs between builds",
    )
    with zipfile.ZipFile(first) as archive:
        name = "codex_session_gc/installation/__main__.py"
        source = (root / "src" / name).read_bytes()
        _require(archive.read(name) == source, "zipapp copied a different installer entry")
        recorded = json.loads(archive.read("INSTALLER-SOURCE-MAP.json"))
        _require(recorded[name] == hashlib.sha256(source).hexdigest(), "source map differs")

    async def task() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(first),
            "--help",
            cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
            _require(
                process.returncode == 0 and b"--release" in stdout,
                f"isolated zipapp failed: {stderr!r}",
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(task())


def test_frontend_tag_mismatch_refuses_before_creating_version_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed bundle for another version cannot be staged under its own version."""
    request, transport, payload = _frontend_inputs(tmp_path, monkeypatch)
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": NAME,
                "browser_download_url": ASSET,
                "state": "uploaded",
                "size": len(payload),
                "digest": f"sha256:{digest}",
            }
        ],
    }
    transport.responses[f"{API}/releases/tags/{TAG}"] = json.dumps(metadata).encode()
    transport.responses[f"{API}/git/ref/tags/{TAG}"] = json.dumps(
        {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": COMMIT}}
    ).encode()
    transport.responses[ASSET] = payload
    with pytest.raises(PayloadRefusalError, match="expected version"):
        asyncio.run(execute(replace(request, release=TAG), transport))
    _require(not (tmp_path / "app/releases/0.4.1").exists(), "mismatched version root was created")
    _require(not os.path.lexists(request.bin_link), "mismatched version was activated")


@pytest.mark.parametrize("command", ["install", "update"])
def test_installed_cli_uses_same_shared_entry(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Both installed commands forward the exact arguments to the common installer."""
    observed: list[list[str]] = []

    def entry(arguments: list[str]) -> int:
        observed.append(arguments)
        return 0

    monkeypatch.setattr(cli, "installer_main", entry)
    _require(cli.main([command, "--release", TAG, "--action", "stage"]) == 0, "entry failed")
    _require(observed == [["--release", TAG, "--action", "stage"]], "arguments were changed")


@pytest.mark.parametrize(
    ("source", "detail"),
    [
        ("import requests\n", "undeclared module"),
        ("import importlib\n", "Dynamic import machinery"),
        ("from codex_session_gc.installation.missing import value\n", "module is missing"),
    ],
)
def test_zipapp_builder_refuses_unavailable_or_dynamic_sources(
    tmp_path: Path, source: str, detail: str
) -> None:
    """The builder refuses dependencies outside its packaged standard-library closure."""
    package = tmp_path / "src/codex_session_gc"
    (package / "artifacts").mkdir(parents=True)
    (package / "installation").mkdir()
    (package / "__init__.py").write_text('"""Fixture package."""\n')
    (package / "artifacts/__init__.py").write_text('"""Fixture artifacts."""\n')
    (package / "installation/__init__.py").write_text('"""Fixture installer."""\n')
    (package / "installation/__main__.py").write_text(source)
    output = tmp_path / "installer.pyz"
    with pytest.raises(InstallerBuildError, match=detail):
        build_installer(tmp_path, output)
    _require(not output.exists(), "rejected zipapp was published")


@pytest.mark.parametrize("kind", ["io", "interrupt"])
def test_frontend_reports_recoverable_id_after_interrupted_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """A lost activation reply retains a discoverable installation identity."""
    request, transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    original = vars(activation)["_switch"]

    def interrupt(parent_fd: int, temporary: str, name: str) -> None:
        original(parent_fd, temporary, name)
        message = "injected interruption after switch"
        if kind == "interrupt":
            raise KeyboardInterrupt(message)
        raise OSError(message)

    monkeypatch.setattr(activation, "_switch", interrupt)
    with pytest.raises(InstallerError, match="may require reconciliation") as failure:
        asyncio.run(execute(request, transport))
    receipts = list((tmp_path / "app/installations").glob("*.json"))
    _require(len(receipts) == 1, "installation receipt is unavailable")
    identifier = UUID(receipts[0].stem)
    _require(str(identifier) in str(failure.value), "failure omitted the installation ID")
    _require(
        activation_status(request.layout, identifier) is ActivationStatus.ACTIVE,
        "saved link state was not recoverable",
    )
    state_before = receipts[0].read_bytes()
    link_before = request.bin_link.lstat()
    offline = FakeTransport({})
    status_request = replace(
        request,
        action=Action.STATUS,
        installation_id=identifier,
        tools=None,
        home=None,
        receipt_dir=None,
    )
    status = asyncio.run(execute(status_request, offline))
    _require(status["status"] == "active", "status did not observe the active link")
    _require(receipts[0].read_bytes() == state_before, "status modified the receipt")
    _require(request.bin_link.lstat() == link_before, "status modified the link")
    reconciled = asyncio.run(execute(replace(status_request, action=Action.RECONCILE), offline))
    _require(reconciled["status"] == "active", "reconciliation did not settle the observed state")
    _require(request.bin_link.lstat() == link_before, "reconciliation switched the link")
    _require(not offline.urls, "recovery contacted the network")
    monkeypatch.setattr(activation, "_switch", original)
    later, later_transport, _ = _frontend_inputs(tmp_path, monkeypatch, version="0.4.2")
    next_result = asyncio.run(execute(later, later_transport))
    _require(next_result["status"] == "active", "settled recovery blocked the next update")


def test_recovery_cli_needs_no_installer_tools_or_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The status action reads the existing intent without resolving gh, uv or Python."""
    request, transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    result = asyncio.run(execute(request, transport))
    identifier = str(result["installation_id"])
    receipt = tmp_path / "app/installations" / f"{identifier}.json"
    before = receipt.read_bytes()

    def forbidden(_path: Path | None, _name: str) -> Path:
        pytest.fail("Recovery attempted executable discovery")

    monkeypatch.setattr(entry, "_tool", forbidden)
    code = entry.main(
        [
            "--action",
            "status",
            "--installation-id",
            identifier,
            "--install-dir",
            str(tmp_path / "app"),
        ]
    )
    result_text = capsys.readouterr().out
    _require(code == 0 and json.loads(result_text)["status"] == "active", "status CLI failed")
    _require(receipt.read_bytes() == before, "status CLI modified the installation receipt")
    archive = tmp_path / "recovery.pyz"
    build_installer(Path(__file__).resolve().parents[1], archive)

    async def zipapp_status() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(archive),
            "--action",
            "status",
            "--installation-id",
            identifier,
            "--install-dir",
            str(tmp_path / "app"),
            cwd=tmp_path,
            env={"PATH": "", "HOME": str(tmp_path)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
            _require(
                process.returncode == 0 and json.loads(stdout)["status"] == "active",
                f"isolated recovery failed: {stderr!r}",
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(zipapp_status())
    _require(receipt.read_bytes() == before, "zipapp status modified the installation receipt")


def test_zipapp_builder_preserves_existing_output(tmp_path: Path) -> None:
    """An existing unowned archive is never replaced or removed by the builder."""
    output = tmp_path / "installer.pyz"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        build_installer(Path(__file__).resolve().parents[1], output)
    _require(output.read_bytes() == b"existing", "existing installer archive changed")


def test_staged_installation_can_activate_by_id_without_tools_or_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Activate the exact staged receipt, then refuse any redispatch of its intent."""
    request, transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    staged = asyncio.run(execute(replace(request, action=Action.STAGE), transport))
    identifier = UUID(str(staged["installation_id"]))
    record = tmp_path / "app/installations" / f"{identifier}.json"
    before = record.read_bytes()
    offline = FakeTransport({})
    status_request = replace(request, action=Action.STATUS, installation_id=identifier, tools=None)
    status = asyncio.run(execute(status_request, offline))
    _require(status["status"] == "staged", "completed installation was not reported as staged")
    _require(record.read_bytes() == before, "status modified the staged receipt")
    with pytest.raises(ActivationError):
        asyncio.run(execute(replace(status_request, action=Action.RECONCILE), offline))
    _require(record.read_bytes() == before, "reconcile invented an activation intent")

    def forbidden(_path: Path | None, _name: str) -> Path:
        pytest.fail("Saved activation attempted executable discovery")

    monkeypatch.setattr(entry, "_tool", forbidden)
    monkeypatch.setattr(entry, "HttpsTransport", lambda: offline)
    args = [
        "--action",
        "activate",
        "--installation-id",
        str(identifier),
        "--install-dir",
        str(request.layout.base),
        "--bin-dir",
        str(request.bin_link.parent),
        "--home",
        str(request.home),
        "--receipt-dir",
        str(request.receipt_dir),
    ]
    code = entry.main(args)
    output = capsys.readouterr().out
    _require(
        code == 0 and json.loads(output)["installation_id"] == str(identifier),
        "saved installation did not activate",
    )
    _require(not offline.urls, "saved activation contacted the network")
    _require(
        request.bin_link.resolve() == Path(str(staged["release"])) / "venv/bin/codex-session-gc",
        "saved activation targeted a different release",
    )
    active_record = record.read_bytes()
    with pytest.raises(InstallerError, match="cannot be dispatched again"):
        asyncio.run(execute(replace(request, installation_id=identifier, tools=None), offline))
    _require(record.read_bytes() == active_record, "existing activation was redispatched")


def test_failed_preintent_activation_can_retry_same_staged_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent old-link appearance preserves the staged release for an explicit retry."""
    request, transport, _ = _frontend_inputs(tmp_path, monkeypatch)
    old = tmp_path / "app/manual/launcher"

    class AppearingLink(FakeTransport):
        """Publish a disposable old launcher after the initial link observation."""

        async def get(
            self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
        ) -> None:
            """Transfer the controlled response and change only the fixture link."""
            await super().get(url, limit=limit, deadline=deadline, sink=sink)
            if "/releases/download/" in url:
                old.parent.mkdir(parents=True)
                old.write_text("preserve old launcher")
                old.chmod(0o700)
                request.bin_link.symlink_to(old)

    changed = AppearingLink(transport.responses)
    with pytest.raises(InstallerError, match="may require reconciliation") as failure:
        asyncio.run(execute(request, changed))
    receipts = list((tmp_path / "app/installations").glob("*.json"))
    _require(len(receipts) == 1, "staged receipt is unavailable")
    identifier = UUID(receipts[0].stem)
    _require(str(identifier) in str(failure.value), "failure omitted its staged installation ID")
    record = json.loads(receipts[0].read_bytes())
    _require("activation" not in record, "failed precondition unexpectedly recorded an intent")
    offline = FakeTransport({})
    retry = replace(request, installation_id=identifier, expected_current=old, tools=None)
    status = asyncio.run(execute(replace(retry, action=Action.STATUS), offline))
    _require(status["status"] == "staged", "pre-intent failure was not reported as staged")
    result = asyncio.run(execute(retry, offline))
    _require(
        result["status"] == "active" and result["installation_id"] == str(identifier),
        "retry did not activate the same installation",
    )
    _require(old.read_text() == "preserve old launcher", "legacy launcher was modified")
    _require(not offline.urls, "saved retry contacted the network")
