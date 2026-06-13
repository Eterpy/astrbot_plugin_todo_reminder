from __future__ import annotations

import datetime as dt
import asyncio
import hashlib
import sys
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import JobLookupError
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .holiday import HolidayManager
from .models import format_stored_datetime, parse_stored_datetime, repeat_description
from .storage import TodoReminderStore
from .time_utils import now_in_timezone


REGISTRY_NAME = "_TODO_REMINDER_SCHEDULER_REGISTRY"
DEFAULT_REMINDER_PROMPT = (
    "你是一个简洁自然的提醒助手。当前时间是 {current_time}。"
    "请提醒用户：{reminder_text}。如果关联待办不是空：{todo_text}。"
    "请直接输出要发送给用户的话，不要解释背景。"
)
PROMPT_USER_TEXT_LIMIT = 500
LLM_REMINDER_TIMEOUT_SECONDS = 30


class TodoReminderScheduler:
    def __init__(
        self,
        context,
        store: TodoReminderStore,
        holiday_manager: HolidayManager,
        *,
        enable_llm_reminder: bool = True,
        reminder_prompt: str | None = None,
        timezone: ZoneInfo | None = None,
    ):
        self.context = context
        self.store = store
        self.holiday_manager = holiday_manager
        self.enable_llm_reminder = enable_llm_reminder
        self.reminder_prompt = reminder_prompt or DEFAULT_REMINDER_PROMPT
        self.timezone = timezone
        self.namespace = f"todo_reminder_{_stable_hash(str(self.store.data_file.resolve()))}"
        self.scheduler = self._get_scheduler()
        self.restore_jobs()
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("待办提醒调度器已启动")

    def _get_scheduler(self) -> AsyncIOScheduler:
        # The scheduler is stashed on the `sys` module so a single instance
        # survives plugin hot-reloads (a fresh import won't re-run module globals,
        # but `sys` persists). Jobs are namespaced per data-file to stay isolated.
        timezone_key = getattr(self.timezone, "key", None)
        if not hasattr(sys, REGISTRY_NAME):
            scheduler = AsyncIOScheduler(timezone=self.timezone) if self.timezone else AsyncIOScheduler()
            setattr(sys, REGISTRY_NAME, {"scheduler": scheduler, "timezone_key": timezone_key})
        registry = getattr(sys, REGISTRY_NAME)
        scheduler = registry.get("scheduler")
        if scheduler is None or registry.get("timezone_key") != timezone_key:
            if scheduler is not None:
                self._remove_plugin_jobs(scheduler)
            scheduler = AsyncIOScheduler(timezone=self.timezone) if self.timezone else AsyncIOScheduler()
            registry["scheduler"] = scheduler
            registry["timezone_key"] = timezone_key
        return scheduler

    def restore_jobs(self) -> None:
        self._remove_plugin_jobs(self.scheduler)

        missed = 0
        for session_id, session in list(self.store.all_sessions().items()):
            reminders = list(session.get("reminders", []))
            for reminder in reminders:
                try:
                    when = parse_stored_datetime(reminder["datetime"])
                except Exception as exc:
                    logger.warning(f"跳过无法解析时间的提醒: {reminder} ({exc})")
                    continue
                if reminder.get("repeat", "none") == "none" and when < now_in_timezone(self.timezone):
                    self._schedule_missed_reminder(session_id, reminder)
                    missed += 1
                    continue
                self.add_job(session_id, reminder)
        if missed:
            logger.info(f"已安排补发错过的一次性提醒 {missed} 条")

    def _schedule_missed_reminder(self, session_id: str, reminder: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(f"当前没有运行中的事件循环，无法立即补发错过的提醒: {reminder.get('id')}")
            return
        self._schedule_task(
            loop,
            self._handle_missed_one_time_reminder(session_id, reminder["id"]),
            f"补发错过的一次性提醒: {reminder.get('id')}",
        )

    def _schedule_task(self, loop: asyncio.AbstractEventLoop, coro, description: str) -> None:
        task = loop.create_task(coro)

        def _log_task_result(done: asyncio.Task) -> None:
            try:
                done.result()
            except asyncio.CancelledError:
                logger.warning(f"后台任务已取消: {description}")
            except Exception as exc:
                logger.warning(f"后台任务失败: {description} ({exc})")

        task.add_done_callback(_log_task_result)

    async def prepare_reminder(self, session_id: str, reminder: dict[str, Any]) -> None:
        if reminder.get("repeat", "none") != "none":
            return
        await self._adjust_one_time_holiday_datetime(reminder)

    async def _handle_missed_one_time_reminder(self, session_id: str, reminder_id: str) -> None:
        async with self.store.lock:
            reminder = self._find_reminder(session_id, reminder_id)
            if reminder is None:
                return
            original_snapshot = dict(reminder)
            reminder_snapshot = dict(reminder)
        if await self._adjust_one_time_holiday_datetime(reminder_snapshot):
            async with self.store.lock:
                reminder = self._find_reminder(session_id, reminder_id)
                if reminder is None:
                    return
                if not _same_schedule(reminder, original_snapshot):
                    logger.info(f"提醒已被更新，跳过旧快照顺延保存: {reminder_id}")
                    return
                original_datetime = reminder.get("datetime")
                reminder["datetime"] = reminder_snapshot["datetime"]
                try:
                    await self.store.save()
                except Exception as exc:
                    if original_datetime:
                        reminder["datetime"] = original_datetime
                    logger.warning(f"保存顺延后的一次性提醒失败: {reminder_id} ({exc})")
                    return
                self.add_job(session_id, reminder)
            return
        await self._reminder_callback(session_id, reminder_id)

    async def _adjust_one_time_holiday_datetime(self, reminder: dict[str, Any]) -> bool:
        holiday_type = reminder.get("holiday_type", "none")
        if holiday_type not in {"workday", "holiday"}:
            return False
        when = parse_stored_datetime(reminder["datetime"])
        original = when
        now = now_in_timezone(self.timezone)
        if when < now:
            when = now.replace(hour=when.hour, minute=when.minute, second=0, microsecond=0)
            if when < now:
                when += dt.timedelta(days=1)
        for _ in range(370):
            matched = (
                await self.holiday_manager.is_workday(when)
                if holiday_type == "workday"
                else await self.holiday_manager.is_holiday(when)
            )
            if matched:
                break
            when += dt.timedelta(days=1)
        else:
            logger.warning(f"无法为提醒 {reminder.get('id')} 找到满足 {holiday_type} 的日期")
            return False
        if when != original:
            reminder["datetime"] = format_stored_datetime(when)
            return True
        return False

    def add_job(self, session_id: str, reminder: dict[str, Any]) -> str:
        old_job_id = reminder.get("job_id")
        if old_job_id:
            self.remove_job(old_job_id)

        when = parse_stored_datetime(reminder["datetime"])
        job_id = f"{self.namespace}_{_stable_hash(session_id)}_{reminder['id']}"
        callback = self._callback_for(reminder)
        args = [session_id, reminder["id"]]
        trigger_kwargs = self._build_trigger_kwargs(reminder, when)
        self.scheduler.add_job(callback, args=args, misfire_grace_time=60, id=job_id, replace_existing=True, **trigger_kwargs)
        reminder["job_id"] = job_id
        logger.info(f"注册提醒 job: {job_id}")
        return job_id

    def remove_job(self, job_id: str | None) -> bool:
        if not job_id:
            return False
        try:
            self.scheduler.remove_job(job_id)
            return True
        except JobLookupError:
            return False
        except Exception as exc:
            logger.warning(f"删除提醒 job 失败 {job_id}: {exc}")
            return False

    def remove_reminder_job(self, reminder: dict[str, Any]) -> None:
        self.remove_job(reminder.get("job_id"))

    def shutdown(self) -> None:
        self._remove_plugin_jobs(self.scheduler)

    def _remove_plugin_jobs(self, scheduler: AsyncIOScheduler) -> None:
        for job in scheduler.get_jobs():
            if job.id.startswith(f"{self.namespace}_"):
                try:
                    scheduler.remove_job(job.id)
                except JobLookupError:
                    pass
                except Exception as exc:
                    logger.warning(f"删除提醒 job 失败 {job.id}: {exc}")

    def _callback_for(self, reminder: dict[str, Any]):
        holiday_type = reminder.get("holiday_type", "none")
        if holiday_type == "workday":
            return self._workday_callback
        if holiday_type == "holiday":
            return self._holiday_callback
        return self._reminder_callback

    def _build_trigger_kwargs(self, reminder: dict[str, Any], when: dt.datetime) -> dict[str, Any]:
        repeat = reminder.get("repeat", "none")
        if repeat == "daily":
            return {"trigger": "cron", "hour": when.hour, "minute": when.minute}
        if repeat == "weekly":
            return {"trigger": "cron", "day_of_week": when.weekday(), "hour": when.hour, "minute": when.minute}
        if repeat == "monthly":
            return {"trigger": "cron", "day": when.day, "hour": when.hour, "minute": when.minute}
        if repeat == "yearly":
            return {
                "trigger": "cron",
                "month": when.month,
                "day": when.day,
                "hour": when.hour,
                "minute": when.minute,
            }
        return {"trigger": "date", "run_date": when}

    async def _workday_callback(self, session_id: str, reminder_id: str) -> None:
        if await self.holiday_manager.is_workday(now_in_timezone(self.timezone)):
            await self._reminder_callback(session_id, reminder_id)
        else:
            logger.info(f"非工作日，跳过提醒: {reminder_id}")

    async def _holiday_callback(self, session_id: str, reminder_id: str) -> None:
        if await self.holiday_manager.is_holiday(now_in_timezone(self.timezone)):
            await self._reminder_callback(session_id, reminder_id)
        else:
            logger.info(f"非法定节假日，跳过提醒: {reminder_id}")

    async def _reminder_callback(self, session_id: str, reminder_id: str) -> None:
        async with self.store.lock:
            reminder = self._find_reminder(session_id, reminder_id)
            todo = self.store.find_todo(session_id, reminder["todo_id"]) if reminder and reminder.get("todo_id") else None
        if reminder is None:
            logger.warning(f"提醒不存在，跳过: {session_id} {reminder_id}")
            return

        content = await self._build_reminder_content(session_id, reminder, todo)
        message = MessageChain()
        message.chain.append(Plain(content))
        try:
            await self.context.send_message(session_id, message)
        except Exception as exc:
            logger.warning(f"发送提醒失败，保留提醒以便下次重试: {session_id} {reminder_id} ({exc})")
            return

        if reminder.get("repeat", "none") == "none":
            async with self.store.lock:
                session = self.store.get_session(session_id)
                original_reminders = list(session["reminders"])
                removed = self.store.remove_reminder(session_id, reminder_id)
                if removed:
                    try:
                        await self.store.save()
                    except Exception as exc:
                        session["reminders"] = original_reminders
                        logger.warning(f"保存一次性提醒删除状态失败，已保留提醒: {session_id} {reminder_id} ({exc})")
                        return
                    self.remove_job(removed.get("job_id"))

    def _find_reminder(self, session_id: str, reminder_id: str) -> dict[str, Any] | None:
        for reminder in self.store.get_session(session_id)["reminders"]:
            if reminder.get("id") == reminder_id:
                return reminder
        return None

    async def _build_reminder_content(
        self,
        session_id: str,
        reminder: dict[str, Any],
        todo: dict[str, Any] | None,
    ) -> str:
        fallback = self._fallback_message(reminder, todo)
        if not self.enable_llm_reminder:
            return fallback
        provider = self.context.get_using_provider()
        if provider is None:
            return fallback

        try:
            todo_text = _truncate_prompt_text(todo.get("text")) if todo else ""
            prompt = self.reminder_prompt.format(
                reminder_text=_truncate_prompt_text(reminder.get("text")),
                todo_text=todo_text,
                current_time=now_in_timezone(self.timezone).strftime("%Y-%m-%d %H:%M"),
            )
            response = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, session_id=session_id, contexts=[]),
                timeout=LLM_REMINDER_TIMEOUT_SECONDS,
            )
            text = getattr(response, "completion_text", None)
            return text.strip() if text else fallback
        except Exception as exc:
            logger.warning(f"AI 生成提醒失败，使用固定文本: {exc}")
            return fallback

    def _fallback_message(self, reminder: dict[str, Any], todo: dict[str, Any] | None) -> str:
        if todo:
            return f"[提醒] {reminder.get('text', '')}\n关联待办：{todo.get('text', '')}"
        repeat = repeat_description(reminder.get("repeat", "none"), reminder.get("holiday_type", "none"))
        return f"提醒：{reminder.get('text', '')}\n规则：{repeat}"


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _truncate_prompt_text(value: Any) -> str:
    text = str(value or "")
    if len(text) <= PROMPT_USER_TEXT_LIMIT:
        return text
    return text[:PROMPT_USER_TEXT_LIMIT] + "..."


def _same_schedule(current: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    return (
        current.get("datetime") == snapshot.get("datetime")
        and current.get("repeat", "none") == snapshot.get("repeat", "none")
        and current.get("holiday_type", "none") == snapshot.get("holiday_type", "none")
    )
