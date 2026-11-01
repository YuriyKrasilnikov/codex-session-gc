# Copyright (c) 2026

"""Exercise bounded installer behavior using isolated synthetic artifacts."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import struct
import sys
import tarfile
import zipfile
from typing import TYPE_CHECKING

import pytest

from codex_session_gc.artifacts import ArtifactIdentity, SourceIdentity
from codex_session_gc.artifacts.bundle import RELEASE_FORMAT
from codex_session_gc.artifacts.ustar import UstarError, UstarReader
from codex_session_gc.installation.payload import PayloadRefusalError, extract_payload
from codex_session_gc.installation.provenance import PREDICATE_TYPE, verify_artifact

if TYPE_CHECKING:
    from pathlib import Path


VERSION = "0.4.1"
ROOT = f"codex-session-gc-{VERSION}-linux-x86_64"
WHEEL = f"codex_session_gc-{VERSION}-py3-none-any.whl"


def _wheel_bytes(wheel: io.BytesIO, change: str) -> bytes:
    """Return a wheel or one with an invalid directory."""
    result = wheel.getvalue()
    if change == "wheel-directory":
        altered = bytearray(result)
        struct.pack_into("<HH", altered, len(altered) - 14, 10001, 10001)
        result = bytes(altered)
    elif change == "zip64-sentinel":
        altered = bytearray(result)
        struct.pack_into("<HH", altered, len(altered) - 14, 0xFFFF, 0xFFFF)
        result = bytes(altered)
    elif change in ("zip64-locator", "zip64-max-comment"):
        classic = bytearray(result[-22:])
        offset = len(result) - len(classic)
        record = struct.pack("<4sQ2H2I4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 2**32, 2**32, 2**40, 0)
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, offset, 1)
        comment = b""
        if change == "zip64-max-comment":
            comment = b"x" * 65535
            struct.pack_into("<H", classic, 20, len(comment))
        result = result[:-22] + record + locator + bytes(classic) + comment
    return result


def _pax_archive(result: bytes, change: str) -> bytes:
    """Optionally insert an oversized extension header."""
    if change != "pax":
        return result
    stream = gzip.decompress(result)
    extension = tarfile.TarInfo("extended")
    extension.type = tarfile.XHDTYPE
    extension.size = 2**30
    return gzip.compress(stream[:512] + extension.tobuf(format=tarfile.USTAR_FORMAT))


def _add_bad_member(archive: tarfile.TarFile, change: str) -> None:
    """Insert the selected unsafe archive member."""
    if change in ("traversal", "absolute", "symlink", "hardlink", "other-root"):
        names = {
            "traversal": f"{ROOT}/../../outside",
            "absolute": "/outside",
            "symlink": f"{ROOT}/link",
            "hardlink": f"{ROOT}/link",
            "other-root": "other/file",
        }
        bad = tarfile.TarInfo(names[change])
        if change in ("symlink", "hardlink"):
            bad.type = tarfile.SYMTYPE if change == "symlink" else tarfile.LNKTYPE
            bad.linkname = "../../outside"
        archive.addfile(bad, io.BytesIO(b""))


def _bundle(change: str = "") -> bytes:
    """Construct a synthetic manifest-first release archive."""
    helper = b"test-native-helper"
    contract = json.dumps(
        {
            "binary_sha256": list(hashlib.sha256(helper).digest()),
            "source_identity": "qualified",
            "wire_version": 6,
        }
    ).encode()
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"codex_session_gc-{VERSION}.dist-info/METADATA",
            f"Name: codex-session-gc\nVersion: {VERSION}\nRequires-Python: >=3.14\n",
        )
        archive.writestr("codex_session_gc/execution/closed_work_contract.json", contract)
    wheel_bytes = _wheel_bytes(wheel, change)
    digest = hashlib.sha256(wheel_bytes).hexdigest()
    provenance = {"upstream": "source"}
    files = {
        WHEEL: wheel_bytes,
        "codex-session-gc-native": helper,
        "native/build-inputs/provenance.json": json.dumps(provenance).encode(),
        "requirements-runtime.txt": ("pydantic==2.0 --hash=sha256:" + "a" * 64 + "\n").encode(),
        "install-requirements.txt": (
            f"-r requirements-runtime.txt\n./{WHEEL} --hash=sha256:{digest}\n".encode()
        ),
    }
    for name in (
        "README.md",
        "docs/contract.md",
        "docs/recovery.md",
        "docs/architecture.md",
        "docs/compatibility.md",
        "native/README.md",
        "LICENSE",
        "NOTICE",
        "third_party/NATIVE_NOTICES.txt",
        "third_party/RUST_LIBRARY_NOTICES.html",
        "third_party/NOTICE_SOURCES.json",
    ):
        files[name] = b"synthetic notice or document"
    hashes = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        for name, data in files.items()
    }
    manifest = {
        "format": RELEASE_FORMAT,
        "package_version": VERSION,
        "requires_python": ">=3.14",
        "native_source_identity": "qualified",
        "wire_version": 6,
        "wheel_contract_sha256": hashlib.sha256(contract).hexdigest(),
        "wheel_source_map_sha256": "b" * 64,
        "source_provenance": provenance,
        "payload": hashes,
    }
    if change == "helper":
        files["codex-session-gc-native"] = b"incorrect"
        hashes["codex-session-gc-native"] = {
            "sha256": hashlib.sha256(b"incorrect").hexdigest(),
            "bytes": 9,
        }
    if change == "control-size":
        hashes["requirements-runtime.txt"]["bytes"] = 1024 * 1024 + 1
    if change == "manifest":
        manifest["package_version"] = "other"
    if change == "v1":
        manifest["format"] = "codex-session-gc-release-v1"
    if change == "missing":
        del files["requirements-runtime.txt"]
    files["manifest.json"] = json.dumps(manifest).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        root = tarfile.TarInfo(ROOT)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        manifest_data = files.pop("manifest.json")
        member = tarfile.TarInfo(f"{ROOT}/manifest.json")
        member.size = len(manifest_data)
        archive.addfile(member, io.BytesIO(manifest_data))
        _add_bad_member(archive, change)
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(f"{ROOT}/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        if change == "duplicate":
            member = tarfile.TarInfo(f"{ROOT}/manifest.json")
            archive.addfile(member, io.BytesIO(b""))
    return _pax_archive(output.getvalue(), change)


def _consume(
    tmp_path: Path,
    change: str = "",
    *,
    reserve_bytes: int = 0,
    bundle_data: bytes | None = None,
    expected_version: str = VERSION,
) -> Path:
    """Verify and consume one synthetic release archive."""
    data = _bundle(change) if bundle_data is None else bundle_data
    archive = tmp_path / "archive"
    archive.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
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
    verifier = tmp_path / "gh"
    verifier.write_text(f"#!{sys.executable}\nprint({json.dumps(proof)!r})\n")
    verifier.chmod(0o700)
    source = SourceIdentity("a" * 40, "refs/tags/test")
    target = tmp_path / "versions" / VERSION

    def create(version: str) -> Path:
        """Create an exclusive final version root."""
        if version != VERSION:
            pytest.fail("Unexpected: version != VERSION")
        target.parent.mkdir(exist_ok=True)
        target.mkdir()
        return target

    async def run() -> None:
        """Run an isolated installation attempt."""
        with await verify_artifact(
            archive, ArtifactIdentity(digest, len(data)), source, trusted_gh=verifier
        ) as lease:
            extracted = extract_payload(
                lease, create, reserve_bytes=reserve_bytes, expected_version=expected_version
            )
            if extracted.version != VERSION:
                pytest.fail("Unexpected: extracted.version != VERSION")

    asyncio.run(run())
    return target


def test_valid_descriptor_stream(tmp_path: Path) -> None:
    """Consume a verified archive from its original descriptor."""
    root = _consume(tmp_path)
    if not ((root / WHEEL).is_file()):
        pytest.fail("Unexpected: not (root / WHEEL).is_file()")
    if (root / "codex-session-gc-native").read_bytes() != b"test-native-helper":
        pytest.fail("Unexpected: (root / 'codex-session-gc-native').read_bytes...")


@pytest.mark.parametrize(
    "change",
    [
        "traversal",
        "absolute",
        "symlink",
        "hardlink",
        "other-root",
        "duplicate",
        "manifest",
        "missing",
        "helper",
        "v1",
        "control-size",
        "wheel-directory",
        "zip64-sentinel",
    ],
)
def test_rejects_bad_archive_before_escaping_owned_root(tmp_path: Path, change: str) -> None:
    """Rejects bad archive before escaping owned root."""
    with pytest.raises(PayloadRefusalError):
        _consume(tmp_path, change)
    if (tmp_path / "outside").exists():
        pytest.fail("Unexpected: (tmp_path / 'outside').exists()")


def test_capacity_refusal_precedes_bulk_payload_write(tmp_path: Path) -> None:
    """Capacity refusal precedes bulk payload write."""
    with pytest.raises(PayloadRefusalError, match="Insufficient installation capacity"):
        _consume(tmp_path, reserve_bytes=10**30)
    root = tmp_path / "versions" / VERSION
    if not (root.is_dir()):
        pytest.fail("Unexpected: not root.is_dir()")
    if tuple(root.iterdir()):
        pytest.fail("Unexpected: tuple(root.iterdir())")


def test_oversized_extension_refused_before_extension_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversized extension refused before extension body."""
    original = gzip.GzipFile.read
    requested: list[int] = []

    def measured(stream: gzip.GzipFile, size: int = -1) -> bytes:
        """Record requested decompressed bytes."""
        requested.append(size)
        return original(stream, size)

    monkeypatch.setattr(gzip.GzipFile, "read", measured)
    with pytest.raises(PayloadRefusalError):
        _consume(tmp_path, "pax")
    max_header_bytes = 2 * 512
    if not (sum(requested) <= max_header_bytes):
        pytest.fail("Unexpected: not sum(requested) <= max_header_bytes")
    if (tmp_path / "versions" / VERSION).exists():
        pytest.fail("Unexpected: (tmp_path / 'versions' / VERSION).exists()")


