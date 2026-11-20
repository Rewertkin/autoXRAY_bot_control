"""Форматирование: объёмы, даты, длительности, прогресс-бар."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

_TZ: ZoneInfo = ZoneInfo("Europe/Moscow")


def set_timezone(name: str) -> None:
    global _TZ
    _TZ = ZoneInfo(name)


def tz() -> ZoneInfo:
    return _TZ


def local_midnight(ts: int | None = None) -> int:
    dt = datetime.fromtimestamp(ts or time.time(), _TZ)
    return int(dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def size(n: int | None) -> str:
    if n is None:
        return "∞"
    value = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.0f} {unit}" if unit in ("Б", "КБ") else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} ТБ"


def dt(ts: int | None, with_year: bool = False) -> str:
    if not ts:
        return "-"
    fmt = "%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M"
    return datetime.fromtimestamp(ts, _TZ).strftime(fmt)


def ago(ts: int | None) -> str:
    if not ts:
        return "никогда"
    sec = int(time.time() - ts)
    if sec < 60:
        return "только что"
    if sec < 3600:
        return f"{sec // 60} мин назад"
    if sec < 86400:
        return f"{sec // 3600} ч назад"
    return f"{sec // 86400} д назад"


def left(ts: int | None) -> str:
    if not ts:
        return "бессрочно"
    sec = int(ts - time.time())
    if sec <= 0:
        return "истёк"
    d, rem = divmod(sec, 86400)
    h = rem // 3600
    if d:
        return f"{d} д {h} ч"
    m = rem % 3600 // 60
    return f"{h} ч {m} мин"


def duration(sec: int) -> str:
    td = timedelta(seconds=max(0, sec))
    h, rem = divmod(int(td.total_seconds()), 3600)
    m = rem // 60
    return f"{h} ч {m} мин" if h else f"{m} мин"


def span(sec: int) -> str:
    """Длительность: 3 д 4 ч, 5 ч 12 мин, 7 мин."""
    sec = max(0, int(sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def bar(used: int, total: int | None, width: int = 10) -> str:
    if not total:
        return ""
    frac = min(1.0, used / total)
    filled = round(frac * width)
    return "▓" * filled + "░" * (width - filled) + f" {frac * 100:.0f}%"


def h(text: str) -> str:
    return escape(str(text), quote=False)
