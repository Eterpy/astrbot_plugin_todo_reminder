from __future__ import annotations

import copy
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    TODO_STATUS_DONE,
    TODO_STATUS_OPEN,
    make_reminder,
    make_todo,
    normalize_holiday_type,
    normalize_repeat,
    repeat_description,
)
from .scheduler import TodoReminderScheduler
from .storage import TodoReminderStore
from .time_utils import now_in_timezone, parse_datetime, parse_datetime_for_llm

MAX_TEXT_LENGTH = 500
MAX_NOTES_LENGTH = 1000
DEFAULT_MAX_ITEMS_PER_USER = 50


class TodoReminderService:
    def __init__(
        self,
        store: TodoReminderStore,
        scheduler: TodoReminderScheduler,
        max_items_per_user: int = 50,
        *,
        timezone: ZoneInfo | None = None,
    ):
        self.store = store
        self.scheduler = scheduler
        self.max_items_per_user = _normalize_max_items(max_items_per_user)
        self.timezone = timezone
        self._lock = store.lock

    async def create_todo(
        self,
        session_id: str,
        text: str,
        *,
        notes: str | None = None,
        reminder_text: str | None = None,
        datetime_str: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
        time_already_parsed: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        todo_text = _require_text(text, "待办内容", max_length=MAX_TEXT_LENGTH)
        note_text = _optional_text(notes, "待办备注", max_length=MAX_NOTES_LENGTH)
        reminder_body = _require_text(reminder_text, "提醒内容") if reminder_text else todo_text
        parsed_time = None
        normalized_repeat = normalize_repeat(repeat)
        normalized_holiday_type = normalize_holiday_type(holiday_type)
        if datetime_str:
            parsed_time = (
                self._validate_preparsed_datetime(datetime_str, reject_past=True)
                if time_already_parsed
                else parse_datetime(datetime_str, reject_explicit_past=True, timezone=self.timezone)
            )

        todo = make_todo(todo_text, note_text)
        reminder = None
        if parsed_time:
            reminder = make_reminder(
                reminder_body,
                parsed_time,
                repeat=normalized_repeat,
                holiday_type=normalized_holiday_type,
                todo_id=todo["id"],
            )
            await self.scheduler.prepare_reminder(session_id, reminder)

        async with self._lock:
            self._ensure_can_create(session_id, 1 + (1 if datetime_str else 0))
            session = self.store.get_session(session_id)
            original_todos = list(session["todos"])
            original_reminders = list(session["reminders"])
            added_job_id = None
            try:
                self.store.add_todo(session_id, todo)
                if reminder:
                    self.store.add_reminder(session_id, reminder)
                    added_job_id = self.scheduler.add_job(session_id, reminder)
                await self.store.save()
            except Exception:
                session["todos"] = original_todos
                session["reminders"] = original_reminders
                if added_job_id:
                    self.scheduler.remove_job(added_job_id)
                raise
            return todo, reminder

    async def create_reminder(
        self,
        session_id: str,
        text: str,
        datetime_str: str,
        *,
        week: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
        todo_id: str | None = None,
        time_already_parsed: bool = False,
    ) -> dict[str, Any]:
        reminder_text = _require_text(text, "提醒内容", max_length=MAX_TEXT_LENGTH)
        normalized_repeat = normalize_repeat(repeat)
        normalized_holiday_type = normalize_holiday_type(holiday_type)
        parsed_time = (
            self._validate_preparsed_datetime(datetime_str, reject_past=True)
            if time_already_parsed
            else parse_datetime(datetime_str, week=week, reject_explicit_past=True, timezone=self.timezone)
        )

        reminder = make_reminder(
            reminder_text,
            parsed_time,
            repeat=normalized_repeat,
            holiday_type=normalized_holiday_type,
            todo_id=todo_id,
        )
        await self.scheduler.prepare_reminder(session_id, reminder)

        async with self._lock:
            self._ensure_can_create(session_id, 1)
            session = self.store.get_session(session_id)
            original_reminders = list(session["reminders"])
            added_job_id = None
            try:
                self.store.add_reminder(session_id, reminder)
                added_job_id = self.scheduler.add_job(session_id, reminder)
                await self.store.save()
            except Exception:
                session["reminders"] = original_reminders
                if added_job_id:
                    self.scheduler.remove_job(added_job_id)
                raise
            return reminder

    async def edit_todo(self, session_id: str, selector: str | int, text: str) -> dict[str, Any] | None:
        cleaned_text = _require_text(text, "待办内容", max_length=MAX_TEXT_LENGTH)
        async with self._lock:
            todo, _ = self.store.resolve_todo(session_id, selector)
            if todo is None:
                return None
            before = copy.deepcopy(todo)
            try:
                todo = self.store.set_todo_text(session_id, selector, cleaned_text)
                await self.store.save()
            except Exception:
                todo.clear()
                todo.update(before)
                raise
            return todo

    async def set_todo_done(self, session_id: str, selector: str | int, done: bool) -> dict[str, Any] | None:
        async with self._lock:
            status = TODO_STATUS_DONE if done else TODO_STATUS_OPEN
            todo, _ = self.store.resolve_todo(session_id, selector)
            if todo is None:
                return None
            before = copy.deepcopy(todo)
            try:
                todo = self.store.set_todo_status(session_id, selector, status)
                await self.store.save()
            except Exception:
                todo.clear()
                todo.update(before)
                raise
            return todo

    async def delete_todo(self, session_id: str, selector: str | int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        async with self._lock:
            session = self.store.get_session(session_id)
            original_todos = list(session["todos"])
            original_reminders = list(session["reminders"])
            todo, linked = self.store.remove_todo(session_id, selector)
            if todo:
                try:
                    await self.store.save()
                except Exception:
                    session["todos"] = original_todos
                    session["reminders"] = original_reminders
                    raise
                for reminder in linked:
                    self.scheduler.remove_reminder_job(reminder)
            return todo, linked

    async def delete_all_todos(self, session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async with self._lock:
            session = self.store.get_session(session_id)
            original_todos = list(session["todos"])
            original_reminders = list(session["reminders"])
            todos, linked_reminders = self.store.remove_all_todos(session_id)
            if todos or linked_reminders:
                try:
                    await self.store.save()
                except Exception:
                    session["todos"] = original_todos
                    session["reminders"] = original_reminders
                    raise
                for reminder in linked_reminders:
                    self.scheduler.remove_reminder_job(reminder)
            return todos, linked_reminders

    async def delete_reminder(self, session_id: str, selector: str | int) -> dict[str, Any] | None:
        async with self._lock:
            session = self.store.get_session(session_id)
            original_reminders = list(session["reminders"])
            reminder = self.store.remove_reminder(session_id, selector)
            if reminder:
                try:
                    await self.store.save()
                except Exception:
                    session["reminders"] = original_reminders
                    raise
                self.scheduler.remove_reminder_job(reminder)
            return reminder

    async def delete_all_reminders(self, session_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            session = self.store.get_session(session_id)
            original_reminders = list(session["reminders"])
            reminders = self.store.remove_all_reminders(session_id)
            if reminders:
                try:
                    await self.store.save()
                except Exception:
                    session["reminders"] = original_reminders
                    raise
                for reminder in reminders:
                    self.scheduler.remove_reminder_job(reminder)
            return reminders

    async def delete_reminders_by_keyword(self, session_id: str, keyword: str) -> list[dict[str, Any]]:
        async with self._lock:
            session = self.store.get_session(session_id)
            original_reminders = list(session["reminders"])
            reminders = self.store.remove_reminders_by_keyword(session_id, keyword)
            if reminders:
                try:
                    await self.store.save()
                except Exception:
                    session["reminders"] = original_reminders
                    raise
                for reminder in reminders:
                    self.scheduler.remove_reminder_job(reminder)
            return reminders

    async def delete_reminders_by_todo_keyword(self, session_id: str, keyword: str) -> list[dict[str, Any]]:
        async with self._lock:
            session = self.store.get_session(session_id)
            original_reminders = list(session["reminders"])
            reminders = self.store.remove_reminders_by_todo_keyword(session_id, keyword)
            if reminders:
                try:
                    await self.store.save()
                except Exception:
                    session["reminders"] = original_reminders
                    raise
                for reminder in reminders:
                    self.scheduler.remove_reminder_job(reminder)
            return reminders

    async def edit_reminder(
        self,
        session_id: str,
        selector: str | int,
        *,
        text: str | None = None,
        datetime_str: str | None = None,
        week: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
        time_already_parsed: bool = False,
    ) -> dict[str, Any] | None:
        parsed_time = None
        if datetime_str:
            parsed_time = (
                self._validate_preparsed_datetime(datetime_str, reject_past=True)
                if time_already_parsed
                else parse_datetime(datetime_str, week=week, reject_explicit_past=True, timezone=self.timezone)
            )
        cleaned_text = _require_text(text, "提醒内容", max_length=MAX_TEXT_LENGTH) if text is not None else None
        normalized_repeat = normalize_repeat(repeat) if repeat else None
        normalized_holiday_type = normalize_holiday_type(holiday_type) if holiday_type else None
        before_for_prepare = None
        if parsed_time is not None or normalized_repeat is not None or normalized_holiday_type is not None:
            async with self._lock:
                current, _ = self.store.resolve_reminder(session_id, selector)
                if current is None:
                    return None
                before_for_prepare = copy.deepcopy(current)
            prepared = copy.deepcopy(before_for_prepare)
            if cleaned_text is not None:
                prepared["text"] = cleaned_text
            if parsed_time is not None:
                prepared["datetime"] = parsed_time
            if normalized_repeat is not None:
                prepared["repeat"] = normalized_repeat
            if normalized_holiday_type is not None:
                prepared["holiday_type"] = normalized_holiday_type
            await self.scheduler.prepare_reminder(session_id, prepared)
            if parsed_time is not None:
                parsed_time = prepared["datetime"]

        async with self._lock:
            reminder, _ = self.store.resolve_reminder(session_id, selector)
            if reminder is None:
                return None
            if before_for_prepare and not _same_reminder_snapshot(reminder, before_for_prepare):
                raise ValueError("提醒已被其他操作更新，请重新查看后再修改。")
            before = copy.deepcopy(reminder)
            old_job_id = reminder.get("job_id")
            schedule_changed = parsed_time is not None or normalized_repeat is not None or normalized_holiday_type is not None
            new_job_id = None
            try:
                updated = self.store.update_reminder(
                    session_id,
                    selector,
                    text=cleaned_text,
                    datetime_str=parsed_time,
                    repeat=normalized_repeat,
                    holiday_type=normalized_holiday_type,
                )
                if updated and schedule_changed:
                    new_job_id = self.scheduler.add_job(session_id, updated)
                await self.store.save()
            except Exception:
                reminder.clear()
                reminder.update(before)
                if new_job_id and new_job_id != old_job_id:
                    self.scheduler.remove_job(new_job_id)
                if old_job_id:
                    try:
                        self.scheduler.add_job(session_id, reminder)
                    except Exception:
                        pass
                raise
            return reminder

    def list_text(self, session_id: str, status: str = "open") -> str:
        status_filter = _normalize_status_filter(status)
        all_todos = self.store.peek_session(session_id)["todos"]
        reminders = self.store.list_reminders(session_id)
        open_count = sum(1 for todo in all_todos if todo.get("status") != TODO_STATUS_DONE)
        done_count = len(all_todos) - open_count
        lines: list[str] = []

        lines.append("待办清单")
        lines.append(f"概览：未完成 {open_count} · 已完成 {done_count} · 提醒 {len(reminders)}")
        lines.append("")

        todo_lines: list[str] = []
        for index, todo in enumerate(all_todos, 1):
            if not _matches_status_filter(todo, status_filter):
                continue
            marker = "√" if todo.get("status") == TODO_STATUS_DONE else "□"
            todo_lines.append(f"{index}. {marker} {todo.get('text', '')}")

        if todo_lines:
            lines.append(_todo_section_title(status_filter))
            lines.extend(todo_lines)
        else:
            lines.append(f"{_todo_section_title(status_filter)}")
            lines.append("无")

        if reminders:
            lines.append("")
            lines.append("提醒列表")
            for index, reminder in enumerate(reminders, 1):
                linked = self._linked_todo_description(session_id, reminder, prefix=" · ")
                rule = repeat_description(reminder.get("repeat", "none"), reminder.get("holiday_type", "none"))
                lines.append(f"{index}. {reminder.get('text', '')}")
                lines.append(f"   └ {reminder.get('datetime', '')} · {rule}{linked}")
        else:
            lines.append("")
            lines.append("提醒列表")
            lines.append("无")

        return "\n".join(lines)

    def reminders_text(self, session_id: str) -> str:
        reminders = self.store.list_reminders(session_id)
        if not reminders:
            return "当前没有提醒。"
        lines = ["提醒列表"]
        for index, reminder in enumerate(reminders, 1):
            linked = self._linked_todo_description(session_id, reminder, prefix=" · ")
            rule = repeat_description(reminder.get("repeat", "none"), reminder.get("holiday_type", "none"))
            lines.append(f"{index}. {reminder.get('text', '')}")
            lines.append(f"   └ {reminder.get('datetime', '')} · {rule}{linked}")
        return "\n".join(lines)

    def _linked_todo_description(self, session_id: str, reminder: dict[str, Any], prefix: str = "，") -> str:
        todo_id = reminder.get("todo_id")
        if not todo_id:
            return ""
        todo = self.store.find_todo(session_id, todo_id)
        if todo:
            return f"{prefix}关联：{todo.get('text', '')}"
        return f"{prefix}关联待办已删除"

    def _ensure_can_create(self, session_id: str, count: int) -> None:
        if self.max_items_per_user <= 0:
            return
        if self.store.count_items(session_id) + count > self.max_items_per_user:
            raise ValueError(f"创建失败：已达到每个私聊会话最大条目数限制({self.max_items_per_user})。")

    def _validate_preparsed_datetime(self, value: str, *, reject_past: bool) -> str:
        from .models import format_stored_datetime, parse_stored_datetime

        parsed = parse_stored_datetime(value)
        if reject_past and parsed < now_in_timezone(self.timezone):
            raise ValueError("提醒时间不能早于当前时间。")
        return format_stored_datetime(parsed)


def _normalize_status_filter(status: str | None) -> str:
    value = (status or "open").strip().lower()
    aliases = {
        "all": "all",
        "a": "all",
        "全部": "all",
        "done": "done",
        "completed": "done",
        "finish": "done",
        "finished": "done",
        "已完成": "done",
        "open": "open",
        "todo": "open",
        "doing": "open",
        "未完成": "open",
    }
    if value in aliases:
        return aliases[value]
    return "open"


def _matches_status_filter(todo: dict[str, Any], status_filter: str) -> bool:
    if status_filter == "all":
        return True
    is_done = todo.get("status") == TODO_STATUS_DONE
    if status_filter == "done":
        return is_done
    return not is_done


def _todo_section_title(status_filter: str) -> str:
    if status_filter == "all":
        return "待办列表："
    if status_filter == "done":
        return "已完成待办："
    return "未完成待办："


def parse_llm_datetime(value: str, timezone: ZoneInfo | None = None) -> str:
    return parse_datetime_for_llm(value, reject_explicit_past=True, timezone=timezone)


def _normalize_max_items(value: int) -> int:
    if value < 0:
        return DEFAULT_MAX_ITEMS_PER_USER
    return value


def _optional_text(value: str | None, field_name: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > max_length:
        raise ValueError(f"{field_name}不能超过 {max_length} 个字符。")
    return text


def _require_text(value: str | None, field_name: str, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field_name}不能为空。")
    if len(text) > max_length:
        raise ValueError(f"{field_name}不能超过 {max_length} 个字符。")
    return text


def _same_reminder_snapshot(current: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    return (
        current.get("id") == snapshot.get("id")
        and current.get("text") == snapshot.get("text")
        and current.get("datetime") == snapshot.get("datetime")
        and current.get("repeat", "none") == snapshot.get("repeat", "none")
        and current.get("holiday_type", "none") == snapshot.get("holiday_type", "none")
    )
