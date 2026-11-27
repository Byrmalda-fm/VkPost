"""Раздел «Активные посты»: список, просмотр, правка, удаление, повторная отправка."""
from __future__ import annotations

import logging
import math

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ...config import Config
from ...db import Database
from ...service import Service
from ...timeutil import WEEKDAYS_SHORT
from .. import keyboards as kb
from ..render import STATUS_SHORT, esc, post_head, send_card
from .create import discard_draft, start_edit

log = logging.getLogger(__name__)
router = Router(name="active")


async def build_list(db: Database, page: int) -> tuple[str, InlineKeyboardMarkup | None, int]:
    total = await db.count_posts()
    pages = max(1, math.ceil(total / kb.PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    if total == 0:
        return "📋 <b>Активные посты</b>\n\nПока нет ни одного поста. Нажмите «➕ Добавить пост».", None, 0
    posts = await db.list_posts(kb.PAGE_SIZE, page * kb.PAGE_SIZE)
    lines = [f"📋 <b>Активные посты</b> · всего {total}\n"]
    for p in posts:
        flags = (" 🔁" if p.repeat_weekly else "") + (" 📷" if p.has_photo else "")
        lines.append(f"{STATUS_SHORT.get(p.status, '•')} <b>#{p.id}</b> · {WEEKDAYS_SHORT[p.weekday]} "
                     f"{p.send_time}{flags}\n      {esc(p.preview)}")
        if p.last_error and p.status != "sending":
            lines.append(f"      ⚠️ {esc(p.last_error[:90])}")
    lines.append("\n⏳ ожидает · ✅ отправлено · ❌ ошибка · ❓ неизвестно")
    return "\n".join(lines), kb.list_kb(posts, page, pages), page


async def _edit_or_answer(message: Message, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:  # сообщение с фото или текст не изменился
        await message.answer(text, reply_markup=markup)


async def show_list(message: Message, db: Database, page: int, edit: bool) -> None:
    text, markup, _ = await build_list(db, page)
    if edit:
        await _edit_or_answer(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.regexp(r"^ls:\d+$"))
async def list_page(cb: CallbackQuery, db: Database):
    await cb.answer()
    await show_list(cb.message, db, int(cb.data.split(":")[1]), edit=True)


@router.callback_query(F.data.regexp(r"^vw:\d+$"))
async def view_post(cb: CallbackQuery, db: Database, cfg: Config):
    post = await db.get_post(int(cb.data.split(":")[1]))
    if post is None:
        await cb.answer("Пост не найден", show_alert=True)
        return
    await cb.answer()
    await send_card(cb.message, post_head(post, cfg), post.text, post.photo_file_id if post.has_photo else None,
                    None, kb.card_kb(post))


@router.callback_query(F.data.regexp(r"^ep:\d+$"))
async def edit_post(cb: CallbackQuery, state: FSMContext, db: Database, cfg: Config, svc: Service):
    post = await db.get_post(int(cb.data.split(":")[1]))
    if post is None:
        await cb.answer("Пост не найден", show_alert=True)
        return
    if post.status == "sending":
        await cb.answer("Пост отправляется прямо сейчас — подождите минуту", show_alert=True)
        return
    await cb.answer()
    await start_edit(cb.message, state, cfg, svc, post)


@router.callback_query(F.data.regexp(r"^dl:\d+$"))
async def delete_ask(cb: CallbackQuery, db: Database):
    post = await db.get_post(int(cb.data.split(":")[1]))
    if post is None:
        await cb.answer("Пост не найден", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(f"🗑 Удалить пост <b>#{post.id}</b> «{esc(post.preview)}»?\n"
                            "История отправок сохранится.", reply_markup=kb.confirm_delete_kb(post.id))


@router.callback_query(F.data.regexp(r"^dly:\d+$"))
async def delete_do(cb: CallbackQuery, db: Database, svc: Service):
    pid = int(cb.data.split(":")[1])
    ok = await svc.delete_post(pid)
    await cb.answer("Удалено" if ok else "Пост уже удалён")
    await show_list(cb.message, db, 0, edit=True)


@router.callback_query(F.data.regexp(r"^rt:\d+$"))
async def retry_ask(cb: CallbackQuery, db: Database):
    post = await db.get_post(int(cb.data.split(":")[1]))
    if post is None:
        await cb.answer("Пост не найден", show_alert=True)
        return
    await cb.answer()
    warn = ""
    if post.status in ("unknown", "sent"):
        warn = "\n\n⚠️ Сначала проверьте «Предложенные» в VK: возможно, запись уже там, и будет дубль."
    await cb.message.answer(f"🔁 Отправить пост <b>#{post.id}</b> ещё раз прямо сейчас?{warn}",
                            reply_markup=kb.confirm_retry_kb(post.id))


@router.callback_query(F.data.regexp(r"^rty:\d+$"))
async def retry_do(cb: CallbackQuery, svc: Service, cfg: Config):
    ok = await svc.retry_now(int(cb.data.split(":")[1]))
    if not ok:
        await cb.answer("Нельзя повторить этот пост", show_alert=True)
        return
    await cb.answer()
    svc.kick()  # не ждём расписания: проверка запускается сразу
    await cb.message.answer("📤 Пост поставлен в очередь — отправка начинается прямо сейчас. "
                            "Результат придёт сообщением.")
