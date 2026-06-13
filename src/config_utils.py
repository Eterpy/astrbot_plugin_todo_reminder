from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger


DEFAULT_TIMEZONE = "Asia/Shanghai"


def get_config_value(config: Any, key: str, default: Any = None) -> Any:
    try:
        return config.get(key, default)
    except AttributeError:
        return default


def parse_int_config(config: Any, key: str, default: int) -> int:
    value = get_config_value(config, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(f"配置项 {key}={value!r} 不是合法整数，使用默认值 {default}")
        return default


def parse_bool_config(config: Any, key: str, default: bool) -> bool:
    value = get_config_value(config, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "开启", "是"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "关闭", "否"}:
            return False
    logger.warning(f"配置项 {key}={value!r} 不是合法布尔值，使用默认值 {default}")
    return default


def parse_text_config(config: Any, key: str, default: str = "") -> str:
    value = get_config_value(config, key, default)
    if value is None:
        return default
    return str(value)


def parse_timezone_config(config: Any, key: str = "timezone", default: str = DEFAULT_TIMEZONE) -> tuple[str, ZoneInfo]:
    value = parse_text_config(config, key, default).strip() or default
    try:
        return value, ZoneInfo(value)
    except ZoneInfoNotFoundError:
        logger.warning(f"配置项 {key}={value!r} 不是合法时区，使用默认值 {default}")
        try:
            return default, ZoneInfo(default)
        except ZoneInfoNotFoundError as exc:
            message = f"默认时区 {default!r} 不可用，请安装 tzdata 或配置有效 IANA 时区。"
            logger.error(message)
            raise RuntimeError(message) from exc
