# Copyright (c) 2026

"""Acceptance tests for strict UTC cutoff comparison."""

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Final

import pytest

from codex_session_gc.domain import (
    InvalidActivityError,
    NaiveActivityError,
    NaiveCutoffError,
    NonUtcCutoffError,
    UnknownActivityError,
    UtcCutoff,
)

UTC_CUTOFF: Final = datetime(2030, 1, 2, 12, tzinfo=UTC)
ONE_MINUTE: Final = timedelta(minutes=1)
TWO_HOURS: Final = timedelta(hours=2)
PLUS_ONE_HOUR: Final = timezone(timedelta(hours=1))
ZERO_OFFSET_GMT: Final = timezone(timedelta(), name="GMT")


def test_requires_strictly_earlier_activity() -> None:
    """Treat equality as retained rather than older."""
    cutoff = UtcCutoff(UTC_CUTOFF)

    _expect_true(result=cutoff.is_strictly_before(UTC_CUTOFF - ONE_MINUTE))
    _expect_false(result=cutoff.is_strictly_before(UTC_CUTOFF))
    _expect_false(result=cutoff.is_strictly_before(UTC_CUTOFF + ONE_MINUTE))


def test_rejects_naive_or_non_utc_cutoffs() -> None:
    """Require an explicit UTC boundary."""
    naive_cutoff = UTC_CUTOFF.replace(tzinfo=None)
    non_utc_cutoff = datetime(2030, 1, 2, 13, tzinfo=PLUS_ONE_HOUR)

    with pytest.raises(NaiveCutoffError):
        UtcCutoff(naive_cutoff)
    with pytest.raises(NonUtcCutoffError):
        UtcCutoff(non_utc_cutoff)


def test_rejects_unknown_or_invalid_activity() -> None:
    """Refuse activity values that cannot establish an instant."""
    cutoff = UtcCutoff(UTC_CUTOFF)

    with pytest.raises(UnknownActivityError):
        cutoff.is_strictly_before(None)
    with pytest.raises(InvalidActivityError):
        cutoff.is_strictly_before("2030-01-02T12:00:00Z")
    with pytest.raises(NaiveActivityError):
        cutoff.is_strictly_before(UTC_CUTOFF.replace(tzinfo=None))


def test_compares_equivalent_offset_aware_instants() -> None:
    """Compare activity by instant rather than UTC offset spelling."""
    cutoff = UtcCutoff(UTC_CUTOFF)
    earlier_offset_activity = datetime(2030, 1, 2, 12, 59, tzinfo=PLUS_ONE_HOUR)
    equal_offset_activity = datetime(2030, 1, 2, 13, tzinfo=PLUS_ONE_HOUR)

    _expect_true(result=cutoff.is_strictly_before(earlier_offset_activity))
    _expect_false(result=cutoff.is_strictly_before(equal_offset_activity))


def test_accepts_equivalent_fixed_zero_offset_cutoff() -> None:
    """Accept a named zero-offset cutoff as the same UTC instant."""
    alternate_cutoff = UTC_CUTOFF.replace(tzinfo=ZERO_OFFSET_GMT)
    cutoff = UtcCutoff(alternate_cutoff)

    _expect_datetime_same(actual=cutoff.timestamp, expected=UTC_CUTOFF)
    _expect_true(result=cutoff.is_strictly_before(UTC_CUTOFF - ONE_MINUTE))


def test_detaches_cutoff_from_mutable_input_timezone() -> None:
    """Keep a constructed cutoff stable after caller timezone mutation."""
    mutable_timezone = MutableUtcTimezone()
    input_cutoff = UTC_CUTOFF.replace(tzinfo=mutable_timezone)
    cutoff = UtcCutoff(input_cutoff)
    activity = UTC_CUTOFF - ONE_MINUTE

    mutable_timezone.set_offset(PLUS_ONE_HOUR.utcoffset(None))

    _expect_datetime_same(actual=cutoff.timestamp, expected=UTC_CUTOFF)
    _expect_true(result=cutoff.is_strictly_before(activity))


def test_preserves_order_when_both_instants_are_translated() -> None:
    """Keep strict ordering invariant under a shared time translation."""
    activity = UTC_CUTOFF - ONE_MINUTE
    cutoff = UtcCutoff(UTC_CUTOFF)
    translated_cutoff = UtcCutoff(UTC_CUTOFF + TWO_HOURS)

    _expect_same(
        actual=cutoff.is_strictly_before(activity),
        expected=translated_cutoff.is_strictly_before(activity + TWO_HOURS),
    )


def _expect_true(*, result: bool) -> None:
    """Fail when a result is not true."""
    if not result:
        pytest.fail("Expected true.")


def _expect_false(*, result: bool) -> None:
    """Fail when a result is not false."""
    if result:
        pytest.fail("Expected false.")


def _expect_same(*, actual: bool, expected: bool) -> None:
    """Fail when two boolean results differ."""
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, got {actual!r}.")


def _expect_datetime_same(*, actual: datetime, expected: datetime) -> None:
    """Fail when two observable datetime instants differ."""
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, got {actual!r}.")


class MutableUtcTimezone(tzinfo):
    """A caller-owned timezone whose offset can change after construction."""

    def __init__(self) -> None:
        """Start at UTC offset."""
        self._offset = timedelta()

    def dst(self, _datetime: datetime | None) -> timedelta:
        """Return no daylight-saving adjustment."""
        return timedelta()

    def set_offset(self, offset: timedelta) -> None:
        """Change the caller-owned offset."""
        self._offset = offset

    def tzname(self, _datetime: datetime | None) -> str:
        """Return a stable human-readable timezone name."""
        return "UTC"

    def utcoffset(self, _datetime: datetime | None) -> timedelta:
        """Return the current caller-owned offset."""
        return self._offset
