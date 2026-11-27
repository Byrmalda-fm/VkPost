"""Тексты карточек постов и отправка карточек с фото."""
from __future__ import annotations

import html
import logging
import os
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message

from ..db import Post
from ..timeutil import WEEKDAYS, fmt_local, tz_label

log = logging.getLogger(__name__)

STATUS_ICON = {
    "pending": "⏳ Ожидает отправки",
    "sending": "📤 Отправляется",
    "sent": "✅ Отправлено",
    "error": "❌ Ошибка",
    "unknown": "❓ Результат неизвестен",
}
STATUS_SHORT = {"pending": "⏳", "sending": "📤", "sent": "✅", "error": "❌", "unknown": "❓"}


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def card_head(*, title: str, topic: str | None, weekday: int, send_time: str, repeat: bool,
              tz_name: str, status: str | None = None, next_run_at: str | None = None,
              last_error: str | None = None, tz: Any = None) -> str:
    lines = [f"<b>{title}</b>"]
    lines.append(f"📅 {WEEKDAYS[weekday]} · ⏰ {send_time} ({tz_label(tz_name)})"
                 f"{' · 🔁 каждую неделю' if repeat else ' · один раз'}")
    if topic:
        lines.append(f"🏷 Тема: {esc(topic)}")
    if status:
        lines.append(f"Статус: {STATUS_ICON.get(status, status)}")
    if next_run_at and tz is not None and status in (None, "pending"):
        lines.append(f"Ближайшая отправка: {fmt_local(next_run_at, tz)}")
    if last_error:
        lines.append(f"⚠️ {esc(last_error)}")
    return "\n".join(lines)


def post_head(post: Post, cfg, title: str | None = None) -> str:
    return card_head(title=title or f"Пост #{post.id}", topic=post.topic, weekday=post.weekday,
                     send_time=post.send_time, repeat=post.repeat_weekly, tz_name=cfg.timezone,
                     status=post.status, next_run_at=post.next_run_at, last_error=post.last_error,
                     tz=cfg.tz)


async def send_card(target: Message, head: str, text: str, photo_file_id: str | None,
                    photo_path: str | None, kb: InlineKeyboardMarkup | None) -> None:
    """Показывает карточку поста: с фото (если есть) и кнопками."""
    body = f"{head}\n\n──────────\n{esc(text)}"
    if photo_file_id or (photo_path and os.path.exists(photo_path)):
        sources: list[Any] = []
        if photo_file_id:
            sources.append(photo_file_id)
        if photo_path and os.path.exists(photo_path):
            sources.append(FSInputFile(photo_path))
        last_exc: Exception | None = None
        for src in sources:
            try:
                if len(body) <= 1024:
                    await target.answer_photo(src, caption=body, reply_markup=kb)
                else:
                    await target.answer_photo(src)
                    await target.answer(body[:4096], reply_markup=kb)
                return
            except TelegramBadRequest as e:
                last_exc = e
                log.warning("Не удалось показать фото (%s), пробую запасной вариант", e)
        await target.answer("⚠️ Фото не удалось показать.\n\n" + body[:4000], reply_markup=kb)
        if last_exc:
            log.error("Фото недоступно: %s", last_exc)
        return
    await target.answer(body[:4096], reply_markup=kb)
