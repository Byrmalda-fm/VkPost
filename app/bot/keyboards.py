"""Клавиатуры бота."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton as IB, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
)

from ..db import Post
from ..timeutil import WEEKDAYS

BTN_ADD = "➕ Добавить пост"
BTN_LIST = "📋 Активные посты"
BTN_REPORTS = "📊 Отчёты"
PAGE_SIZE = 5
LOG_PAGE_SIZE = 6


def _kb(rows: list[list[IB]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_ADD)],
                  [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_REPORTS)]],
        resize_keyboard=True, input_field_placeholder="GRAY Editor",
    )


CANCEL_ROW = [IB(text="✖️ Отменить", callback_data="fl:cancel")]


def cancel_only() -> InlineKeyboardMarkup:
    return _kb([CANCEL_ROW])


def topic_kb() -> InlineKeyboardMarkup:
    return _kb([[IB(text="⏭ Без темы", callback_data="tp:skip")], CANCEL_ROW])


def weekdays_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, name in enumerate(WEEKDAYS):
        row.append(IB(text=name, callback_data=f"day:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(CANCEL_ROW)
    return _kb(rows)


def repeat_kb() -> InlineKeyboardMarkup:
    return _kb([[IB(text="1️⃣ Один раз", callback_data="rp:0")],
                [IB(text="🔁 Каждую неделю", callback_data="rp:1")], CANCEL_ROW])


def preview_kb() -> InlineKeyboardMarkup:
    return _kb([[IB(text="✅ Подтвердить", callback_data="pv:ok")],
                [IB(text="✏️ Изменить", callback_data="pv:edit"),
                 IB(text="✖️ Отменить", callback_data="fl:cancel")]])


def edit_menu_kb() -> InlineKeyboardMarkup:
    return _kb([
        [IB(text="📝 Текст и фото", callback_data="ed:text"), IB(text="🏷 Тема", callback_data="ed:topic")],
        [IB(text="📅 День", callback_data="ed:day"), IB(text="🔁 Повтор", callback_data="ed:repeat")],
        [IB(text="⬅️ Назад к просмотру", callback_data="ed:back")],
    ])


def list_kb(posts: list[Post], page: int, pages: int) -> InlineKeyboardMarkup:
    rows = []
    for p in posts:
        rows.append([IB(text=f"👁 #{p.id}", callback_data=f"vw:{p.id}"),
                     IB(text=f"✏️ #{p.id}", callback_data=f"ep:{p.id}"),
                     IB(text=f"🗑 #{p.id}", callback_data=f"dl:{p.id}")])
    nav = []
    if page > 0:
        nav.append(IB(text="⬅️", callback_data=f"ls:{page - 1}"))
    nav.append(IB(text=f"{page + 1}/{max(pages, 1)}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(IB(text="➡️", callback_data=f"ls:{page + 1}"))
    rows.append(nav)
    return _kb(rows)


def card_kb(post: Post) -> InlineKeyboardMarkup:
    rows = [[IB(text="✏️ Изменить", callback_data=f"ep:{post.id}"),
             IB(text="🗑 Удалить", callback_data=f"dl:{post.id}")]]
    if post.status in ("error", "unknown", "sent"):
        rows.append([IB(text="🔁 Отправить ещё раз", callback_data=f"rt:{post.id}")])
    rows.append([IB(text="⬅️ К списку", callback_data="ls:0")])
    return _kb(rows)


def confirm_delete_kb(post_id: int) -> InlineKeyboardMarkup:
    return _kb([[IB(text="🗑 Да, удалить", callback_data=f"dly:{post_id}"),
                 IB(text="↩️ Нет", callback_data="ls:0")]])


def confirm_retry_kb(post_id: int) -> InlineKeyboardMarkup:
    return _kb([[IB(text="🔁 Да, отправить", callback_data=f"rty:{post_id}"),
                 IB(text="↩️ Нет", callback_data="ls:0")]])


def reports_kb(page: int, pages: int) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(IB(text="⬅️", callback_data=f"rp_pg:{page - 1}"))
    nav.append(IB(text=f"{page + 1}/{max(pages, 1)}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(IB(text="➡️", callback_data=f"rp_pg:{page + 1}"))
    return _kb([nav, [IB(text="🕐 Сводка за 24 часа", callback_data="rp_day")]])


def vktest_kb() -> InlineKeyboardMarkup:
    return _kb([[IB(text="🧪 Отправить тестовую запись", callback_data="vktest:ok")],
                [IB(text="↩️ Отмена", callback_data="noop")]])

