# Copyright (c) 2026

"""CI acceptance follows real pytest outcomes rather than its usual skip-success exit."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tools.ci_pytest import run

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("def test_case():\n    pass\n", pytest.ExitCode.OK),
        (
            "import pytest\ndef test_case():\n    pytest.skip('not evaluated')\n",
            pytest.ExitCode.TESTS_FAILED,
        ),
        ("def test_case():\n    raise RuntimeError('failed')\n", pytest.ExitCode.TESTS_FAILED),
        ("value = 1\n", pytest.ExitCode.NO_TESTS_COLLECTED),
    ],
)
def test_real_pytest_outcomes_define_partition_acceptance(
    tmp_path: Path,
    body: str,
    expected: pytest.ExitCode,
) -> None:
    """A passing partition, skipped case, failure and empty collection remain distinct."""
    source = tmp_path / f"test_{tmp_path.name}.py"
    source.write_text(body)
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    result = run(
        [str(source), "-c", str(config), "--import-mode=importlib", "-p", "no:cacheprovider"]
    )
    if result != expected:
        pytest.fail(f"Expected exit {expected}, observed {result}")


def test_collection_skip_is_not_hidden_by_another_passing_module(tmp_path: Path) -> None:
    """A module-level skip is incomplete even when another required module passes."""
    skipped = tmp_path / "test_collection_unavailable.py"
    skipped.write_text("import pytest\npytest.skip('unavailable', allow_module_level=True)\n")
    passed = tmp_path / "test_collection_available.py"
    passed.write_text("def test_case():\n    pass\n")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    result = run(
        [
            str(skipped),
            str(passed),
            "-c",
            str(config),
            "--import-mode=importlib",
            "-p",
            "no:cacheprovider",
        ]
    )
    if result != pytest.ExitCode.TESTS_FAILED:
        pytest.fail(f"Skipped required module accepted with exit {result}")
