from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo


def _install_dependency_stubs() -> None:
    class _Logger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    class _Plain:
        def __init__(self, text: str):
            self.text = text

    class _MessageChain:
        def __init__(self):
            self.chain = []

    class _JobLookupError(Exception):
        pass

    class _AsyncIOScheduler:
        def __init__(self, **kwargs):
            self.jobs = {}
            self.running = False

        def get_jobs(self):
            return list(self.jobs.values())

        def start(self):
            self.running = True

        def add_job(self, callback, args=None, id=None, replace_existing=False, **kwargs):
            if not id:
                raise ValueError("job id is required")
            if not replace_existing and id in self.jobs:
                raise ValueError("job already exists")
            job = types.SimpleNamespace(id=id, callback=callback, args=args or [], kwargs=kwargs)
            self.jobs[id] = job
            return job

        def remove_job(self, job_id):
            if job_id not in self.jobs:
                raise _JobLookupError(job_id)
            del self.jobs[job_id]

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.AstrBotConfig = dict
    api_module.logger = _Logger()
    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.MessageChain = _MessageChain
    class _FilterNamespace:
        @staticmethod
        def llm_tool(**kwargs):
            return lambda func: func

    event_module.filter = _FilterNamespace
    event_filter_module = types.ModuleType("astrbot.api.event.filter")
    class _CommandGroup:
        def __init__(self, func):
            self.func = func

        def __call__(self, *args, **kwargs):
            return self.func(*args, **kwargs)

        def command(self, *args, **kwargs):
            return lambda func: func

    event_filter_module.command_group = lambda *args, **kwargs: (lambda func: _CommandGroup(func))
    components_module = types.ModuleType("astrbot.api.message_components")
    components_module.Plain = _Plain
    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = object
    star_module.Star = object
    star_module.StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path(tempfile.gettempdir()) / name)
    star_module.register = lambda *args, **kwargs: (lambda cls: cls)

    apscheduler_module = types.ModuleType("apscheduler")
    schedulers_module = types.ModuleType("apscheduler.schedulers")
    asyncio_module = types.ModuleType("apscheduler.schedulers.asyncio")
    asyncio_module.AsyncIOScheduler = _AsyncIOScheduler
    base_module = types.ModuleType("apscheduler.schedulers.base")
    base_module.JobLookupError = _JobLookupError
    aiohttp_module = types.ModuleType("aiohttp")
    aiohttp_module.ClientSession = object

    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules.setdefault("astrbot.api", api_module)
    sys.modules.setdefault("astrbot.api.event", event_module)
    sys.modules.setdefault("astrbot.api.event.filter", event_filter_module)
    sys.modules.setdefault("astrbot.api.message_components", components_module)
    sys.modules.setdefault("astrbot.api.star", star_module)
    sys.modules.setdefault("apscheduler", apscheduler_module)
    sys.modules.setdefault("apscheduler.schedulers", schedulers_module)
    sys.modules.setdefault("apscheduler.schedulers.asyncio", asyncio_module)
    sys.modules.setdefault("apscheduler.schedulers.base", base_module)
    sys.modules.setdefault("aiohttp", aiohttp_module)


_install_dependency_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.models import format_stored_datetime, make_reminder, make_todo, parse_stored_datetime
from src.config_utils import parse_bool_config, parse_int_config, parse_timezone_config
from src.commands import TodoReminderCommands
from src.scheduler import REGISTRY_NAME, TodoReminderScheduler
from src.service import TodoReminderService, parse_llm_datetime
from src.storage import TodoReminderStore
from src.time_utils import parse_datetime
from astrbot_plugin_todo_reminder.main import TodoReminderPlugin


class _DummyScheduler:
    def __init__(self):
        self.jobs = []
        self.removed = []
        self.prepared = []

    def add_job(self, session_id, reminder):
        reminder["job_id"] = f"job_{reminder['id']}"
        self.jobs.append((session_id, reminder))
        return reminder["job_id"]

    def remove_reminder_job(self, reminder):
        self.removed.append(reminder)

    def remove_job(self, job_id):
        self.removed.append({"job_id": job_id})

    async def prepare_reminder(self, session_id, reminder):
        self.prepared.append((session_id, reminder))


