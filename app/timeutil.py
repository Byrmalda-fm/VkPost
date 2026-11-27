"""Работа со временем: дни недели, разбор «ЧЧ:ММ», расчёт ближайшей отправки."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone, tzinfo

UTC = timezone.utc
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

WEEKDAYS = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]
WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

_TIME_RE = re.compile(r"^\s*(\d{1,2})\s*[:.\-]\s*(\d{2})\s*$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(ISO_FMT)


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, ISO_FMT).replace(tzinfo=UTC)


def parse_time(s: str | None) -> tuple[int, int] | None:
    """'9:05', '09:05', '9.05' -> (9, 5); при ошибке None."""
    m = _TIME_RE.match(s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


def format_hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def next_occurrence(weekday: int, hour: int, minute: int, tz: tzinfo,
                    now: datetime | None = None) -> datetime:
    """Ближайший момент (UTC) указанного дня недели и времени строго позже `now`."""
    now_local = (now or utcnow()).astimezone(tz)
    days = (weekday - now_local.weekday()) % 7
    day = now_local.date() + timedelta(days=days)
    cand = datetime.combine(day, time(hour, minute), tzinfo=tz)
    if cand <= now_local:
        cand = datetime.combine(day + timedelta(days=7), time(hour, minute), tzinfo=tz)
    return cand.astimezone(UTC)


def latest_daily_boundary(now: datetime, hour: int, minute: int, tz: tzinfo) -> datetime:
    """Последний момент HH:MM (по tz), не позже now. Возвращает UTC."""
    local = now.astimezone(tz)
    b = datetime.combine(local.date(), time(hour, minute), tzinfo=tz)
    if b > local:
        b = datetime.combine(local.date() - timedelta(days=1), time(hour, minute), tzinfo=tz)
    return b.astimezone(UTC)


def window_for_boundary(boundary: datetime, tz: tzinfo) -> tuple[datetime, datetime]:
    """Сутки, заканчивающиеся в boundary: [boundary - 1 день, boundary)."""
    end_local = boundary.astimezone(tz)
    start_local = datetime.combine(
        end_local.date() - timedelta(days=1), end_local.timetz().replace(tzinfo=None), tzinfo=tz
    )
    return start_local.astimezone(UTC), boundary


def fmt_local(iso: str | None, tz: tzinfo, with_year: bool = False) -> str:
    if not iso:
        return "—"
    fmt = "%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M"
    return from_iso(iso).astimezone(tz).strftime(fmt)


def tz_label(tz_name: str) -> str:
    return "МСК" if tz_name == "Europe/Moscow" else tz_name
