"""Раздел «Отчёты»."""
from __future__ import annotations

import math

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...config import Config
from ...db import Database
from ...reporting import format_log_entry
from ...service import Service
from .. import keyboards as kb

router = Router(name="reports")


async def build_reports(db: Database, cfg: Config, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    total = await db.count_logs()
    if total == 0:
        return "📊 <b>Отчёты</b>\n\nОтправок пока не было.", None
    pages = max(1, math.ceil(total / kb.LOG_PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    entries = await db.list_logs(kb.LOG_PAGE_SIZE, page * kb.LOG_PAGE_SIZE)
    body = "\n\n".join(format_log_entry(e, cfg.tz) for e in entries)
    text = (f"📊 <b>Отчёты</b> · всего отправок: {total}\n"
            f"<i>Новые — сверху. Ежедневный отчёт приходит в "
            f"{cfg.report_hour:02d}:{cfg.report_minute:02d}.</i>\n\n{body}")
    return text, kb.reports_kb(page, pages)


async def show_reports(message: Message, db: Database, cfg: Config, page: int, edit: bool) -> None:
    text, markup = await build_reports(db, cfg, page)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^rp_pg:\d+$"))
async def reports_page(cb: CallbackQuery, db: Database, cfg: Config):
    await cb.answer()
    await show_reports(cb.message, db, cfg, int(cb.data.split(":")[1]), edit=True)


@router.callback_query(F.data == "rp_day")
async def reports_day(cb: CallbackQuery, svc: Service):
    await cb.answer()
    await cb.message.answer(await svc.day_report_text())
