from __future__ import annotations

import datetime as dt
import uuid
from typing import Any


DATE_TIME_FORMAT = "%Y-%m-%d %H:%M"
REPEAT_TYPES = {"none", "daily", "weekly", "monthly", "yearly"}
HOLIDAY_TYPES = {"none", "workday", "holiday"}
TODO_STATUS_OPEN = "open"
TODO_STATUS_DONE = "done"


def now_str() -> str:
    return dt.datetime.now().strftime(DATE_TIME_FORMAT)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_repeat(repeat: str | None) -> str:
    value = (repeat or "none").strip().lower()
    if value not in REPEAT_TYPES:
        raise ValueError("重复类型错误，可选值：none,daily,weekly,monthly,yearly")
    return value


def normalize_holiday_type(holiday_type: str | None) -> str:
    value = (holiday_type or "none").strip().lower()
    if value not in HOLIDAY_TYPES:
        raise ValueError("节假日类型错误，可选值：none,workday,holiday")
    return value


def make_todo(text: str, notes: str | None = None) -> dict[str, Any]:
    current = now_str()
    item: dict[str, Any] = {
        "id": new_id("todo"),
        "text": text.strip(),
        "status": TODO_STATUS_OPEN,
        "created_at": current,
        "updated_at": current,
    }
    if notes:
        item["notes"] = notes.strip()
    return item


def make_reminder(
    text: str,
    datetime_str: str,
    repeat: str | None = None,
    holiday_type: str | None = None,
    todo_id: str | None = None,
) -> dict[str, Any]:
    current = now_str()
    reminder: dict[str, Any] = {
        "id": new_id("rem"),
        "text": text.strip(),
        "datetime": datetime_str,
        "repeat": normalize_repeat(repeat),
        "holiday_type": normalize_holiday_type(holiday_type),
        "created_at": current,
        "updated_at": current,
    }
    if todo_id:
        reminder["todo_id"] = todo_id
    return reminder


def update_timestamp(item: dict[str, Any]) -> None:
    item["updated_at"] = now_str()


def parse_stored_datetime(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, DATE_TIME_FORMAT)


def format_stored_datetime(value: dt.datetime) -> str:
    return value.strftime(DATE_TIME_FORMAT)


def repeat_description(repeat: str | None, holiday_type: str | None = None) -> str:
    repeat_value = normalize_repeat(repeat)
    holiday_value = normalize_holiday_type(holiday_type)
    base = {
        "none": "一次性",
        "daily": "每天",
        "weekly": "每周",
        "monthly": "每月",
        "yearly": "每年",
    }[repeat_value]
    if holiday_value == "workday":
        return f"{base}，仅工作日触发"
    if holiday_value == "holiday":
        return f"{base}，仅节假日触发"
    return base

