"""The audit window, and the timezone it is expressed in (SPEC.md §6.6).

> A window with no timezone is a day-boundary bug waiting to be found by
> whoever quotes the number.

So a window is never a bare pair of dates. It is parsed in the workspace's
declared timezone, converted to UTC for comparison against records (which are
stored UTC), and carries the zone it was expressed in so the report can state
the requested window, the observed one, and the timezone together.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .errors import ConfigError

SEPARATOR = ".."


@dataclass(frozen=True)
class Window:
    """A half-open interval `[start, end)` in UTC, plus the zone it was written in.

    The end date given by a user is inclusive of that whole day — `--window
    2026-08-01..2026-08-31` means all of August — which is expressed internally
    as an exclusive bound at midnight on 1 September in the declared zone.
    """

    start: datetime
    end: datetime
    timezone: str

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400.0

    def describe(self) -> str:
        zone = ZoneInfo(self.timezone)
        local_start = self.start.astimezone(zone)
        local_end = (self.end - timedelta(microseconds=1)).astimezone(zone)
        return f"{local_start.date().isoformat()}..{local_end.date().isoformat()} ({self.timezone})"


def parse(spec: str, timezone: str) -> Window:
    """Parse `YYYY-MM-DD..YYYY-MM-DD` in the given IANA zone."""
    if SEPARATOR not in spec:
        raise ConfigError(
            f"window {spec!r} must be written as START..END, e.g. 2026-08-01..2026-08-31"
        )
    raw_start, _, raw_end = spec.partition(SEPARATOR)
    zone = ZoneInfo(timezone)

    start_date = _date(raw_start.strip(), spec)
    end_date = _date(raw_end.strip(), spec)
    if end_date < start_date:
        raise ConfigError(f"window {spec!r} ends before it starts")

    start = datetime.combine(start_date, time.min, tzinfo=zone)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone)
    return Window(start=start.astimezone(UTC), end=end.astimezone(UTC), timezone=timezone)


def _date(value: str, spec: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"window {spec!r}: {value!r} is not a YYYY-MM-DD date") from exc
