"""UTC/local-time handling. Dates are kept explicit so statistics are auditable."""
from __future__ import annotations

from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from typing import Any


class TimezoneError(ValueError):
    pass


def get_zone(name: str | None) -> ZoneInfo | timezone:
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception as exc:  # Python does not guarantee tzdata package is installed
        raise TimezoneError(f"Unsupported IANA timezone: {name}") from exc


def parse_dt(value: str | None, fallback_tz: str | None = None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # GPX can contain fractional seconds with unusual precision in controlled fixtures.
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_zone(fallback_tz))
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def local_date(dt: datetime, tz_name: str | None) -> date:
    return dt.astimezone(get_zone(tz_name)).date()


def local_datetime(dt: datetime, tz_name: str | None) -> datetime:
    return dt.astimezone(get_zone(tz_name))


def seconds_between(a: datetime | None, b: datetime | None) -> int | None:
    if not a or not b:
        return None
    return max(0, int((b - a).total_seconds()))


def ensure_aware(dt: datetime, tz_name: str | None) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=get_zone(tz_name))
