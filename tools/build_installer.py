# Copyright (c) 2026

"""Build the one standard-library installer zipapp from current project sources."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import stat
import sys
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MAIN = b"from codex_session_gc.installation.__main__ import main\nraise SystemExit(main())\n"
_SOURCE_LIMIT = 16 * 1024 * 1024
_TOTAL_LIMIT = 64 * 1024 * 1024


class InstallerBuildError(Exception):
    """Current installer source cannot be packaged as the declared stdlib closure."""


def _fail(message: str) -> None:
    raise InstallerBuildError(message)


def _dynamic_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return (isinstance(node.func, ast.Name) and node.func.id in ("__import__", "eval", "exec")) or (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "importlib"
        and node.func.attr == "import_module"
    )


def _check_modules(modules: list[str], included: set[str]) -> None:
    for module in modules:
        if module == "importlib" or module.startswith("importlib."):
            _fail("Dynamic import machinery is outside the installer closure.")
        if module == "__future__" or module.split(".", 1)[0] in sys.stdlib_module_names:
            continue
        if module in (
            "codex_session_gc",
            "codex_session_gc.artifacts",
            "codex_session_gc.installation",
        ) or module.startswith(("codex_session_gc.artifacts.", "codex_session_gc.installation.")):
            if module not in included:
                _fail(f"Installer source module is missing: {module}")
            continue
        _fail(f"Installer source imports an undeclared module: {module}")


def _stdlib_closure(path: Path, data: bytes, included: set[str]) -> None:
    try:
        syntax = ast.parse(data, filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as error:
        message = "Installer source is not valid Python."
        raise InstallerBuildError(message) from error
    for node in ast.walk(syntax):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                _fail("Installer sources must use explicit absolute imports.")
            modules = [node.module or ""]
        elif _dynamic_call(node):
            _fail("Dynamic installer imports are outside the packaged closure.")
        _check_modules(modules, included)


def _read_source(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > _SOURCE_LIMIT:
            _fail("Installer sources must be bounded regular files.")
        data = source.read(_SOURCE_LIMIT + 1)
        after = os.fstat(source.fileno())
        if len(data) != info.st_size or (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            _fail("Installer source changed while read.")
        return data


def _sources(root: Path) -> dict[str, bytes]:
    package = root / "src/codex_session_gc"
    paths = [package / "__init__.py"]
    for directory in (package / "artifacts", package / "installation"):
        if not directory.is_dir() or directory.is_symlink():
            _fail("Installer source package is unavailable or aliased.")
        paths.extend(sorted(directory.glob("*.py")))
    required = package / "installation/__main__.py"
    if required not in paths:
        _fail("The shared installer entry point is unavailable.")
    files: dict[str, bytes] = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            _fail("Installer sources must be regular non-symlink files.")
        data = _read_source(path)
        files[path.relative_to(root / "src").as_posix()] = data
    if sum(map(len, files.values())) > _TOTAL_LIMIT:
        _fail("Installer sources exceed their total byte bound.")
    included = {
        name.removesuffix("/__init__.py").replace("/", ".")
        if name.endswith("/__init__.py")
        else name.removesuffix(".py").replace("/", ".")
        for name in files
    }
    for name, data in files.items():
        _stdlib_closure(root / "src" / name, data, included)
    root_module = ast.parse(files["codex_session_gc/__init__.py"])
    if (
        len(root_module.body) != 1
        or not isinstance(root_module.body[0], ast.Expr)
        or not isinstance(root_module.body[0].value, ast.Constant)
        or not isinstance(root_module.body[0].value.value, str)
    ):
        _fail("The zipapp root package must contain only its docstring.")
    return files


def _entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, data)


def _verify_output(path: Path, identity: os.stat_result, expected: str, size: int) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (
            identity.st_dev,
            identity.st_ino,
        ) or before.st_size != size:
            _fail("Published zipapp identity changed during assembly.")
        digest = hashlib.sha256()
        offset = 0
        while offset < size:
            data = os.pread(descriptor, min(1024 * 1024, size - offset), offset)
            if not data:
                _fail("Published zipapp is incomplete.")
            digest.update(data)
            offset += len(data)
        after = os.fstat(descriptor)
        if digest.hexdigest() != expected or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            _fail("Published zipapp changed during verification.")
    finally:
        os.close(descriptor)


def build_installer(root: Path, output: Path) -> tuple[str, int]:
    """Write one absent deterministic zipapp and verify its current source identities."""
    if not root.is_absolute() or not output.is_absolute():
        _fail("Installer source and output paths must be absolute.")
    files = _sources(root)
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    data = io.BytesIO()
    data.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(data, "w") as archive:
        _entry(archive, "__main__.py", _MAIN)
        for name, content in sorted(files.items()):
            _entry(archive, name, content)
        _entry(
            archive,
            "INSTALLER-SOURCE-MAP.json",
            (json.dumps(digests, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )
    for name, expected in digests.items():
        if hashlib.sha256(_read_source(root / "src" / name)).hexdigest() != expected:
            _fail("Installer source changed during assembly.")
    raw = data.getvalue()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o755)
    identity = os.fstat(fd)
    expected = hashlib.sha256(raw).hexdigest()
    try:
        with os.fdopen(fd, "wb") as destination:
            destination.write(raw)
            destination.flush()
            os.fsync(destination.fileno())
        _verify_output(output, identity, expected, len(raw))
    except BaseException:
        try:
            current = output.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                output.unlink()
        except FileNotFoundError:
            pass
        raise
    return expected, len(raw)


def main() -> int:
    """Require an absent output and report its immutable digest and size."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    digest, size = build_installer(_ROOT, arguments.output)
    sys.stdout.write(json.dumps({"sha256": digest, "bytes": size}, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
