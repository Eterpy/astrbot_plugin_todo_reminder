from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import command_group
from astrbot.api.star import Context, Star, StarTools, register

from .src.config_utils import parse_bool_config, parse_int_config, parse_text_config, parse_timezone_config
from .src.commands import TodoReminderCommands
from .src.holiday import HolidayManager
from .src.scheduler import TodoReminderScheduler
from .src.service import TodoReminderService, parse_llm_datetime
from .src.storage import TodoReminderStore


def _tool_error_message(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    logger.error(f"todo_reminder 工具执行失败: {exc}")
    return "操作失败，请稍后重试。"


@register("todo_reminder", "interpy", "私聊本地待办与提醒插件", "0.1.0")
class TodoReminderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.max_items_per_user = parse_int_config(self.config, "max_items_per_user", 50)
        if self.max_items_per_user < 0:
            logger.warning("配置项 max_items_per_user 不能为负数，使用默认值 50")
            self.max_items_per_user = 50
        self.reply_in_group = parse_bool_config(self.config, "reply_in_group", False)
        self.enable_llm_reminder = parse_bool_config(self.config, "enable_llm_reminder", True)
        self.reminder_prompt = parse_text_config(self.config, "reminder_prompt", "")
        self.holiday_cache_days = parse_int_config(self.config, "holiday_cache_days", 30)
        self.timezone_name, self.timezone = parse_timezone_config(self.config)

        data_dir = StarTools.get_data_dir("todo_reminder")
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = TodoReminderStore(data_dir / "todo_reminder_data.json")
        self.holiday_manager = HolidayManager(data_dir / "holiday_cache.json", self.holiday_cache_days)
        self.scheduler_manager = TodoReminderScheduler(
            context,
            self.store,
            self.holiday_manager,
            enable_llm_reminder=self.enable_llm_reminder,
            reminder_prompt=self.reminder_prompt,
            timezone=self.timezone,
        )
        self.service = TodoReminderService(
            self.store,
            self.scheduler_manager,
            self.max_items_per_user,
            timezone=self.timezone,
        )
        self.commands = TodoReminderCommands(self.service, reply_in_group=self.reply_in_group)
        logger.info("todo_reminder 插件启动成功（仅支持私聊）")

    @command_group("todo")
    def todo(self):
        """本地待办和提醒"""
        pass

    @todo.command("help")
    async def help_cmd(self, event: AstrMessageEvent):
        """显示帮助"""
        async for result in self.commands.help(event):
            yield result

    @todo.command("add")
    async def add_todo_cmd(
        self,
        event: AstrMessageEvent,
        text: str,
        time_str: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        """添加待办，可选绑定提醒"""
        async for result in self.commands.add_todo(event, text, time_str, repeat, holiday_type):
            yield result

    @todo.command("ls")
    async def list_cmd(self, event: AstrMessageEvent, status: str | None = "open"):
        """查看待办和提醒"""
        async for result in self.commands.list_all(event, status):
            yield result

    @todo.command("list")
    async def list_alias_cmd(self, event: AstrMessageEvent, status: str | None = "open"):
        """查看待办和提醒"""
        async for result in self.commands.list_all(event, status):
            yield result

    @todo.command("all")
    async def all_cmd(self, event: AstrMessageEvent):
        """查看全部待办和提醒"""
        async for result in self.commands.list_all(event, "all"):
            yield result

    @todo.command("open")
    async def open_cmd(self, event: AstrMessageEvent):
        """查看未完成待办和提醒"""
        async for result in self.commands.list_all(event, "open"):
            yield result

    @todo.command("completed")
    async def completed_cmd(self, event: AstrMessageEvent):
        """查看已完成待办和提醒"""
        async for result in self.commands.list_all(event, "done"):
            yield result

    @todo.command("done")
    async def done_cmd(self, event: AstrMessageEvent, selector: str):
        """完成待办"""
        async for result in self.commands.done_todo(event, selector, True):
            yield result

    @todo.command("undone")
    async def undone_cmd(self, event: AstrMessageEvent, selector: str):
        """取消完成待办"""
        async for result in self.commands.done_todo(event, selector, False):
            yield result

    @todo.command("edit")
    async def edit_cmd(self, event: AstrMessageEvent, selector: str, text: str):
        """修改待办内容"""
        async for result in self.commands.edit_todo(event, selector, text):
            yield result

    @todo.command("rm")
    async def rm_cmd(self, event: AstrMessageEvent, selector: str):
        """删除待办"""
        async for result in self.commands.remove_todo(event, selector):
            yield result

    @todo.command("del")
    async def del_cmd(self, event: AstrMessageEvent, selector: str):
        """删除待办"""
        async for result in self.commands.remove_todo(event, selector):
            yield result

    @todo.command("clear")
    async def clear_cmd(self, event: AstrMessageEvent):
        """删除所有待办"""
        async for result in self.commands.clear_todos(event):
            yield result

    @todo.command("remind")
    async def remind_cmd(
        self,
        event: AstrMessageEvent,
        text: str,
        time_str: str,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        """添加独立提醒"""
        async for result in self.commands.add_reminder(event, text, time_str, repeat, holiday_type):
            yield result

    @todo.command("reminders")
    async def reminders_cmd(self, event: AstrMessageEvent):
        """查看提醒"""
        async for result in self.commands.list_reminders(event):
            yield result

    @todo.command("rlist")
    async def rlist_cmd(self, event: AstrMessageEvent):
        """查看提醒"""
        async for result in self.commands.list_reminders(event):
            yield result

    @todo.command("rclear")
    async def rclear_cmd(self, event: AstrMessageEvent, keyword: str | None = None):
        """删除所有提醒，或删除包含关键词的提醒"""
        async for result in self.commands.clear_reminders(event, keyword):
            yield result

    @todo.command("rclear-todo")
    async def rclear_todo_cmd(self, event: AstrMessageEvent, keyword: str):
        """删除关联到匹配待办的提醒"""
        async for result in self.commands.clear_reminders_by_todo(event, keyword):
            yield result

    @todo.command("rclear_todo")
    async def rclear_todo_alias_cmd(self, event: AstrMessageEvent, keyword: str):
        """删除关联到匹配待办的提醒"""
        async for result in self.commands.clear_reminders_by_todo(event, keyword):
            yield result

    @todo.command("redt")
    async def redt_cmd(
        self,
        event: AstrMessageEvent,
        selector: str,
        text: str | None = None,
        time_str: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        """修改提醒"""
        async for result in self.commands.edit_reminder(event, selector, text, time_str, repeat, holiday_type):
            yield result

    @todo.command("rtime")
    async def rtime_cmd(
        self,
        event: AstrMessageEvent,
        selector: str,
        time_str: str,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        """修改提醒时间和重复规则"""
        async for result in self.commands.edit_reminder(event, selector, None, time_str, repeat, holiday_type):
            yield result

    @todo.command("rtext")
    async def rtext_cmd(self, event: AstrMessageEvent, selector: str, text: str):
        """修改提醒内容"""
        async for result in self.commands.edit_reminder(event, selector, text, None, None, None):
            yield result

    @todo.command("rrm")
    async def rrm_cmd(self, event: AstrMessageEvent, selector: str):
        """删除提醒"""
        async for result in self.commands.remove_reminder(event, selector):
            yield result

    @filter.llm_tool(name="create_todo")
    async def create_todo_tool(
        self,
        event: AstrMessageEvent,
        text: str,
        datetime_str: str | None = None,
        reminder_text: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
        notes: str | None = None,
    ):
        """创建待办，可选绑定提醒。

        Args:
            text(string): 待办内容
            datetime_str(string): 可选，提醒时间，格式 YYYY-MM-DD HH:MM
            reminder_text(string): 可选，提醒内容；为空时使用待办内容
            repeat(string): 可选，none,daily,weekly,monthly,yearly
            holiday_type(string): 可选，none,workday,holiday
            notes(string): 可选，待办备注
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        try:
            parsed = parse_llm_datetime(datetime_str, self.timezone) if datetime_str else None
            todo, reminder = await self.service.create_todo(
                event.unified_msg_origin,
                text,
                notes=notes,
                reminder_text=reminder_text,
                datetime_str=parsed,
                repeat=repeat,
                holiday_type=holiday_type,
                time_already_parsed=bool(parsed),
            )
        except Exception as exc:
            return _tool_error_message(exc)
        if reminder:
            return f"已创建待办\n内容：{todo['text']}\n提醒：{reminder['datetime']}"
        return f"已创建待办\n内容：{todo['text']}"

    @filter.llm_tool(name="create_reminder")
    async def create_reminder_tool(
        self,
        event: AstrMessageEvent,
        text: str,
        datetime_str: str,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        """创建独立提醒。

        Args:
            text(string): 提醒内容
            datetime_str(string): 提醒时间，格式 YYYY-MM-DD HH:MM
            repeat(string): 可选，none,daily,weekly,monthly,yearly
            holiday_type(string): 可选，none,workday,holiday
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        try:
            reminder = await self.service.create_reminder(
                event.unified_msg_origin,
                text,
                parse_llm_datetime(datetime_str, self.timezone),
                repeat=repeat,
                holiday_type=holiday_type,
                time_already_parsed=True,
            )
        except Exception as exc:
            return _tool_error_message(exc)
        return f"已创建提醒\n内容：{reminder['text']}\n时间：{reminder['datetime']}"

    @filter.llm_tool(name="list_todos_and_reminders")
    async def list_todos_and_reminders_tool(self, event: AstrMessageEvent, status: str | None = "open"):
        """列出当前私聊中的待办和提醒。

        Args:
            status(string): open,done,all
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        return self.service.list_text(event.unified_msg_origin, status or "open")

    @filter.llm_tool(name="update_todo")
    async def update_todo_tool(
        self,
        event: AstrMessageEvent,
        selector: str,
        text: str | None = None,
        done: str | None = None,
    ):
        """修改待办内容或完成状态。

        Args:
            selector(string): 待办序号
            text(string): 可选，新待办内容
            done(string): 可选，yes 表示完成，no 表示取消完成
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        try:
            changed = None
            if text is not None:
                changed = await self.service.edit_todo(event.unified_msg_origin, selector, text)
            if done and done.lower() in {"yes", "no"}:
                changed = await self.service.set_todo_done(event.unified_msg_origin, selector, done.lower() == "yes")
        except Exception as exc:
            return _tool_error_message(exc)
        if not changed:
            return "没有找到这个待办，或没有提供要修改的内容。"
        return f"已更新待办\n内容：{changed['text']}"

    @filter.llm_tool(name="delete_todo")
    async def delete_todo_tool(self, event: AstrMessageEvent, selector: str, confirm: str | None = None):
        """删除待办，并同步删除关联提醒。

        Args:
            selector(string): 待办序号；如果用户要删除所有待办，传 all
            confirm(string): 批量删除确认；删除所有待办时必须传 yes
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        if self._is_all_selector(selector):
            if not self._is_confirmed(confirm):
                session = self.store.peek_session(event.unified_msg_origin)
                count = len(session["todos"])
                linked_count = self._count_linked_reminders_for_todos(session)
                if not count:
                    return "当前没有待办。"
                return f"将删除所有待办 {count} 条，并同步删除关联提醒 {linked_count} 条。确认删除请再次调用并传 confirm=yes。"
            todos, linked = await self.service.delete_all_todos(event.unified_msg_origin)
            if not todos:
                return "当前没有待办。"
            suffix = f"\n已同步删除关联提醒：{len(linked)} 条" if linked else ""
            return f"已删除所有待办\n数量：{len(todos)} 条{suffix}"
        todo, linked = await self.service.delete_todo(event.unified_msg_origin, selector)
        if not todo:
            return "没有找到这个待办。"
        suffix = f"\n已同步删除关联提醒：{len(linked)} 条" if linked else ""
        return f"已删除待办\n内容：{todo['text']}{suffix}"

    @filter.llm_tool(name="delete_all_todos")
    async def delete_all_todos_tool(self, event: AstrMessageEvent, confirm: str | None = None):
        """删除当前私聊中的所有待办，并同步删除这些待办关联的提醒。

        Args:
            confirm(string): 批量删除确认；必须传 yes
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        if not self._is_confirmed(confirm):
            session = self.store.peek_session(event.unified_msg_origin)
            count = len(session["todos"])
            linked_count = self._count_linked_reminders_for_todos(session)
            if not count:
                return "当前没有待办。"
            return f"将删除所有待办 {count} 条，并同步删除关联提醒 {linked_count} 条。确认删除请再次调用并传 confirm=yes。"
        todos, linked = await self.service.delete_all_todos(event.unified_msg_origin)
        if not todos:
            return "当前没有待办。"
        suffix = f"\n已同步删除关联提醒：{len(linked)} 条" if linked else ""
        return f"已删除所有待办\n数量：{len(todos)} 条{suffix}"

    @filter.llm_tool(name="update_reminder")
    async def update_reminder_tool(
        self,
        event: AstrMessageEvent,
        selector: str,
        text: str | None = None,
        datetime_str: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        """修改提醒。

        Args:
            selector(string): 提醒序号
            text(string): 可选，新提醒内容
            datetime_str(string): 可选，新提醒时间，格式 YYYY-MM-DD HH:MM
            repeat(string): 可选，none,daily,weekly,monthly,yearly
            holiday_type(string): 可选，none,workday,holiday
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        try:
            parsed = parse_llm_datetime(datetime_str, self.timezone) if datetime_str else None
            reminder = await self.service.edit_reminder(
                event.unified_msg_origin,
                selector,
                text=text,
                datetime_str=parsed,
                repeat=repeat,
                holiday_type=holiday_type,
                time_already_parsed=bool(parsed),
            )
        except Exception as exc:
            return _tool_error_message(exc)
        if not reminder:
            return "没有找到这个提醒。"
        return f"已更新提醒\n内容：{reminder['text']}\n时间：{reminder['datetime']}"

    @filter.llm_tool(name="delete_reminder")
    async def delete_reminder_tool(self, event: AstrMessageEvent, selector: str, confirm: str | None = None):
        """删除提醒。

        Args:
            selector(string): 提醒序号；如果用户要删除所有提醒，传 all
            confirm(string): 批量删除确认；删除所有提醒时必须传 yes
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        if self._is_all_selector(selector):
            if not self._is_confirmed(confirm):
                count = len(self.store.peek_session(event.unified_msg_origin)["reminders"])
                if not count:
                    return "当前没有提醒。"
                return f"将删除所有提醒 {count} 条。确认删除请再次调用并传 confirm=yes。"
            reminders = await self.service.delete_all_reminders(event.unified_msg_origin)
            if not reminders:
                return "当前没有提醒。"
            return f"已删除所有提醒\n数量：{len(reminders)} 条"
        reminder = await self.service.delete_reminder(event.unified_msg_origin, selector)
        if not reminder:
            return "没有找到这个提醒。"
        return f"已删除提醒\n内容：{reminder['text']}"

    @filter.llm_tool(name="delete_all_reminders")
    async def delete_all_reminders_tool(self, event: AstrMessageEvent, confirm: str | None = None):
        """删除当前私聊中的所有提醒。

        Args:
            confirm(string): 批量删除确认；必须传 yes
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        if not self._is_confirmed(confirm):
            count = len(self.store.peek_session(event.unified_msg_origin)["reminders"])
            if not count:
                return "当前没有提醒。"
            return f"将删除所有提醒 {count} 条。确认删除请再次调用并传 confirm=yes。"
        reminders = await self.service.delete_all_reminders(event.unified_msg_origin)
        if not reminders:
            return "当前没有提醒。"
        return f"已删除所有提醒\n数量：{len(reminders)} 条"

    @filter.llm_tool(name="delete_reminders_by_keyword")
    async def delete_reminders_by_keyword_tool(self, event: AstrMessageEvent, keyword: str, confirm: str | None = None):
        """删除提醒内容包含关键词的所有提醒。

        Args:
            keyword(string): 提醒内容关键词
            confirm(string): 批量删除确认；必须传 yes
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        if not self._is_confirmed(confirm):
            count = self._count_reminders_by_keyword(event.unified_msg_origin, keyword)
            if not count:
                return f"没有找到包含“{keyword}”的提醒。"
            return f"将删除包含“{keyword}”的提醒 {count} 条。确认删除请再次调用并传 confirm=yes。"
        reminders = await self.service.delete_reminders_by_keyword(event.unified_msg_origin, keyword)
        if not reminders:
            return f"没有找到包含“{keyword}”的提醒。"
        return f"已删除匹配提醒\n关键词：{keyword}\n数量：{len(reminders)} 条"

    @filter.llm_tool(name="delete_reminders_by_todo_keyword")
    async def delete_reminders_by_todo_keyword_tool(self, event: AstrMessageEvent, keyword: str, confirm: str | None = None):
        """删除所有关联到匹配待办的提醒。

        Args:
            keyword(string): 待办内容关键词
            confirm(string): 批量删除确认；必须传 yes
        """
        if not self._is_private_event(event):
            return "本插件仅支持私聊，请在私聊中使用。"
        if not self._is_confirmed(confirm):
            count = self._count_reminders_by_todo_keyword(event.unified_msg_origin, keyword)
            if not count:
                return f"没有找到关联“{keyword}”待办的提醒。"
            return f"将删除关联“{keyword}”待办的提醒 {count} 条。确认删除请再次调用并传 confirm=yes。"
        reminders = await self.service.delete_reminders_by_todo_keyword(event.unified_msg_origin, keyword)
        if not reminders:
            return f"没有找到关联“{keyword}”待办的提醒。"
        return f"已删除关联待办提醒\n待办关键词：{keyword}\n数量：{len(reminders)} 条"

    async def terminate(self):
        try:
            self.scheduler_manager.shutdown()
        except Exception as exc:
            logger.warning(f"关闭 todo_reminder 调度任务失败: {exc}")

    def _is_private_event(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            return ":FriendMessage:" in getattr(event, "unified_msg_origin", "")

    def _is_all_selector(self, selector: str | None) -> bool:
        if selector is None:
            return False
        return selector.strip().lower() in {
            "all",
            "全部",
            "所有",
            "清空",
            "全部待办",
            "所有待办",
            "全部提醒",
            "所有提醒",
        }

    def _is_confirmed(self, confirm: str | None) -> bool:
        return (confirm or "").strip().lower() in {"yes", "true", "确认"}

    def _count_reminders_by_keyword(self, session_id: str, keyword: str) -> int:
        keyword = (keyword or "").strip()
        if not keyword:
            return 0
        return sum(1 for reminder in self.store.peek_session(session_id)["reminders"] if keyword in str(reminder.get("text", "")))

    def _count_reminders_by_todo_keyword(self, session_id: str, keyword: str) -> int:
        keyword = (keyword or "").strip()
        if not keyword:
            return 0
        session = self.store.peek_session(session_id)
        matched_todo_ids = {todo.get("id") for todo in session["todos"] if keyword in str(todo.get("text", ""))}
        return sum(1 for reminder in session["reminders"] if reminder.get("todo_id") in matched_todo_ids)

    def _count_linked_reminders_for_todos(self, session: dict) -> int:
        todo_ids = {todo.get("id") for todo in session.get("todos", [])}
        return sum(1 for reminder in session.get("reminders", []) if reminder.get("todo_id") in todo_ids)