class _FailingAddJobScheduler(_DummyScheduler):
    def add_job(self, session_id, reminder):
        raise RuntimeError("add job failed")


class _FailingSecondAddJobScheduler(_DummyScheduler):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def add_job(self, session_id, reminder):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("second add failed")
        return super().add_job(session_id, reminder)


class _FailingSaveStore(TodoReminderStore):
    async def save(self):
        raise RuntimeError("save failed")


class _HolidayManager:
    def __init__(self, *, workdays=None, holidays=None):
        self.workdays = set(workdays or [])
        self.holidays = set(holidays or [])

    async def is_workday(self, value=None):
        if self.workdays:
            return value.strftime("%Y-%m-%d") in self.workdays
        return True

    async def is_holiday(self, value=None):
        if self.holidays:
            return value.strftime("%Y-%m-%d") in self.holidays
        return True


class _EditingHolidayManager(_HolidayManager):
    def __init__(self, store, reminder, *, new_datetime: str, workdays=None):
        super().__init__(workdays=workdays)
        self.store = store
        self.reminder = reminder
        self.new_datetime = new_datetime
        self.edited = False

    async def is_workday(self, value=None):
        if not self.edited:
            self.reminder["datetime"] = self.new_datetime
            self.edited = True
        return await super().is_workday(value)


class _EditingPrepareScheduler(_DummyScheduler):
    def __init__(self, target_reminder, *, new_text: str):
        super().__init__()
        self.target_reminder = target_reminder
        self.new_text = new_text
        self.edited = False

    async def prepare_reminder(self, session_id, reminder):
        await super().prepare_reminder(session_id, reminder)
        if not self.edited:
            self.target_reminder["text"] = self.new_text
            self.edited = True


class _Context:
    def __init__(self, *, fail_send: bool = False, provider=None):
        self.fail_send = fail_send
        self.provider = provider
        self.messages = []

    async def send_message(self, session_id, message):
        if self.fail_send:
            raise RuntimeError("send failed")
        self.messages.append((session_id, message))

    def get_using_provider(self):
        return self.provider


class _Event:
    unified_msg_origin = "session"

    def is_private_chat(self):
        return True

    def plain_result(self, text):
        return text


