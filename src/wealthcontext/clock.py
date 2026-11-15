"""One injectable UTC clock; business-date conversion belongs at request entry."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def utc_timestamp(instant: datetime) -> str:
    """Fixed-width UTC text keeps SQLite ordering identical to instant ordering."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("stored instants must have a timezone")
    try:
        normalized = instant.astimezone(UTC)
    except OverflowError as error:
        raise ValueError("UTC instant is outside years 1 through 9999") from error
    return normalized.isoformat(timespec="microseconds")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class FrozenClock:
    instant: datetime

    def __post_init__(self) -> None:
        if self.instant.tzinfo is None or self.instant.utcoffset() is None:
            raise ValueError("clock instants must have a timezone")

    def now(self) -> datetime:
        return self.instant.astimezone(UTC)
