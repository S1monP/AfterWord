"""Timezone rules for the whole server.

* Storage is UTC, always ``YYYY-MM-DDTHH:MM:SSZ``.
* A timestamp without an offset means local wall-clock time in
  ``AFTERWORD_TIMEZONE`` (default ``Europe/London``): what the device puts in
  ``started_at``, what a browser's ``datetime-local`` / ``date`` input
  sends, and an ICS "floating" time.
* Pages show local time.

Comparing or bucketing by UTC dates puts anything between midnight and
01:00 BST on the previous day, and stepping a weekly series by 7 x 24 h
in UTC moves it an hour when the clocks change, so do both in local time.

Stdlib only (``zoneinfo``); on Windows the ``tzdata`` package supplies
the zone database.
"""

from __future__ import annotations

import datetime as _dt
import os
from zoneinfo import ZoneInfo

TIMEZONE_ENV = "AFTERWORD_TIMEZONE"
DEFAULT_TIMEZONE = "Europe/London"
UTC = _dt.UTC
_FMT = "%Y-%m-%dT%H:%M:%SZ"


def local_tz() -> ZoneInfo:
    """The configured local zone (read each call; ZoneInfo caches)."""
    return ZoneInfo(os.environ.get(TIMEZONE_ENV, "").strip() or DEFAULT_TIMEZONE)


def parse(value: str) -> _dt.datetime:
    """ISO-8601 date or datetime -> aware UTC datetime.

    Naive values (and date-only, as local midnight) are local time.
    Raises ``ValueError`` for anything ``fromisoformat`` rejects.
    """
    dt = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz())
    return dt.astimezone(UTC)


def fmt(dt: _dt.datetime) -> str:
    """Aware datetime -> the storage form (UTC, trailing Z)."""
    return dt.astimezone(UTC).strftime(_FMT)


def to_utc_iso(value: str) -> str:
    """Normalise any accepted timestamp to the storage form."""
    return fmt(parse(value))


def to_local(value: str | None) -> _dt.datetime | None:
    """Stored timestamp -> aware local datetime (``None`` if empty or
    unparseable)."""
    if not value:
        return None
    try:
        return parse(value).astimezone(local_tz())
    except ValueError:
        return None


def local_now() -> _dt.datetime:
    return _dt.datetime.now(local_tz())


def local_today() -> _dt.date:
    return local_now().date()


def local_midnight_utc(day: _dt.date) -> str:
    """Start of a local calendar day, in the storage form."""
    return fmt(_dt.datetime.combine(day, _dt.time(0, 0), tzinfo=local_tz()))