class _PluginHarness:
    def __init__(self, store, service):
        self.store = store
        self.service = service
        self.timezone = None

    _is_private_event = TodoReminderPlugin._is_private_event
    _is_all_selector = TodoReminderPlugin._is_all_selector
    _is_confirmed = TodoReminderPlugin._is_confirmed
    _count_reminders_by_keyword = TodoReminderPlugin._count_reminders_by_keyword
    _count_reminders_by_todo_keyword = TodoReminderPlugin._count_reminders_by_todo_keyword
    _count_linked_reminders_for_todos = TodoReminderPlugin._count_linked_reminders_for_todos
    create_todo_tool = TodoReminderPlugin.create_todo_tool
    create_reminder_tool = TodoReminderPlugin.create_reminder_tool
    update_reminder_tool = TodoReminderPlugin.update_reminder_tool
    delete_todo_tool = TodoReminderPlugin.delete_todo_tool
    delete_all_todos_tool = TodoReminderPlugin.delete_all_todos_tool
    delete_reminder_tool = TodoReminderPlugin.delete_reminder_tool
    delete_all_reminders_tool = TodoReminderPlugin.delete_all_reminders_tool
    delete_reminders_by_keyword_tool = TodoReminderPlugin.delete_reminders_by_keyword_tool
    delete_reminders_by_todo_keyword_tool = TodoReminderPlugin.delete_reminders_by_todo_keyword_tool


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if hasattr(sys, REGISTRY_NAME):
            delattr(sys, REGISTRY_NAME)

    async def _drain_pending(self):
        # save() now offloads the file write to a thread, so a single sleep(0)
        # no longer runs a fire-and-forget background task to completion.
        for _ in range(100):
            await asyncio.sleep(0)
            pending = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done()
            ]
            if not pending:
                return
            await asyncio.wait(pending, timeout=1)

    async def test_invalid_todo_reminder_time_does_not_mutate_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            scheduler = _DummyScheduler()
            service = TodoReminderService(store, scheduler)

            with self.assertRaises(ValueError):
                await service.create_todo("session", "写周报", datetime_str="not-a-time")

            session = store.get_session("session")
            self.assertEqual(session["todos"], [])
            self.assertEqual(session["reminders"], [])
            self.assertEqual(scheduler.jobs, [])

    async def test_add_job_failure_rolls_back_created_reminder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            service = TodoReminderService(store, _FailingAddJobScheduler())
            future = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=1))

            with self.assertRaisesRegex(RuntimeError, "add job failed"):
                await service.create_reminder("session", "喝水", future, time_already_parsed=True)

            self.assertEqual(store.get_session("session")["reminders"], [])

    async def test_save_failure_rolls_back_created_todo_and_reminder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _FailingSaveStore(Path(temp_dir) / "data.json")
            scheduler = _DummyScheduler()
            service = TodoReminderService(store, scheduler)
            future = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=1))

            with self.assertRaisesRegex(RuntimeError, "save failed"):
                await service.create_todo("session", "写周报", datetime_str=future, time_already_parsed=True)

            session = store.get_session("session")
            self.assertEqual(session["todos"], [])
            self.assertEqual(session["reminders"], [])
            self.assertEqual(len(scheduler.removed), 1)

    async def test_empty_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            service = TodoReminderService(store, _DummyScheduler())

            with self.assertRaisesRegex(ValueError, "待办内容不能为空"):
                await service.create_todo("session", "  ")

            future = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=1))
            with self.assertRaisesRegex(ValueError, "提醒内容不能为空"):
                await service.create_reminder("session", "  ", future, time_already_parsed=True)

    async def test_edit_todo_save_failure_rolls_back_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _FailingSaveStore(Path(temp_dir) / "data.json")
            todo = make_todo("写周报")
            before = dict(todo)
            store.add_todo("session", todo)
            service = TodoReminderService(store, _DummyScheduler())

            with self.assertRaisesRegex(RuntimeError, "save failed"):
                await service.edit_todo("session", "1", "改成月报")

            self.assertEqual(store.get_session("session")["todos"][0], before)

    async def test_done_todo_save_failure_rolls_back_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _FailingSaveStore(Path(temp_dir) / "data.json")
            todo = make_todo("写周报")
            before = dict(todo)
            store.add_todo("session", todo)
            service = TodoReminderService(store, _DummyScheduler())

            with self.assertRaisesRegex(RuntimeError, "save failed"):
                await service.set_todo_done("session", "1", True)

            self.assertEqual(store.get_session("session")["todos"][0], before)

    async def test_delete_reminder_save_failure_keeps_reminder_and_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _FailingSaveStore(Path(temp_dir) / "data.json")
            scheduler = _DummyScheduler()
            reminder = make_reminder("喝水", "2099-01-01 09:00")
            reminder["job_id"] = "job_reminder"
            store.add_reminder("session", reminder)
            service = TodoReminderService(store, scheduler)

            with self.assertRaisesRegex(RuntimeError, "save failed"):
                await service.delete_reminder("session", "1")

            self.assertEqual(store.get_session("session")["reminders"], [reminder])
            self.assertEqual(scheduler.removed, [])

    async def test_delete_reminder_success_removes_job_after_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            scheduler = _DummyScheduler()
            reminder = make_reminder("喝水", "2099-01-01 09:00")
            reminder["job_id"] = "job_reminder"
            store.add_reminder("session", reminder)
            service = TodoReminderService(store, scheduler)

            removed = await service.delete_reminder("session", "1")

            self.assertEqual(removed, reminder)
            self.assertEqual(store.get_session("session")["reminders"], [])
            self.assertEqual(scheduler.removed, [reminder])

    def test_storage_backs_up_corrupt_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "data.json"
            data_file.write_text("{bad json", encoding="utf-8")

            store = TodoReminderStore(data_file)

            self.assertEqual(store.data, {"sessions": {}})
            backups = list(Path(temp_dir).glob("data.json.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{bad json")

    def test_storage_normalizes_legal_but_malformed_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "data.json"
            data_file.write_text(
                json.dumps(
                    {
                        "sessions": {
                            "session": {
                                "todos": [{"id": "todo_1", "text": "写周报"}, {"id": "bad"}],
                                "reminders": [
                                    {"id": "rem_1", "text": "喝水", "datetime": "2099-01-01 09:00"},
                                    {"id": "bad", "text": "无时间"},
                                    {"id": "bad_time", "text": "坏时间", "datetime": "not-a-time"},
                                ],
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            store = TodoReminderStore(data_file)

            session = store.get_session("session")
            self.assertEqual(len(session["todos"]), 1)
            self.assertEqual(session["todos"][0]["status"], "open")
            self.assertEqual(len(session["reminders"]), 1)
            self.assertEqual(session["reminders"][0]["repeat"], "none")

    async def test_atomic_save_writes_readable_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_file = Path(temp_dir) / "data.json"
            store = TodoReminderStore(data_file)
            store.add_todo("session", {"id": "todo_1", "text": "写周报", "status": "open"})

            await store.save()

            loaded = json.loads(data_file.read_text(encoding="utf-8"))
            self.assertEqual(loaded["sessions"]["session"]["todos"][0]["text"], "写周报")
            self.assertEqual(list(Path(temp_dir).glob("*.tmp")), [])

    def test_explicit_past_time_is_rejected_but_time_only_rolls_forward(self):
        past = dt.datetime.now() - dt.timedelta(days=1)

        with self.assertRaisesRegex(ValueError, "提醒时间不能早于当前时间"):
            parse_datetime(format_stored_datetime(past), reject_explicit_past=True)

        parsed = parse_stored_datetime(parse_datetime("0000", reject_explicit_past=True))
        self.assertGreater(parsed, dt.datetime.now())

    def test_month_day_time_rolls_to_next_year_when_same_minute_has_passed(self):
        current = dt.datetime(2099, 6, 14, 20, 49, 50)

        self.assertEqual(
            parse_datetime("06-14-20:49", now=current),
            "2100-06-14 20:49",
        )
        self.assertEqual(
            parse_datetime("06142049", now=current),
            "2100-06-14 20:49",
        )

    def test_month_day_time_finds_next_valid_leap_year(self):
        current = dt.datetime(2099, 1, 1, 0, 0)

        self.assertEqual(parse_datetime("02-29-09:00", now=current), "2104-02-29 09:00")
        self.assertEqual(parse_datetime("02290900", now=current), "2104-02-29 09:00")

    def test_llm_relative_delay_keeps_full_offset_after_minute_rounding(self):
        parsed = parse_llm_datetime(delay_minutes=1, now=dt.datetime(2099, 1, 1, 20, 49, 50))

        self.assertEqual(parsed, "2099-01-01 20:51")

    def test_llm_relative_delay_accepts_seconds(self):
        parsed = parse_llm_datetime(delay_seconds=90, now=dt.datetime(2099, 1, 1, 20, 49, 50))

        self.assertEqual(parsed, "2099-01-01 20:52")

    def test_llm_absolute_time_uses_injected_now(self):
        parsed = parse_llm_datetime("00:00", now=dt.datetime(2099, 1, 1, 20, 49, 50))

        self.assertEqual(parsed, "2099-01-02 00:00")

    def test_llm_relative_delay_takes_precedence_over_datetime(self):
        parsed = parse_llm_datetime(
            "2099-01-01 20:50",
            delay_minutes=1,
            now=dt.datetime(2099, 1, 1, 20, 49, 50),
        )

        self.assertEqual(parsed, "2099-01-01 20:51")

    def test_llm_relative_delay_rejects_invalid_values(self):
        with self.assertRaisesRegex(ValueError, "延迟分钟数必须是非负整数"):
            parse_llm_datetime(delay_minutes="abc", now=dt.datetime(2099, 1, 1, 20, 49, 50))
        with self.assertRaisesRegex(ValueError, "延迟秒数不能为负数"):
            parse_llm_datetime(delay_seconds=-1, now=dt.datetime(2099, 1, 1, 20, 49, 50))
        with self.assertRaisesRegex(ValueError, "相对提醒延迟不能超过 366 天"):
            parse_llm_datetime(delay_seconds=366 * 24 * 60 * 60 + 1, now=dt.datetime(2099, 1, 1, 20, 49, 50))

    async def test_missed_one_time_reminder_is_sent_then_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            reminder = make_reminder(
                "开会",
                format_stored_datetime(dt.datetime.now() - dt.timedelta(minutes=5)),
            )
            store.add_reminder("session", reminder)
            context = _Context()

            TodoReminderScheduler(context, store, _HolidayManager(), enable_llm_reminder=False)
            await self._drain_pending()

            self.assertEqual(len(context.messages), 1)
            self.assertEqual(store.get_session("session")["reminders"], [])

    async def test_failed_missed_reminder_send_is_retained(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            reminder = make_reminder(
                "开会",
                format_stored_datetime(dt.datetime.now() - dt.timedelta(minutes=5)),
            )
            store.add_reminder("session", reminder)
            context = _Context(fail_send=True)

            TodoReminderScheduler(context, store, _HolidayManager(), enable_llm_reminder=False)
            await asyncio.sleep(0)

            self.assertEqual(len(store.get_session("session")["reminders"]), 1)

    async def test_sent_one_time_reminder_delete_save_failure_is_retained(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _FailingSaveStore(Path(temp_dir) / "data.json")
            reminder = make_reminder(
                "开会",
                format_stored_datetime(dt.datetime.now() - dt.timedelta(minutes=5)),
            )
            store.add_reminder("session", reminder)
            context = _Context()

            TodoReminderScheduler(context, store, _HolidayManager(), enable_llm_reminder=False)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertEqual(len(context.messages), 1)
            self.assertEqual(store.get_session("session")["reminders"], [reminder])

    async def test_bad_prompt_template_falls_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            scheduler = TodoReminderScheduler(
                _Context(provider=object()),
                store,
                _HolidayManager(),
                reminder_prompt="{missing_placeholder}",
            )
            reminder = make_reminder(
                "开会",
                format_stored_datetime(dt.datetime.now() + dt.timedelta(minutes=5)),
            )

            content = await scheduler._build_reminder_content("session", reminder, None)

            self.assertIn("提醒：开会", content)

    async def test_text_only_reminder_edit_does_not_recreate_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            scheduler = _DummyScheduler()
            service = TodoReminderService(store, scheduler)
            future = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=1))
            reminder = await service.create_reminder("session", "喝水", future, time_already_parsed=True)
            scheduler.jobs.clear()

            edited = await service.edit_reminder("session", "1", text="喝茶")

            self.assertEqual(edited["text"], "喝茶")
            self.assertEqual(scheduler.jobs, [])
            self.assertEqual(reminder["job_id"], edited["job_id"])

    async def test_edit_reminder_job_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            scheduler = _FailingSecondAddJobScheduler()
            service = TodoReminderService(store, scheduler)
            future = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=1))
            reminder = await service.create_reminder("session", "喝水", future, time_already_parsed=True)
            before = dict(reminder)
            later = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=2))

            with self.assertRaisesRegex(RuntimeError, "second add failed"):
                await service.edit_reminder("session", "1", datetime_str=later, time_already_parsed=True)

            self.assertEqual(store.get_session("session")["reminders"][0], before)

    async def test_one_time_workday_reminder_rolls_forward(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            context = _Context()
            holiday_manager = _HolidayManager(workdays={"2099-01-03"})
            scheduler = TodoReminderScheduler(context, store, holiday_manager, enable_llm_reminder=False)
            reminder = make_reminder("上班提醒", "2099-01-01 09:00", holiday_type="workday")

            await scheduler.prepare_reminder("session", reminder)

            self.assertEqual(reminder["datetime"], "2099-01-03 09:00")

    async def test_missed_workday_reminder_rolls_forward_instead_of_sending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            reminder = make_reminder(
                "上班提醒",
                format_stored_datetime(dt.datetime.now() - dt.timedelta(days=1)),
                holiday_type="workday",
            )
            old_time = parse_stored_datetime(reminder["datetime"])
            expected_date = (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%d")
            store.add_reminder("session", reminder)
            context = _Context()
            holiday_manager = _HolidayManager(workdays={expected_date})

            TodoReminderScheduler(context, store, holiday_manager, enable_llm_reminder=False)
            await self._drain_pending()

            updated = store.get_session("session")["reminders"][0]
            self.assertEqual(len(context.messages), 0)
            self.assertGreater(parse_stored_datetime(updated["datetime"]), old_time)
            self.assertEqual(parse_stored_datetime(updated["datetime"]).strftime("%Y-%m-%d"), expected_date)

    async def test_missed_workday_save_failure_restores_datetime_without_job(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _FailingSaveStore(Path(temp_dir) / "data.json")
            reminder = make_reminder(
                "上班提醒",
                format_stored_datetime(dt.datetime.now() - dt.timedelta(days=1)),
                holiday_type="workday",
            )
            original_datetime = reminder["datetime"]
            expected_date = (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%d")
            store.add_reminder("session", reminder)
            context = _Context()
            holiday_manager = _HolidayManager(workdays={expected_date})

            scheduler = TodoReminderScheduler(context, store, holiday_manager, enable_llm_reminder=False)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            updated = store.get_session("session")["reminders"][0]
            self.assertEqual(updated["datetime"], original_datetime)
            self.assertEqual(scheduler.scheduler.jobs, {})

    async def test_missed_workday_roll_forward_does_not_overwrite_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            reminder = make_reminder(
                "上班提醒",
                format_stored_datetime(dt.datetime.now() - dt.timedelta(days=1)),
                holiday_type="workday",
            )
            new_datetime = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=3))
            expected_date = (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%d")
            store.add_reminder("session", reminder)
            context = _Context()
            holiday_manager = _EditingHolidayManager(store, reminder, new_datetime=new_datetime, workdays={expected_date})

            scheduler = TodoReminderScheduler(context, store, holiday_manager, enable_llm_reminder=False)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            updated = store.get_session("session")["reminders"][0]
            self.assertEqual(updated["datetime"], new_datetime)
            self.assertEqual(scheduler.scheduler.jobs, {})

    async def test_timezone_change_clears_old_plugin_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            reminder = make_reminder("喝水", "2099-01-01 09:00")
            store.add_reminder("session", reminder)

            first = TodoReminderScheduler(_Context(), store, _HolidayManager(), enable_llm_reminder=False)
            old_scheduler = first.scheduler
            self.assertTrue(old_scheduler.jobs)

            TodoReminderScheduler(
                _Context(),
                TodoReminderStore(Path(temp_dir) / "data.json"),
                _HolidayManager(),
                enable_llm_reminder=False,
                timezone=ZoneInfo("UTC"),
            )

            self.assertEqual(old_scheduler.jobs, {})

    async def test_different_data_files_do_not_clear_each_other_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_store = TodoReminderStore(Path(temp_dir) / "first.json")
            first_reminder = make_reminder("喝水", "2099-01-01 09:00")
            first_store.add_reminder("session", first_reminder)

            first = TodoReminderScheduler(_Context(), first_store, _HolidayManager(), enable_llm_reminder=False)
            old_scheduler = first.scheduler
            self.assertTrue(old_scheduler.jobs)

            second_store = TodoReminderStore(Path(temp_dir) / "second.json")
            second_reminder = make_reminder("散步", "2099-01-01 10:00")
            second_store.add_reminder("session", second_reminder)
            second = TodoReminderScheduler(_Context(), second_store, _HolidayManager(), enable_llm_reminder=False)

            self.assertIn(first_reminder["job_id"], old_scheduler.jobs)
            self.assertIn(second_reminder["job_id"], second.scheduler.jobs)
            second.shutdown()
            self.assertIn(first_reminder["job_id"], old_scheduler.jobs)
            self.assertNotIn(second_reminder["job_id"], second.scheduler.jobs)

    def test_config_parsing_handles_string_values(self):
        config = {
            "reply_in_group": "false",
            "max_items_per_user": "12",
            "timezone": "Invalid/Zone",
        }

        self.assertFalse(parse_bool_config(config, "reply_in_group", True))
        self.assertEqual(parse_int_config(config, "max_items_per_user", 50), 12)
        name, timezone = parse_timezone_config(config)
        self.assertEqual(name, "Asia/Shanghai")
        self.assertEqual(timezone.key, "Asia/Shanghai")

    async def test_multiline_command_tail_gets_actionable_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            commands = TodoReminderCommands(TodoReminderService(store, _DummyScheduler()))
            results = [
                result
                async for result in commands.add_reminder(
                    _Event(),
                    "喝水",
                    "15:22",
                    "/todo",
                    "reminders",
                )
            ]

            self.assertEqual(len(results), 1)
            self.assertIn("一次只发送一条 /todo 命令", results[0])

    async def test_add_todo_multiline_command_tail_gets_actionable_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            commands = TodoReminderCommands(TodoReminderService(store, _DummyScheduler()))
            results = [
                result
                async for result in commands.add_todo(
                    _Event(),
                    "喝水",
                    "/todo",
                    "reminders",
                )
            ]

            self.assertEqual(len(results), 1)
            self.assertIn("一次只发送一条 /todo 命令", results[0])
            self.assertEqual(store.get_session("session")["todos"], [])

    async def test_edit_commands_multiline_command_tail_gets_actionable_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            commands = TodoReminderCommands(TodoReminderService(store, _DummyScheduler()))

            todo_results = [
                result
                async for result in commands.edit_todo(
                    _Event(),
                    "1",
                    "新内容\n/todo reminders",
                )
            ]
            reminder_results = [
                result
                async for result in commands.edit_reminder(
                    _Event(),
                    "1",
                    text="新提醒\n/todo reminders",
                )
            ]

            self.assertEqual(len(todo_results), 1)
            self.assertEqual(len(reminder_results), 1)
            self.assertIn("一次只发送一条 /todo 命令", todo_results[0])
            self.assertIn("一次只发送一条 /todo 命令", reminder_results[0])

    def test_short_id_prefix_does_not_select_first_item(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            first = make_todo("写周报")
            second = make_todo("写月报")
            store.add_todo("session", first)
            store.add_todo("session", second)

            self.assertEqual(store.resolve_todo("session", first["id"][:7]), (None, None))
            self.assertEqual(store.resolve_todo("session", first["id"][:8]), (first, 0))
            self.assertEqual(store.resolve_todo("session", "写周报"), (first, 0))

    async def test_overlong_content_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            service = TodoReminderService(store, _DummyScheduler())
            future = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=1))

            with self.assertRaisesRegex(ValueError, "待办内容不能超过 500 个字符"):
                await service.create_todo("session", "x" * 501)
            with self.assertRaisesRegex(ValueError, "提醒内容不能超过 500 个字符"):
                await service.create_reminder("session", "x" * 501, future, time_already_parsed=True)
            with self.assertRaisesRegex(ValueError, "待办备注不能超过 1000 个字符"):
                await service.create_todo("session", "写周报", notes="x" * 1001)

            session = store.get_session("session")
            self.assertEqual(session["todos"], [])
            self.assertEqual(session["reminders"], [])

    async def test_negative_max_items_uses_default_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            service = TodoReminderService(store, _DummyScheduler(), max_items_per_user=-1)

            for index in range(50):
                await service.create_todo("session", f"任务 {index}")
            with self.assertRaisesRegex(ValueError, "最大条目数限制\\(50\\)"):
                await service.create_todo("session", "第 51 条")

    async def test_llm_bulk_delete_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            scheduler = _DummyScheduler()
            service = TodoReminderService(store, scheduler)
            harness = _PluginHarness(store, service)
            todo = make_todo("写周报")
            reminder = make_reminder("写周报提醒", "2099-01-01 09:00", todo_id=todo["id"])
            store.add_todo("session", todo)
            store.add_reminder("session", reminder)

            result = await harness.delete_all_todos_tool(_Event())
            self.assertIn("confirm=yes", result)
            self.assertEqual(len(store.get_session("session")["todos"]), 1)

            result = await harness.delete_all_todos_tool(_Event(), confirm="yes")
            self.assertIn("已删除所有待办", result)
            self.assertEqual(store.get_session("session")["todos"], [])
            self.assertEqual(store.get_session("session")["reminders"], [])

    async def test_llm_keyword_delete_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            service = TodoReminderService(store, _DummyScheduler())
            harness = _PluginHarness(store, service)
            store.add_reminder("session", make_reminder("喝水", "2099-01-01 09:00"))
            store.add_reminder("session", make_reminder("喝水休息", "2099-01-01 10:00"))

            result = await harness.delete_reminders_by_keyword_tool(_Event(), "喝水")
            self.assertIn("confirm=yes", result)
            self.assertEqual(len(store.get_session("session")["reminders"]), 2)

            result = await harness.delete_reminders_by_keyword_tool(_Event(), "喝水", confirm="yes")
            self.assertIn("数量：2 条", result)
            self.assertEqual(store.get_session("session")["reminders"], [])

    async def test_llm_create_reminder_tool_accepts_relative_delay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            service = TodoReminderService(store, _DummyScheduler())
            harness = _PluginHarness(store, service)

            result = await harness.create_reminder_tool(_Event(), "喝水", delay_minutes="1")

            reminder = store.get_session("session")["reminders"][0]
            self.assertIn("已创建提醒", result)
            self.assertEqual(reminder["text"], "喝水")
            self.assertGreater(parse_stored_datetime(reminder["datetime"]), dt.datetime.now())

    async def test_llm_create_todo_tool_accepts_relative_delay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            service = TodoReminderService(store, _DummyScheduler())
            harness = _PluginHarness(store, service)

            result = await harness.create_todo_tool(_Event(), "写周报", delay_minutes="1")

            session = store.get_session("session")
            self.assertIn("已创建待办", result)
            self.assertEqual(len(session["todos"]), 1)
            self.assertEqual(len(session["reminders"]), 1)
            self.assertEqual(session["reminders"][0]["todo_id"], session["todos"][0]["id"])

    async def test_llm_update_reminder_tool_accepts_relative_delay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            scheduler = _DummyScheduler()
            service = TodoReminderService(store, scheduler)
            harness = _PluginHarness(store, service)
            reminder = make_reminder("喝水", format_stored_datetime(dt.datetime.now() + dt.timedelta(days=1)))
            store.add_reminder("session", reminder)

            result = await harness.update_reminder_tool(_Event(), "1", delay_minutes="1")

            updated = store.get_session("session")["reminders"][0]
            self.assertIn("已更新提醒", result)
            self.assertGreater(parse_stored_datetime(updated["datetime"]), dt.datetime.now())
            self.assertLess(parse_stored_datetime(updated["datetime"]), dt.datetime.now() + dt.timedelta(minutes=3))
            self.assertTrue(scheduler.jobs)

    async def test_llm_relative_delay_tool_returns_validation_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            service = TodoReminderService(store, _DummyScheduler())
            harness = _PluginHarness(store, service)

            result = await harness.create_reminder_tool(_Event(), "喝水", delay_seconds=str(366 * 24 * 60 * 60 + 1))

            self.assertEqual(result, "相对提醒延迟不能超过 366 天。")
            self.assertEqual(store.get_session("session")["reminders"], [])

    async def test_edit_reminder_rejects_stale_schedule_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = TodoReminderStore(Path(temp_dir) / "data.json")
            reminder = make_reminder("喝水", format_stored_datetime(dt.datetime.now() + dt.timedelta(days=1)))
            store.add_reminder("session", reminder)
            scheduler = _EditingPrepareScheduler(reminder, new_text="已被别的操作修改")
            service = TodoReminderService(store, scheduler)
            later = format_stored_datetime(dt.datetime.now() + dt.timedelta(days=2))

            with self.assertRaisesRegex(ValueError, "提醒已被其他操作更新"):
                await service.edit_reminder("session", "1", datetime_str=later, time_already_parsed=True)

            updated = store.get_session("session")["reminders"][0]
            self.assertEqual(updated["text"], "已被别的操作修改")
            self.assertNotEqual(updated["datetime"], later)


if __name__ == "__main__":
    unittest.main()
