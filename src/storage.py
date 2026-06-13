from __future__ import annotations

import datetime as dt
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .models import (
    HOLIDAY_TYPES,
    REPEAT_TYPES,
    TODO_STATUS_DONE,
    TODO_STATUS_OPEN,
    now_str,
    parse_stored_datetime,
    update_timestamp,
)


EMPTY_DATA: dict[str, Any] = {"sessions": {}}


class TodoReminderStore:
    def __init__(self, data_file: Path):
        self.data_file = Path(data_file)
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.data_file.exists():
            self._write(EMPTY_DATA.copy())
            return {"sessions": {}}
        try:
            with self.data_file.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as exc:
            backup_path = self._backup_corrupt_file()
            suffix = f"，已备份到 {backup_path}" if backup_path else ""
            logger.error(f"加载待办提醒数据失败，将使用空数据{suffix}: {exc}")
            return {"sessions": {}}
        if not isinstance(loaded, dict):
            return {"sessions": {}}
        normalized, dropped = _normalize_data(loaded)
        if dropped:
            logger.warning(f"加载待办提醒数据时已过滤非法条目 {dropped} 条")
        return normalized

    def _serialize(self, data: dict[str, Any]) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    def _write_text(self, text: str) -> None:
        tmp_path = self.data_file.with_name(f".{self.data_file.name}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(self.data_file)

    def _write(self, data: dict[str, Any]) -> None:
        self._write_text(self._serialize(data))

    async def save(self) -> None:
        # Serialize on the event loop (under the caller's lock) so the dict isn't
        # iterated concurrently, then push the blocking write+fsync to a thread.
        text = self._serialize(self.data)
        await asyncio.to_thread(self._write_text, text)

    def _backup_corrupt_file(self) -> Path | None:
        if not self.data_file.exists():
            return None
        timestamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = self.data_file.with_name(f"{self.data_file.name}.bak.{timestamp}")
        counter = 1
        while backup_path.exists():
            backup_path = self.data_file.with_name(f"{self.data_file.name}.bak.{timestamp}.{counter}")
            counter += 1
        try:
            self.data_file.replace(backup_path)
        except Exception as exc:
            logger.warning(f"备份损坏数据文件失败: {exc}")
            return None
        return backup_path

    def get_session(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        sessions = self.data.setdefault("sessions", {})
        session = sessions.setdefault(session_id, {"todos": [], "reminders": []})
        session.setdefault("todos", [])
        session.setdefault("reminders", [])
        return session

    def all_sessions(self) -> dict[str, dict[str, list[dict[str, Any]]]]:
        return self.data.setdefault("sessions", {})

    def peek_session(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        # Read-only access: never persists an empty session for unknown ids.
        session = self.data.get("sessions", {}).get(session_id)
        if not isinstance(session, dict):
            return {"todos": [], "reminders": []}
        session.setdefault("todos", [])
        session.setdefault("reminders", [])
        return session

    def count_items(self, session_id: str) -> int:
        session = self.peek_session(session_id)
        return len(session["todos"]) + len(session["reminders"])

    def add_todo(self, session_id: str, todo: dict[str, Any]) -> dict[str, Any]:
        self.get_session(session_id)["todos"].append(todo)
        return todo

    def add_reminder(self, session_id: str, reminder: dict[str, Any]) -> dict[str, Any]:
        self.get_session(session_id)["reminders"].append(reminder)
        return reminder

    def list_todos(self, session_id: str, status: str = "open") -> list[dict[str, Any]]:
        todos = self.peek_session(session_id)["todos"]
        if status == "all":
            return list(todos)
        if status == TODO_STATUS_DONE:
            return [todo for todo in todos if todo.get("status") == TODO_STATUS_DONE]
        return [todo for todo in todos if todo.get("status", TODO_STATUS_OPEN) != TODO_STATUS_DONE]

    def list_reminders(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.peek_session(session_id)["reminders"])

    def resolve_todo(self, session_id: str, selector: str | int) -> tuple[dict[str, Any] | None, int | None]:
        todos = self.get_session(session_id)["todos"]
        return _resolve_item(todos, selector)

    def resolve_reminder(self, session_id: str, selector: str | int) -> tuple[dict[str, Any] | None, int | None]:
        reminders = self.get_session(session_id)["reminders"]
        return _resolve_item(reminders, selector)

    def find_todo(self, session_id: str, todo_id: str) -> dict[str, Any] | None:
        for todo in self.peek_session(session_id)["todos"]:
            if todo.get("id") == todo_id:
                return todo
        return None

    def find_reminder(self, session_id: str, reminder_id: str) -> dict[str, Any] | None:
        for reminder in self.peek_session(session_id)["reminders"]:
            if reminder.get("id") == reminder_id:
                return reminder
        return None

    def remove_todo(self, session_id: str, selector: str | int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        session = self.get_session(session_id)
        todo, index = self.resolve_todo(session_id, selector)
        if todo is None or index is None:
            return None, []
        removed = session["todos"].pop(index)
        linked: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for reminder in session["reminders"]:
            if reminder.get("todo_id") == removed.get("id"):
                linked.append(reminder)
            else:
                remaining.append(reminder)
        session["reminders"] = remaining
        return removed, linked

    def remove_all_todos(self, session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        session = self.get_session(session_id)
        removed_todos = list(session["todos"])
        removed_todo_ids = {todo.get("id") for todo in removed_todos}
        linked_reminders: list[dict[str, Any]] = []
        remaining_reminders: list[dict[str, Any]] = []
        for reminder in session["reminders"]:
            if reminder.get("todo_id") in removed_todo_ids:
                linked_reminders.append(reminder)
            else:
                remaining_reminders.append(reminder)
        session["todos"] = []
        session["reminders"] = remaining_reminders
        return removed_todos, linked_reminders

    def remove_reminder(self, session_id: str, selector: str | int) -> dict[str, Any] | None:
        reminders = self.get_session(session_id)["reminders"]
        reminder, index = self.resolve_reminder(session_id, selector)
        if reminder is None or index is None:
            return None
        return reminders.pop(index)

    def remove_all_reminders(self, session_id: str) -> list[dict[str, Any]]:
        session = self.get_session(session_id)
        removed = list(session["reminders"])
        session["reminders"] = []
        return removed

    def remove_reminders_by_keyword(self, session_id: str, keyword: str) -> list[dict[str, Any]]:
        keyword = keyword.strip()
        if not keyword:
            return []
        session = self.get_session(session_id)
        removed: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for reminder in session["reminders"]:
            if keyword in str(reminder.get("text", "")):
                removed.append(reminder)
            else:
                remaining.append(reminder)
        session["reminders"] = remaining
        return removed

    def remove_reminders_by_todo_keyword(self, session_id: str, keyword: str) -> list[dict[str, Any]]:
        keyword = keyword.strip()
        if not keyword:
            return []
        session = self.get_session(session_id)
        matched_todo_ids = {
            todo.get("id")
            for todo in session["todos"]
            if keyword in str(todo.get("text", ""))
        }
        if not matched_todo_ids:
            return []
        removed: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for reminder in session["reminders"]:
            if reminder.get("todo_id") in matched_todo_ids:
                removed.append(reminder)
            else:
                remaining.append(reminder)
        session["reminders"] = remaining
        return removed

    def set_todo_text(self, session_id: str, selector: str | int, text: str) -> dict[str, Any] | None:
        todo, _ = self.resolve_todo(session_id, selector)
        if todo is None:
            return None
        todo["text"] = text.strip()
        update_timestamp(todo)
        return todo

    def set_todo_status(self, session_id: str, selector: str | int, status: str) -> dict[str, Any] | None:
        todo, _ = self.resolve_todo(session_id, selector)
        if todo is None:
            return None
        todo["status"] = status
        update_timestamp(todo)
        return todo

    def update_reminder(
        self,
        session_id: str,
        selector: str | int,
        *,
        text: str | None = None,
        datetime_str: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ) -> dict[str, Any] | None:
        reminder, _ = self.resolve_reminder(session_id, selector)
        if reminder is None:
            return None
        if text:
            reminder["text"] = text.strip()
        if datetime_str:
            reminder["datetime"] = datetime_str
        if repeat:
            reminder["repeat"] = repeat
        if holiday_type:
            reminder["holiday_type"] = holiday_type
        update_timestamp(reminder)
        return reminder


MIN_ID_PREFIX_LENGTH = 8


def _resolve_item(items: list[dict[str, Any]], selector: str | int) -> tuple[dict[str, Any] | None, int | None]:
    selector_str = str(selector).strip()
    if selector_str.isdigit():
        index = int(selector_str) - 1
        if 0 <= index < len(items):
            return items[index], index
        return None, None
    if not selector_str:
        return None, None

    id_prefix_matches: list[tuple[dict[str, Any], int]] = []
    text_matches: list[tuple[dict[str, Any], int]] = []
    for index, item in enumerate(items):
        item_id = str(item.get("id", ""))
        if item_id == selector_str:
            return item, index
        if len(selector_str) >= MIN_ID_PREFIX_LENGTH and item_id.startswith(selector_str):
            id_prefix_matches.append((item, index))
        text = str(item.get("text", ""))
        if selector_str in text:
            text_matches.append((item, index))
    if len(id_prefix_matches) == 1:
        return id_prefix_matches[0]
    if len(text_matches) == 1:
        return text_matches[0]
    return None, None


def _normalize_data(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    normalized: dict[str, Any] = {"sessions": {}}
    dropped = 0
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return normalized, 0
    for session_id, session in sessions.items():
        if not isinstance(session_id, str) or not isinstance(session, dict):
            dropped += 1
            continue
        todos, dropped_todos = _normalize_items(session.get("todos"), _normalize_todo)
        reminders, dropped_reminders = _normalize_items(session.get("reminders"), _normalize_reminder)
        dropped += dropped_todos + dropped_reminders
        normalized["sessions"][session_id] = {"todos": todos, "reminders": reminders}
    return normalized, dropped


def _normalize_items(value: Any, normalizer) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(value, list):
        return [], 0
    result: list[dict[str, Any]] = []
    dropped = 0
    for item in value:
        normalized = normalizer(item)
        if normalized is None:
            dropped += 1
            continue
        result.append(normalized)
    return result, dropped


def _normalize_todo(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = str(item.get("id", "")).strip()
    text = str(item.get("text", "")).strip()
    if not item_id or not text:
        return None
    current = now_str()
    status = item.get("status")
    if status not in {TODO_STATUS_OPEN, TODO_STATUS_DONE}:
        status = TODO_STATUS_OPEN
    normalized = dict(item)
    normalized["id"] = item_id
    normalized["text"] = text
    normalized["status"] = status
    normalized["created_at"] = str(item.get("created_at") or current)
    normalized["updated_at"] = str(item.get("updated_at") or normalized["created_at"])
    if "notes" in normalized and normalized["notes"] is not None:
        normalized["notes"] = str(normalized["notes"]).strip()
    return normalized


def _normalize_reminder(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_id = str(item.get("id", "")).strip()
    text = str(item.get("text", "")).strip()
    datetime_str = str(item.get("datetime", "")).strip()
    if not item_id or not text or not datetime_str:
        return None
    try:
        parse_stored_datetime(datetime_str)
    except ValueError:
        return None
    current = now_str()
    repeat = str(item.get("repeat", "none")).strip().lower()
    if repeat not in REPEAT_TYPES:
        repeat = "none"
    holiday_type = str(item.get("holiday_type", "none")).strip().lower()
    if holiday_type not in HOLIDAY_TYPES:
        holiday_type = "none"
    normalized = dict(item)
    normalized["id"] = item_id
    normalized["text"] = text
    normalized["datetime"] = datetime_str
    normalized["repeat"] = repeat
    normalized["holiday_type"] = holiday_type
    normalized["created_at"] = str(item.get("created_at") or current)
    normalized["updated_at"] = str(item.get("updated_at") or normalized["created_at"])
    if "todo_id" in normalized and normalized["todo_id"] is not None:
        normalized["todo_id"] = str(normalized["todo_id"])
    if "job_id" in normalized and normalized["job_id"] is not None:
        normalized["job_id"] = str(normalized["job_id"])
    return normalized
