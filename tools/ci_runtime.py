# Copyright (c) 2026

"""Record the actual hosted execution environment without asserting artifact qualification."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

from tools.ci_native import digest


def main() -> None:
    """Bind observed runtime/tool versions to the checkout commit and dependency lock."""
    evidence = {
        "source_commit": os.environ["GC_CI_SOURCE_COMMIT"],
        "uv_version": os.environ["GC_CI_UV_ACTUAL"],
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": sys.executable,
        "libc": platform.libc_ver(),
        "machine": platform.machine(),
        "kernel": platform.release(),
        "os_release": platform.freedesktop_os_release(),
        "runner_os": os.environ.get("RUNNER_OS"),
        "runner_arch": os.environ.get("RUNNER_ARCH"),
        "hosted_image_os": os.environ.get("GC_CI_IMAGE_OS"),
        "hosted_image_version": os.environ.get("GC_CI_IMAGE_VERSION"),
        "uv_lock_sha256": digest(Path("uv.lock")),
    }
    sys.stdout.write(json.dumps(evidence, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
