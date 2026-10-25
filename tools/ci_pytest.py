# Copyright (c) 2026

"""Run a declared CI test partition without treating skipped cases as acceptance."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence


class RequireEvaluatedTests:
    """Preserve pytest results and make incomplete evaluation fail the CI partition."""

    def __init__(self) -> None:
        """Start an independent result observer for one pytest invocation."""
        self.skipped = 0

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Count actual skip outcomes, including expected-failure skips."""
        if report.skipped:
            self.skipped += 1

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """Include modules skipped during collection in the incomplete result."""
        if report.skipped:
            self.skipped += 1

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        """A skipped required case cannot turn the declared test partition green."""
        if self.skipped and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def run(arguments: Sequence[str]) -> int:
    """Execute the supplied test partition with pytest's ordinary collection and failures."""
    return int(pytest.main(list(arguments), plugins=[RequireEvaluatedTests()]))


def main() -> None:
    """Use explicit test paths/inputs from the workflow, never infer a fallback partition."""
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
