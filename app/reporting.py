"""Форматирование отчётов (чистые функции, HTML для Telegram)."""
from __future__ import annotations

import html
from datetime import datetime, tzinfo

from .db import LogEntry
from .timeutil import fmt_local, to_iso

RESULT_ICON = {
    "confirmed": "✅", "unconfirmed": "⚠️", "error": "❌",
    "missed": "⏭", "unknown": "❓", "sending": "📤",
}
RESULT_TEXT = {
    "confirmed": "Предложена, VK подтвердил",
    "unconfirmed": "VK принял запрос, но подтвердить не удалось",
    "error": "Не отправлено",
    "missed": "Пропущено",
    "unknown": "Результат неизвестен — проверьте предложку VK",
    "sending": "Отправляется",
}


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def format_log_entry(e: LogEntry, tz: tzinfo) -> str:
    icon = RESULT_ICON.get(e.result, "•")
    when = fmt_local(e.finished_at or e.started_at, tz)
    lines = [f"{icon} <b>{when}</b> · {esc(e.preview)}"]
    detail = RESULT_TEXT.get(e.result, e.result)
    if e.vk_post_id and e.result in ("confirmed", "unconfirmed"):
        detail += f" (id {e.vk_post_id})"
    lines.append(f"   ↳ {detail}")
    if e.error and e.result not in ("confirmed",):
        lines.append(f"   ↳ Причина: {esc(e.error)}")
    return "\n".join(lines)


def format_report(entries: list[LogEntry], start: datetime, end: datetime, tz: tzinfo,
                  title: str = "Отчёт за сутки", max_entries: int = 25) -> str:
    period = f"{fmt_local(to_iso(start), tz)} → {fmt_local(to_iso(end), tz)}"
    head = f"📊 <b>{title}</b>\n<i>{period}</i>\n"
    if not entries:
        return head + "\nОтправок не было."
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.result] = counts.get(e.result, 0) + 1
    summary = " · ".join(f"{RESULT_ICON[k]} {v}" for k, v in counts.items())
    body = "\n\n".join(format_log_entry(e, tz) for e in entries[:max_entries])
    tail = f"\n\n…и ещё {len(entries) - max_entries}" if len(entries) > max_entries else ""
    return f"{head}\nИтого: {summary}\n\n{body}{tail}"
