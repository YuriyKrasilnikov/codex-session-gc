# Copyright (c) 2026

"""UTC cutoff validation and strict activity comparison."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class CutoffError(ValueError):
    """Base class for cutoff and activity validation failures."""


class InvalidCutoffError(CutoffError):
    """Raised when a cutoff is not a datetime value."""


class NaiveCutoffError(InvalidCutoffError):
    """Raised when a cutoff has no timezone information."""


class NonUtcCutoffError(InvalidCutoffError):
    """Raised when a timezone-aware cutoff is not explicitly UTC."""


class UnknownActivityError(CutoffError):
    """Raised when activity is unavailable."""


class InvalidActivityError(CutoffError):
    """Raised when activity is not a datetime value."""


class NaiveActivityError(InvalidActivityError):
    """Raised when activity has no timezone information."""


@dataclass(frozen=True, slots=True)
class UtcCutoff:
    """An immutable explicit UTC boundary for strict activity comparison."""

    timestamp: datetime

    def __post_init__(self) -> None:
        """Validate that the boundary is an explicit UTC instant."""
        object.__setattr__(self, "timestamp", _canonicalize_utc_cutoff(self.timestamp))

    def is_strictly_before(self, activity: object) -> bool:
        """Return whether known timezone-aware activity precedes this cutoff."""
        return _require_aware_activity(activity) < self.timestamp


def _canonicalize_utc_cutoff(value: object) -> datetime:
    if not isinstance(value, datetime):
        msg = "A cutoff must be a datetime."
        raise InvalidCutoffError(msg)
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "A cutoff must include explicit UTC timezone information."
        raise NaiveCutoffError(msg)
    if value.utcoffset() != timedelta():
        msg = "A cutoff must use UTC."
        raise NonUtcCutoffError(msg)
    return value.astimezone(UTC)


def _require_aware_activity(value: object) -> datetime:
    if value is None:
        msg = "Activity is unknown."
        raise UnknownActivityError(msg)
    if not isinstance(value, datetime):
        msg = "Activity must be a datetime."
        raise InvalidActivityError(msg)
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "Activity must include timezone information."
        raise NaiveActivityError(msg)
    return value
