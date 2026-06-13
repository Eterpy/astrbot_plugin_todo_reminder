from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

from .models import repeat_description
from .service import TodoReminderService


def _user_error_message(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    logger.error(f"todo_reminder 命令执行失败: {exc}")
    return "操作失败，请稍后重试。"


class TodoReminderCommands:
    def __init__(self, service: TodoReminderService, *, reply_in_group: bool = False):
        self.service = service
        self.reply_in_group = reply_in_group

    async def add_todo(
        self,
        event: AstrMessageEvent,
        text: str,
        time_str: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        if not await self._ensure_private(event):
            return
        if _contains_command_tail(time_str, repeat, holiday_type):
            yield _one_command_at_a_time_result(event)
            return
        try:
            todo, reminder = await self.service.create_todo(
                event.unified_msg_origin,
                text,
                datetime_str=time_str,
                repeat=repeat,
                holiday_type=holiday_type,
            )
        except Exception as exc:
            yield event.plain_result(_user_error_message(exc))
            return
        if reminder:
            yield event.plain_result(
                "已添加待办\n"
                f"内容：{todo['text']}\n"
                f"提醒：{reminder['datetime']}，{repeat_description(reminder.get('repeat'), reminder.get('holiday_type'))}\n"
                "查看：/todo ls"
            )
        else:
            yield event.plain_result(f"已添加待办\n内容：{todo['text']}\n查看：/todo ls")

    async def list_all(self, event: AstrMessageEvent, status: str | None = "open"):
        if not await self._ensure_private(event):
            return
        yield event.plain_result(self.service.list_text(event.unified_msg_origin, status or "open"))

    async def edit_todo(self, event: AstrMessageEvent, selector: str, text: str):
        if not await self._ensure_private(event):
            return
        if _contains_command_tail(text):
            yield _one_command_at_a_time_result(event)
            return
        try:
            todo = await self.service.edit_todo(event.unified_msg_origin, selector, text)
        except Exception as exc:
            yield event.plain_result(_user_error_message(exc))
            return
        if not todo:
            yield event.plain_result("没有找到这个待办。请用 /todo ls 查看序号后再操作。")
            return
        yield event.plain_result(f"已修改待办\n内容：{todo['text']}")

    async def done_todo(self, event: AstrMessageEvent, selector: str, done: bool):
        if not await self._ensure_private(event):
            return
        todo = await self.service.set_todo_done(event.unified_msg_origin, selector, done)
        if not todo:
            yield event.plain_result("没有找到这个待办。请用 /todo ls 查看序号后再操作。")
            return
        title = "已完成待办" if done else "已取消完成"
        yield event.plain_result(f"{title}\n内容：{todo['text']}")

    async def remove_todo(self, event: AstrMessageEvent, selector: str):
        if not await self._ensure_private(event):
            return
        if _is_all_selector(selector):
            async for result in self.clear_todos(event):
                yield result
            return
        todo, linked = await self.service.delete_todo(event.unified_msg_origin, selector)
        if not todo:
            yield event.plain_result("没有找到这个待办。请用 /todo ls 查看序号后再操作。")
            return
        suffix = f"，并删除关联提醒 {len(linked)} 条" if linked else ""
        yield event.plain_result(f"已删除待办\n内容：{todo['text']}{suffix}")

    async def clear_todos(self, event: AstrMessageEvent):
        if not await self._ensure_private(event):
            return
        todos, linked = await self.service.delete_all_todos(event.unified_msg_origin)
        if not todos:
            yield event.plain_result("当前没有待办。")
            return
        suffix = f"\n已同步删除关联提醒：{len(linked)} 条" if linked else ""
        yield event.plain_result(f"已删除所有待办\n数量：{len(todos)} 条{suffix}")

    async def add_reminder(
        self,
        event: AstrMessageEvent,
        text: str,
        time_str: str,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        if not await self._ensure_private(event):
            return
        if _contains_command_tail(time_str, repeat, holiday_type):
            yield _one_command_at_a_time_result(event)
            return
        try:
            reminder = await self.service.create_reminder(
                event.unified_msg_origin,
                text,
                time_str,
                repeat=repeat,
                holiday_type=holiday_type,
            )
        except Exception as exc:
            yield event.plain_result(_user_error_message(exc))
            return
        yield event.plain_result(
            "已添加提醒\n"
            f"内容：{reminder['text']}\n"
            f"时间：{reminder['datetime']}\n"
            f"规则：{repeat_description(reminder.get('repeat'), reminder.get('holiday_type'))}\n"
            "查看：/todo reminders"
        )

    async def list_reminders(self, event: AstrMessageEvent):
        if not await self._ensure_private(event):
            return
        yield event.plain_result(self.service.reminders_text(event.unified_msg_origin))

    async def edit_reminder(
        self,
        event: AstrMessageEvent,
        selector: str,
        text: str | None = None,
        time_str: str | None = None,
        repeat: str | None = None,
        holiday_type: str | None = None,
    ):
        if not await self._ensure_private(event):
            return
        if _contains_command_tail(text, time_str, repeat, holiday_type):
            yield _one_command_at_a_time_result(event)
            return
        try:
            reminder = await self.service.edit_reminder(
                event.unified_msg_origin,
                selector,
                text=text,
                datetime_str=time_str,
                repeat=repeat,
                holiday_type=holiday_type,
            )
        except Exception as exc:
            yield event.plain_result(_user_error_message(exc))
            return
        if not reminder:
            yield event.plain_result("没有找到这个提醒。请用 /todo reminders 查看序号后再操作。")
            return
        yield event.plain_result(
            "已修改提醒\n"
            f"内容：{reminder['text']}\n"
            f"时间：{reminder['datetime']}\n"
            f"规则：{repeat_description(reminder.get('repeat'), reminder.get('holiday_type'))}"
        )

    async def remove_reminder(self, event: AstrMessageEvent, selector: str):
        if not await self._ensure_private(event):
            return
        if _is_all_selector(selector):
            async for result in self.clear_reminders(event):
                yield result
            return
        reminder = await self.service.delete_reminder(event.unified_msg_origin, selector)
        if not reminder:
            yield event.plain_result("没有找到这个提醒。请用 /todo reminders 查看序号后再操作。")
            return
        yield event.plain_result(f"已删除提醒\n内容：{reminder['text']}")

    async def clear_reminders(self, event: AstrMessageEvent, keyword: str | None = None):
        if not await self._ensure_private(event):
            return
        if keyword:
            reminders = await self.service.delete_reminders_by_keyword(event.unified_msg_origin, keyword)
            if not reminders:
                yield event.plain_result(f"没有找到包含“{keyword}”的提醒。")
                return
            yield event.plain_result(f"已删除匹配提醒\n关键词：{keyword}\n数量：{len(reminders)} 条")
            return
        reminders = await self.service.delete_all_reminders(event.unified_msg_origin)
        if not reminders:
            yield event.plain_result("当前没有提醒。")
            return
        yield event.plain_result(f"已删除所有提醒\n数量：{len(reminders)} 条")

    async def clear_reminders_by_todo(self, event: AstrMessageEvent, keyword: str):
        if not await self._ensure_private(event):
            return
        reminders = await self.service.delete_reminders_by_todo_keyword(event.unified_msg_origin, keyword)
        if not reminders:
            yield event.plain_result(f"没有找到关联“{keyword}”待办的提醒。")
            return
        yield event.plain_result(f"已删除关联待办提醒\n待办关键词：{keyword}\n数量：{len(reminders)} 条")

    async def help(self, event: AstrMessageEvent):
        if not await self._ensure_private(event):
            return
        yield event.plain_result(HELP_TEXT)

    async def _ensure_private(self, event: AstrMessageEvent) -> bool:
        try:
            is_private = event.is_private_chat()
        except Exception:
            origin = getattr(event, "unified_msg_origin", "")
            is_private = ":FriendMessage:" in origin
        if is_private:
            return True
        if self.reply_in_group:
            message = MessageChain()
            message.chain.append(Plain("本插件仅支持私聊，请私聊我使用待办和提醒。"))
            await event.send(message)
        return False


HELP_TEXT = """本地待办与提醒

常用：
/todo add <内容> [时间] [repeat] [workday|holiday]
/todo ls [open|done|all]
/todo done <待办序号>
/todo rm <待办序号>
/todo clear
/todo remind <内容> <时间> [repeat] [workday|holiday]
/todo reminders
/todo rclear [提醒关键词]
/todo rclear-todo <待办关键词>
/todo rclear_todo <待办关键词>

修改：
/todo edit <待办序号> <新内容>
/todo undone <待办序号>
/todo rtime <提醒序号> <时间> [repeat] [workday|holiday]
/todo rtext <提醒序号> <新内容>
/todo rrm <提醒序号>

别名：
/todo list = /todo ls
/todo all = /todo ls all
/todo open = /todo ls open
/todo completed = /todo ls done
/todo del = /todo rm
/todo clear = 删除所有待办
/todo rlist = /todo reminders
/todo rrm all = 删除所有提醒

repeat：none,daily,weekly,monthly,yearly
时间支持：HH:MM、HHMM、YYYY-MM-DD HH:MM、YYYY-MM-DD-HH:MM、MM-DD-HH:MM、YYYYMMDDHHMM、MMDDHHMM
也可以直接说：明天 9 点提醒我开会；添加待办写周报，周五下午提醒。"""


def _is_all_selector(selector: str | None) -> bool:
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


def _contains_command_tail(*values: str | None) -> bool:
    for value in values:
        if not value:
            continue
        stripped = value.strip()
        if stripped.startswith("/"):
            return True
        if "\n/" in stripped or "\r/" in stripped:
            return True
    return False


def _one_command_at_a_time_result(event: AstrMessageEvent):
    return event.plain_result("一次只发送一条 /todo 命令。请分开发送后续命令。")