@pytest.mark.parametrize("variant", ["zip64-locator", "zip64-max-comment"])
def test_zip64_locator_refused_before_zipfile_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """Reject oversized ZIP64 bounds even when classic trailer fields are small."""
    data = _bundle(variant)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("ZipFile was constructed before rejecting the ZIP64 locator")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(PayloadRefusalError, match="ZIP64 wheels are unsupported"):
        _consume(tmp_path, bundle_data=data)


def test_shared_ustar_reader_rejects_unbounded_member_read() -> None:
    """A negative read cannot consume an unbounded compressed member."""
    with gzip.GzipFile(fileobj=io.BytesIO(_bundle()), mode="rb") as stream:
        reader = UstarReader(stream)
        reader.next()
        reader.next()
        with pytest.raises(UstarError, match="nonnegative"):
            reader.read(-1)
        if reader.read(1) != b"{":
            pytest.fail("Rejected read consumed the manifest")


def test_shared_ustar_reports_bad_checksum_as_ustar_error() -> None:
    """A malformed physical header fails through the shared typed error boundary."""
    physical = bytearray(gzip.decompress(_bundle()))
    physical[0] ^= 1
    with gzip.GzipFile(fileobj=io.BytesIO(gzip.compress(bytes(physical))), mode="rb") as stream:
        reader = UstarReader(stream)
        with pytest.raises(UstarError, match="header is invalid"):
            reader.next()


def test_expected_version_mismatch_refuses_before_root_creation(tmp_path: Path) -> None:
    """A verified bundle for another tag cannot create any version root."""
    with pytest.raises(PayloadRefusalError, match="expected version"):
        _consume(tmp_path, expected_version="9.9.9")
    if (tmp_path / "versions").exists():
        pytest.fail("A mismatched release created a version root")
