from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger


class HolidayManager:
    FAILURE_CACHE_MINUTES = 30
    MAX_RESPONSE_BYTES = 1_000_000
    REQUEST_TIMEOUT_SECONDS = 10

    def __init__(self, cache_file: Path, cache_days: int = 30):
        self.cache_file = Path(cache_file)
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_days = max(cache_days, 1)
        self.cache: dict[str, Any] = self._load_cache()

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_file.exists():
            return {}
        try:
            with self.cache_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning(f"加载节假日缓存失败: {exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save_cache(self) -> None:
        tmp_path = self.cache_file.with_name(f".{self.cache_file.name}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(self.cache_file)

    async def fetch_year(self, year: int) -> dict[str, bool]:
        year_key = str(year)
        cached = self.cache.get(year_key, {})
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("data"), dict)
            and self._is_fresh(cached.get("cached_at"))
        ):
            return cached["data"]
        if self._has_recent_failure(year_key):
            return {}

        try:
            url = f"https://timor.tech/api/holiday/year/{year}"
            timeout = aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"获取节假日数据失败，状态码: {response.status}")
                        self._record_failure(year_key)
                        return {}
                    raw = await response.read()
            if len(raw) > self.MAX_RESPONSE_BYTES:
                logger.warning(f"节假日数据响应过大（{len(raw)} 字节），已丢弃")
                self._record_failure(year_key)
                return {}
            payload = json.loads(raw)
        except Exception as exc:
            logger.warning(f"获取节假日数据失败，使用周末兜底: {exc}")
            self._record_failure(year_key)
            return {}

        if payload.get("code") != 0:
            logger.warning(f"获取节假日数据失败: {payload.get('msg')}")
            self._record_failure(year_key)
            return {}

        result: dict[str, bool] = {}
        for date_key, info in payload.get("holiday", {}).items():
            if isinstance(info, dict):
                result[date_key] = bool(info.get("holiday"))
        self.cache[year_key] = {"data": result, "cached_at": dt.datetime.now().isoformat()}
        try:
            self._save_cache()
        except Exception as exc:
            logger.warning(f"保存节假日缓存失败: {exc}")
        return result

    def _is_fresh(self, cached_at: Any) -> bool:
        if not cached_at:
            return False
        try:
            cached_time = dt.datetime.fromisoformat(cached_at)
        except (TypeError, ValueError):
            return False
        return (dt.datetime.now() - cached_time).days <= self.cache_days

    def _has_recent_failure(self, year_key: str) -> bool:
        cached = self.cache.get(year_key, {})
        if not isinstance(cached, dict):
            return False
        failed_at = cached.get("failed_at")
        if not failed_at:
            return False
        try:
            failed_time = dt.datetime.fromisoformat(failed_at)
        except ValueError:
            return False
        return dt.datetime.now() - failed_time < dt.timedelta(minutes=self.FAILURE_CACHE_MINUTES)

    def _record_failure(self, year_key: str) -> None:
        self.cache[year_key] = {"failed_at": dt.datetime.now().isoformat()}
        try:
            self._save_cache()
        except Exception as exc:
            logger.warning(f"保存节假日失败缓存失败: {exc}")

    async def is_holiday(self, value: dt.datetime | None = None) -> bool:
        date = value or dt.datetime.now()
        holiday_data = await self.fetch_year(date.year)
        short = date.strftime("%m-%d")
        if short in holiday_data:
            return holiday_data[short] is True
        return date.weekday() >= 5

    async def is_workday(self, value: dt.datetime | None = None) -> bool:
        date = value or dt.datetime.now()
        holiday_data = await self.fetch_year(date.year)
        short = date.strftime("%m-%d")
        if short in holiday_data:
            return holiday_data[short] is False
        return date.weekday() < 5
