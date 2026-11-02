# Copyright (c) 2026

"""Explicit support intake and offline observation through the public CLI."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.cli import main as cli
from codex_session_gc.installation.provenance import PREDICATE_TYPE, REPOSITORY, VerificationOption
from tests.test_support_intake import _fixture

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_explicit_import_then_offline_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verified fixture bytes can be selected offline without claiming fresh authentication."""
    layout, identifier, artifact, identity, source, verifier = _fixture(tmp_path)
    flags = " ".join(option.value for option in VerificationOption)
    proof = [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": PREDICATE_TYPE,
                    "subject": [{"digest": {"sha256": identity.sha256}}],
                }
            }
        }
    ]
    verifier.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"print({flags!r}) if '--help' in sys.argv else print({json.dumps(proof)!r})\n",
    )
    url = f"https://github.com/{REPOSITORY}/releases/download/v1/support.json"
    metadata = {
        "id": 123,
        "state": "uploaded",
        "size": identity.size,
        "digest": f"sha256:{identity.sha256}",
        "browser_download_url": url,
    }

    class Transport:
        async def get(
            self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
        ) -> None:
            if deadline <= 0:
                pytest.fail("Missing transfer deadline")
            raw = (
                json.dumps(metadata).encode() if "api.github.com" in url else artifact.read_bytes()
            )
            if len(raw) > limit:
                pytest.fail("Fixture exceeds transfer bound")
            sink(raw)

    monkeypatch.setattr(cli, "HttpsTransport", Transport)
    result = cli.main(
        [
            "support",
            "import",
            "--installation-id",
            str(identifier),
            "--install-dir",
            str(layout.base),
            "--asset-id",
            "123",
            "--sha256",
            identity.sha256,
            "--size",
            str(identity.size),
            "--gc-commit",
            source.commit,
            "--gh",
            str(verifier),
        ]
    )
    if result != 0:
        pytest.fail(capsys.readouterr().err)
    imported = json.loads(capsys.readouterr().out)["recorded_verification"]
    if (
        imported["origin"] != "verification_recorded_not_reauthenticated"
        or imported["claim_verified"]
    ):
        pytest.fail("Recorded origin was misrepresented")

    def no_network() -> None:
        pytest.fail("Offline observation constructed a network transport")

    monkeypatch.setattr(cli, "HttpsTransport", no_network)
    result = cli.main(
        [
            "support",
            "show",
            "--installation-id",
            str(identifier),
            "--install-dir",
            str(layout.base),
            "--sha256",
            identity.sha256,
            "--context-sha256",
            imported["context_sha256"],
        ]
    )
    if result != 0:
        pytest.fail(capsys.readouterr().err)
    shown = json.loads(capsys.readouterr().out)
    if (
        shown["recorded_verification"]["origin"] != "verification_recorded_not_reauthenticated"
        or shown["source_support"]["origin"] != "caller_supplied_unverified"
        or shown["source_support"]["claim_verified"]
        or shown["source_support"]["authorizes_operation"]
        or shown["source_support"]["bindings"]["state"] != "unknown"
    ):
        pytest.fail("Offline support observation overclaimed its authority")


def test_invalid_source_refuses_before_any_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed explicit source identity does not trigger an asset request."""
    layout, identifier, _, identity, _, verifier = _fixture(tmp_path)

    def no_network() -> None:
        pytest.fail("Invalid input constructed a network transport")

    monkeypatch.setattr(cli, "HttpsTransport", no_network)
    result = cli.main(
        [
            "support",
            "import",
            "--installation-id",
            str(identifier),
            "--install-dir",
            str(layout.base),
            "--asset-id",
            "123",
            "--sha256",
            identity.sha256,
            "--size",
            str(identity.size),
            "--gc-commit",
            "partial",
            "--gh",
            str(verifier),
        ]
    )
    if result != 1 or "refused" not in capsys.readouterr().err:
        pytest.fail("Invalid source was not refused")


def test_unavailable_verifier_refuses_before_asset_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A verifier without the required options is a typed CLI refusal."""
    layout, identifier, _, identity, source, verifier = _fixture(tmp_path)
    verifier.write_text(f"#!{sys.executable}\nprint('unsupported')\n")

    class Transport:
        async def get(
            self, url: str, *, limit: int, deadline: float, sink: Callable[[bytes], None]
        ) -> None:
            pytest.fail(f"Unavailable verifier allowed {url=} {limit=} {deadline=} {sink=}")

    monkeypatch.setattr(cli, "HttpsTransport", Transport)
    result = cli.main(
        [
            "support",
            "import",
            "--installation-id",
            str(identifier),
            "--install-dir",
            str(layout.base),
            "--asset-id",
            "123",
            "--sha256",
            identity.sha256,
            "--size",
            str(identity.size),
            "--gc-commit",
            source.commit,
            "--gh",
            str(verifier),
        ]
    )
    if result != 1 or "refused" not in capsys.readouterr().err:
        pytest.fail("Unavailable verifier was not refused")
