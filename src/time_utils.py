from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .models import DATE_TIME_FORMAT, format_stored_datetime


WEEK_MAP = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def parse_datetime(
    value: str,
    week: str | None = None,
    *,
    reject_explicit_past: bool = False,
    timezone: ZoneInfo | None = None,
) -> str:
    original = value
    text = value.strip().replace("：", ":")
    now = now_in_timezone(timezone)
    parsed: dt.datetime | None = None

    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d-%H:%M"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            break
        except ValueError:
            continue

    if parsed is None:
        parsed = _parse_month_day_time(text, now)

    if parsed is None and text.isdigit():
        parsed = _parse_compact_digits(text, now)

    if parsed is None and ":" in text:
        parts = text.split(":")
        if len(parts) == 2:
            try:
                hour = int(parts[0])
                minute = int(parts[1])
                parsed = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                parsed = None

    if parsed is None:
        raise ValueError(
            "时间格式错误，支持 HH:MM、HHMM、YYYY-MM-DD HH:MM、YYYY-MM-DD-HH:MM、MM-DD-HH:MM、YYYYMMDDHHMM、MMDDHHMM"
        )

    _validate_datetime(parsed, original)
    has_explicit_year = _has_explicit_year(text)
    if reject_explicit_past and has_explicit_year and parsed < now:
        raise ValueError("提醒时间不能早于当前时间。")
    parsed = _adjust_past_datetime(parsed, now, has_explicit_year=has_explicit_year)
    parsed = adjust_datetime_for_week(parsed, week)
    return format_stored_datetime(parsed)


def parse_datetime_for_llm(
    value: str,
    *,
    reject_explicit_past: bool = False,
    timezone: ZoneInfo | None = None,
) -> str:
    text = value.strip().replace("：", ":")
    parsed: dt.datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d-%H:%M"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is not None:
        now = now_in_timezone(timezone)
        if reject_explicit_past and parsed < now:
            raise ValueError("提醒时间不能早于当前时间。")
        return format_stored_datetime(_adjust_past_datetime(parsed, now, has_explicit_year=True))
    return parse_datetime(text, reject_explicit_past=reject_explicit_past, timezone=timezone)


def now_in_timezone(timezone: ZoneInfo | None = None) -> dt.datetime:
    if timezone is None:
        return dt.datetime.now()
    return dt.datetime.now(timezone).replace(tzinfo=None)


def adjust_datetime_for_week(value: dt.datetime, week: str | None = None) -> dt.datetime:
    if not week:
        return value
    weekday = week.strip().lower()
    if weekday not in WEEK_MAP:
        raise ValueError("星期格式错误，可选值：mon,tue,wed,thu,fri,sat,sun")
    days_ahead = WEEK_MAP[weekday] - value.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return value + dt.timedelta(days=days_ahead)


def _parse_compact_digits(text: str, now: dt.datetime) -> dt.datetime | None:
    try:
        if len(text) == 4:
            hour = int(text[:2])
            minute = int(text[2:])
            return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if len(text) == 8:
            month = int(text[:2])
            day = int(text[2:4])
            hour = int(text[4:6])
            minute = int(text[6:])
            return dt.datetime(now.year, month, day, hour, minute)
        if len(text) == 12:
            year = int(text[:4])
            month = int(text[4:6])
            day = int(text[6:8])
            hour = int(text[8:10])
            minute = int(text[10:])
            return dt.datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    return None


def _parse_month_day_time(text: str, now: dt.datetime) -> dt.datetime | None:
    parts = text.split("-")
    if len(parts) != 3 or ":" not in parts[2]:
        return None
    time_parts = parts[2].split(":")
    if len(time_parts) != 2:
        return None
    try:
        month = int(parts[0])
        day = int(parts[1])
        hour = int(time_parts[0])
        minute = int(time_parts[1])
        return dt.datetime(now.year, month, day, hour, minute)
    except ValueError:
        return None


def _validate_datetime(value: dt.datetime, original: str) -> None:
    if not (0 <= value.hour <= 23 and 0 <= value.minute <= 59):
        raise ValueError(f"时间格式错误：{original}")


def _adjust_past_datetime(value: dt.datetime, now: dt.datetime, has_explicit_year: bool) -> dt.datetime:
    if value >= now:
        return value
    if has_explicit_year:
        return value
    if value.year == now.year and (value.month, value.day) != (now.month, now.day):
        return value.replace(year=now.year + 1)
    return value + dt.timedelta(days=1)


def _has_explicit_year(text: str) -> bool:
    if len(text) == 12 and text.isdigit():
        return True
    if len(text) >= 4 and text[:4].isdigit() and ("-" in text or "/" in text):
        return True
    return False
