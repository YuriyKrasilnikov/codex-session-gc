# Copyright (c) 2026

"""Reuse one qualified helper only with its exact native inputs and raw profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tarfile
import zipfile
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import IO

PROFILE = Path("src/codex_session_gc/execution/closed_work_contract.json")
PROVENANCE = Path("native/build-inputs/provenance.json")
WHEEL_PROFILE = "codex_session_gc/execution/closed_work_contract.json"


class ReuseError(ValueError):
    """Changed inputs require separate native qualification, never a substituted digest."""


@dataclass(frozen=True)
class QualifiedRelease:
    """Reviewed release anchors; runtime CLI inputs cannot replace these identities."""

    url: str
    bundle_sha256: str
    bundle_bytes: int
    helper_sha256: str
    profile_sha256: str
    native_inputs_sha256: str
    wheel_sha256: str
    directory: str
    wheel: str


QUALIFIED = QualifiedRelease(
    "https://github.com/YuriyKrasilnikov/codex-session-gc/releases/download/v0.2.0/"
    "codex-session-gc-0.2.0-linux-x86_64.tar.gz",
    "508078b9a0c1606194ed42f4498095bbc8941dc7cad464dd1eff58a17fb6a286",
    16_236_006,
    "6ca072bf606733acd180031a34a55c9c6581c4b749dd8374a7a12c81d0d4efa5",
    "890ad434de974c6848ab27ac7b934b83d96ea1d437551ce774ef281a8b2e0026",
    "79faa67e13f740b7d889c028ee296fea36ccf605962582caba04e198312530d9",
    "36224e865594a7f49a1978e3ea7ee6acbc8a530e1bea5fa28d4f0638f8466a2f",
    "codex-session-gc-0.2.0-linux-x86_64",
    "codex_session_gc-0.2.0-py3-none-any.whl",
)


def _stream_digest(stream: IO[bytes]) -> str:
    result = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        result.update(block)
    return result.hexdigest()


def digest(path: Path) -> str:
    """Hash complete file bytes without materializing the executable."""
    with path.open("rb") as stream:
        return _stream_digest(stream)


def _require(message: str, *, condition: bool) -> None:
    if not condition:
        raise ReuseError(message)


def _native_paths(root: Path) -> set[str]:
    helper = root / "native/helper"
    paths = {helper / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")}
    paths.update(path for path in (helper / "src").rglob("*") if path.is_file())
    paths.update((root / "native/patches").glob("*.patch"))
    if (helper / "build.rs").exists():
        paths.add(helper / "build.rs")
    paths.update(path for path in (helper / ".cargo").rglob("*") if path.is_file())
    return {path.relative_to(root).as_posix() for path in paths}


def verify_source(root: Path, qualified: QualifiedRelease = QUALIFIED) -> dict[str, str | int]:
    """Refuse native/profile deltas before downloading or selecting any helper."""
    root = root.resolve()
    _require(
        "Raw profile changed; separate qualification and receipt-consumer transition required.",
        condition=digest(root / PROFILE) == qualified.profile_sha256,
    )
    profile = json.loads((root / PROFILE).read_bytes())
    _require(
        "Profile and qualified helper anchors disagree.",
        condition=profile["binary_sha256"] == list(bytes.fromhex(qualified.helper_sha256)),
    )
    provenance = json.loads((root / PROVENANCE).read_bytes())
    current = provenance["current_delivery_inputs"]
    manifest = current["manifest"]
    encoded = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    actual = hashlib.sha256(encoded).hexdigest()
    _require(
        "Native input manifest changed; qualified-helper reuse is refused.",
        condition=actual == current["manifest_sha256"] == qualified.native_inputs_sha256,
    )
    entries = manifest["inputs"]
    declared = {entry["path"] for entry in entries}
    _require(
        "Native input file set changed or contains duplicates.",
        condition=len(declared) == len(entries) and declared == _native_paths(root),
    )
    for entry in entries:
        path = root / entry["path"]
        _require(
            f"Native input is not a regular source file: {entry['path']}",
            condition=not path.is_symlink() and path.resolve().is_relative_to(root),
        )
        _require(
            f"Native input differs from qualified bytes: {entry['path']}",
            condition=path.is_file()
            and path.stat().st_size == entry["bytes"]
            and digest(path) == entry["sha256"],
        )
    return {
        "profile_sha256": qualified.profile_sha256,
        "native_inputs_sha256": actual,
        "native_input_files": len(entries),
        "helper_sha256": qualified.helper_sha256,
    }


def verify_bundle(bundle: Path, qualified: QualifiedRelease = QUALIFIED) -> None:
    """Verify full transport bytes and the helper/profile pair inside the pinned release."""
    _require(
        "Qualified bundle byte size differs.",
        condition=bundle.stat().st_size == qualified.bundle_bytes,
    )
    with bundle.open("rb") as stream:
        _require(
            "Downloaded bundle differs from the pinned release hash.",
            condition=_stream_digest(stream) == qualified.bundle_sha256,
        )
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:gz") as archive:
            names = archive.getnames()
            _require("Duplicate qualified bundle member.", condition=len(names) == len(set(names)))
            helper = archive.getmember(f"{qualified.directory}/codex-session-gc-native")
            wheel = archive.getmember(f"{qualified.directory}/{qualified.wheel}")
            _require(
                "Expected regular release artifacts.", condition=helper.isfile() and wheel.isfile()
            )
            helper_stream = archive.extractfile(helper)
            wheel_stream = archive.extractfile(wheel)
            if helper_stream is None or wheel_stream is None:
                message = "Qualified release members are unavailable."
                raise ReuseError(message)
            with helper_stream, wheel_stream:
                _require(
                    "Qualified bundle helper hash differs.",
                    condition=_stream_digest(helper_stream) == qualified.helper_sha256,
                )
                wheel_bytes = wheel_stream.read()
            _require(
                "Qualified bundle wheel hash differs.",
                condition=hashlib.sha256(wheel_bytes).hexdigest() == qualified.wheel_sha256,
            )
            with zipfile.ZipFile(BytesIO(wheel_bytes)) as wheel_archive:
                raw = wheel_archive.read(WHEEL_PROFILE)
            _require(
                "Qualified bundle raw profile hash differs.",
                condition=hashlib.sha256(raw).hexdigest() == qualified.profile_sha256,
            )


def select_helper(
    root: Path,
    bundle: Path,
    helper: Path,
    qualified: QualifiedRelease = QUALIFIED,
) -> dict[str, str | int]:
    """Validate an explicitly downloaded bundle and copy only its qualified helper."""
    evidence = verify_source(root, qualified)
    _require("Use a new helper output path.", condition=not helper.exists())
    verify_bundle(bundle, qualified)
    with tarfile.open(bundle, "r:gz") as archive:
        source = archive.extractfile(f"{qualified.directory}/codex-session-gc-native")
        if source is None:
            message = "Qualified helper is unavailable."
            raise ReuseError(message)
        with source, helper.open("xb") as target:
            shutil.copyfileobj(source, target)
    _require("Copied helper hash differs.", condition=digest(helper) == qualified.helper_sha256)
    helper.chmod(0o755)
    return {
        **evidence,
        "release_bundle_sha256": qualified.bundle_sha256,
        "qualified_wheel_sha256": qualified.wheel_sha256,
    }


def main() -> None:
    """Expose source checking and explicit acquisition without identity override flags."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--helper", type=Path)
    args = parser.parse_args()
    if args.describe:
        sys.stdout.write(json.dumps(asdict(QUALIFIED), sort_keys=True) + "\n")
        return
    if (args.bundle is None) != (args.helper is None):
        parser.error("--bundle and --helper must be supplied together")
    try:
        evidence = (
            select_helper(args.root, args.bundle, args.helper)
            if args.bundle is not None
            else verify_source(args.root)
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as error:
        parser.exit(1, f"Qualified-helper reuse refused: {error}\n")
    sys.stdout.write(json.dumps(evidence, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
