"""Главное меню, команды и служебные обработчики."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ...config import Config
from ...db import Database
from ...service import Service
from .. import keyboards as kb
from .active import show_list
from .create import discard_draft, start_flow
from .reports import show_reports

router = Router(name="menu")


def welcome(cfg: Config) -> str:
    return (
        "🩶 <b>GRAY Editor</b>\n"
        f"Редактор: <b>{cfg.editor_name}</b>\n\n"
        "Сообщество <b>Black Russia</b> · сервер <b>GRAY</b>\n"
        "Планируйте записи для раздела «Предложить новость» ВКонтакте прямо из Telegram.\n\n"
        "Выберите действие 👇"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, cfg: Config):
    await discard_draft(state)
    await message.answer(welcome(cfg), reply_markup=kb.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message, cfg: Config):
    await message.answer(
        "ℹ️ <b>Как это работает</b>\n"
        "1. «➕ Добавить пост» — пришлите текст (с фото или без), выберите день и время.\n"
        "2. В выбранное время бот отправит запись в «Предложенные» сообщества VK.\n"
        "3. «📋 Активные посты» — просмотр, правка, удаление.\n"
        f"4. «📊 Отчёты» — история отправок; сводка за сутки приходит в "
        f"{cfg.report_hour:02d}:{cfg.report_minute:02d}.\n\n"
        "Служебные команды:\n"
        "/vkcheck — проверить доступ к VK (ничего не публикует)\n"
        "/vktest — отправить тестовую запись в предложенные\n"
        "/start — главное меню",
        reply_markup=kb.main_menu())


@router.message(F.text == kb.BTN_ADD)
async def menu_add(message: Message, state: FSMContext, cfg: Config, svc: Service):
    await start_flow(message, state, cfg, svc)


@router.message(F.text == kb.BTN_LIST)
async def menu_list(message: Message, state: FSMContext, db: Database):
    await discard_draft(state)
    await show_list(message, db, 0, edit=False)


@router.message(F.text == kb.BTN_REPORTS)
async def menu_reports(message: Message, state: FSMContext, db: Database, cfg: Config):
    await discard_draft(state)
    await show_reports(message, db, cfg, 0, edit=False)


@router.message(Command("vkcheck"))
async def cmd_vkcheck(message: Message, svc: Service):
    await message.answer("⏳ Проверяю доступ к VK…")
    await message.answer(await svc.vk_diagnose(), reply_markup=kb.vktest_kb())


@router.message(Command("vktest"))
async def cmd_vktest(message: Message):
    await message.answer(
        "🧪 Будет создана <b>настоящая</b> тестовая запись в «Предложенных» сообщества. "
        "Её нужно будет удалить вручную. Продолжить?", reply_markup=kb.vktest_kb())


@router.callback_query(F.data == "vktest:ok")
async def vktest_do(cb: CallbackQuery, svc: Service):
    await cb.answer()
    await cb.message.answer("⏳ Отправляю тестовую запись…")
    await cb.message.answer(await svc.vk_test())


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()
