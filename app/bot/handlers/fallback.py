"""Запасные обработчики. Подключаются последними."""
from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb

router = Router(name="fallback")


@router.callback_query()
async def stale_button(cb: CallbackQuery):
    await cb.answer("Эта кнопка устарела. Откройте меню и начните заново.", show_alert=True)


@router.message()
async def unexpected(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        await message.answer("🤔 Сейчас я жду другое. Следуйте подсказке выше или нажмите «✖️ Отменить».")
    else:
        await message.answer("Выберите действие в меню 👇", reply_markup=kb.main_menu())
