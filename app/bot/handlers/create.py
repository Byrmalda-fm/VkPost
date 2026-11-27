"""Сценарий «Добавить пост» (он же редактирование существующего поста)."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ...config import Config
from ...service import Service
from ...timeutil import WEEKDAYS, fmt_local, tz_label
from .. import keyboards as kb
from ..render import card_head, esc, send_card

log = logging.getLogger(__name__)
router = Router(name="create")

MAX_TEXT = 4000


class Flow(StatesGroup):
    material = State()
    topic = State()
    weekday = State()
    repeat = State()
    preview = State()


def new_draft() -> dict[str, Any]:
    return {"topic": None, "text": None, "photo_path": None, "photo_file_id": None,
            "photo_new": False, "weekday": None, "send_time": None, "repeat": False}


def _unlink(path: str | None) -> None:
    if path:
        Path(path).unlink(missing_ok=True)


async def discard_draft(state: FSMContext) -> None:
    """Сбрасывает сценарий и удаляет файл фото, скачанный для черновика."""
    data = await state.get_data()
    draft = data.get("draft")
    if draft and draft.get("photo_new"):
        _unlink(draft.get("photo_path"))
    await state.clear()


# ---------- показ шагов ----------
async def show_step(message: Message, state: FSMContext, step: str, cfg: Config, svc: Service) -> None:
    if step == "material":
        await state.set_state(Flow.material)
        await message.answer(
            "📝 <b>Пришлите материал для поста</b>\n\n"
            "• текст с фотографией — одним сообщением (фото с подписью);\n"
            "• или просто текстовое сообщение без фото.\n\n"
            "<i>Фото отправляйте как изображение (не файлом). Подпись к фото — до 1024 символов.</i>",
            reply_markup=kb.cancel_only())
    elif step == "topic":
        await state.set_state(Flow.topic)
        await message.answer("🏷 <b>Тема поста</b> (необязательно)\nКоротко, до 60 символов — "
                             "она будет видна в списках и отчётах. Пришлите текстом или пропустите.",
                             reply_markup=kb.topic_kb())
    elif step == "weekday":
        await state.set_state(Flow.weekday)
        await message.answer(
            "📅 <b>Выберите день недели</b>\n"
            f"Отправка всегда в <b>{cfg.send_time} ({tz_label(cfg.timezone)})</b>.",
            reply_markup=kb.weekdays_kb())
    elif step == "repeat":
        await state.set_state(Flow.repeat)
        await message.answer("🔁 <b>Как отправлять?</b>\nОдин раз — в ближайший выбранный день, "
                             "или повторять каждую неделю.", reply_markup=kb.repeat_kb())
    elif step == "preview":
        await show_preview(message, state, cfg, svc)


async def show_preview(message: Message, state: FSMContext, cfg: Config, svc: Service) -> None:
    data = await state.get_data()
    d, edit_id = data["draft"], data.get("edit_id")
    await state.set_state(Flow.preview)
    await state.update_data(from_preview=False)
    head = card_head(
        title="👁 Предпросмотр" + (f" · пост #{edit_id}" if edit_id else ""),
        topic=d["topic"], weekday=d["weekday"], send_time=cfg.send_time, repeat=d["repeat"],
        tz_name=cfg.timezone, status=None,
        next_run_at=svc.next_run_for(d["weekday"], cfg.send_time), tz=cfg.tz)
    await send_card(message, head, d["text"], d["photo_file_id"], d["photo_path"], kb.preview_kb())


async def advance(message: Message, state: FSMContext, cfg: Config, svc: Service, step: str) -> None:
    """Переход к следующему шагу; при правке из предпросмотра — сразу обратно в предпросмотр."""
    data = await state.get_data()
    await show_step(message, state, "preview" if data.get("from_preview") else step, cfg, svc)


async def start_flow(message: Message, state: FSMContext, cfg: Config, svc: Service) -> None:
    await discard_draft(state)
    await state.update_data(draft=new_draft(), from_preview=False, edit_id=None)
    await show_step(message, state, "material", cfg, svc)


async def start_edit(message: Message, state: FSMContext, cfg: Config, svc: Service, post) -> None:
    """Загружает существующий пост в черновик и открывает предпросмотр."""
    await discard_draft(state)
    # фото существующего поста лежит в БД; в черновике о нём знает только photo_file_id
    draft = {"topic": post.topic, "text": post.text, "photo_path": None,
             "photo_file_id": post.photo_file_id if post.has_photo else None, "photo_new": False,
             "weekday": post.weekday, "send_time": cfg.send_time, "repeat": post.repeat_weekly}
    await state.update_data(draft=draft, from_preview=False, edit_id=post.id)
    await show_preview(message, state, cfg, svc)


# ---------- шаг 1: материал ----------
@router.message(Flow.material, F.photo)
async def material_photo(message: Message, state: FSMContext, bot: Bot, cfg: Config, svc: Service):
    caption = (message.caption or "").strip()
    if not caption:
        await message.answer("📷 К фотографии нужна подпись с текстом. Отправьте фото и текст "
                             "<b>одним сообщением</b> (фото с подписью).", reply_markup=kb.cancel_only())
        return
    media = Path(cfg.media_dir)
    media.mkdir(parents=True, exist_ok=True)
    dest = media / f"{uuid.uuid4().hex}.jpg"
    photo = message.photo[-1]
    try:
        await bot.download(photo, destination=dest)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось скачать фото из Telegram")
        await message.answer("❌ Не удалось сохранить фото. Попробуйте отправить ещё раз.")
        return
    data = await state.get_data()
    draft = data["draft"]
    if draft.get("photo_new"):
        _unlink(draft.get("photo_path"))
    draft.update(text=caption, photo_path=str(dest), photo_file_id=photo.file_id, photo_new=True)
    await state.update_data(draft=draft)
    await advance(message, state, cfg, svc, "topic")


@router.message(Flow.material, F.text)
async def material_text(message: Message, state: FSMContext, cfg: Config, svc: Service):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст пустой. Пришлите текст поста.")
        return
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинный текст ({len(text)} симв.). Максимум — {MAX_TEXT}.")
        return
    data = await state.get_data()
    draft = data["draft"]
    if draft.get("photo_new"):
        _unlink(draft.get("photo_path"))
    draft.update(text=text, photo_path=None, photo_file_id=None, photo_new=False)
    await state.update_data(draft=draft)
    await advance(message, state, cfg, svc, "topic")


# ---------- шаг 2: тема ----------
async def _set_topic(message: Message, state: FSMContext, cfg: Config, svc: Service, topic: str | None):
    data = await state.get_data()
    draft = data["draft"]
    draft["topic"] = topic
    await state.update_data(draft=draft)
    await advance(message, state, cfg, svc, "weekday")


@router.message(Flow.topic, F.text)
async def topic_text(message: Message, state: FSMContext, cfg: Config, svc: Service):
    topic = (message.text or "").strip()[:60] or None
    await _set_topic(message, state, cfg, svc, topic)


@router.callback_query(Flow.topic, F.data == "tp:skip")
async def topic_skip(cb: CallbackQuery, state: FSMContext, cfg: Config, svc: Service):
    await cb.answer()
    await _set_topic(cb.message, state, cfg, svc, None)


# ---------- шаг 3: день ----------
@router.callback_query(Flow.weekday, F.data.regexp(r"^day:[0-6]$"))
async def pick_day(cb: CallbackQuery, state: FSMContext, cfg: Config, svc: Service):
    await cb.answer(WEEKDAYS[int(cb.data.split(":")[1])])
    data = await state.get_data()
    draft = data["draft"]
    draft["weekday"] = int(cb.data.split(":")[1])
    draft["send_time"] = cfg.send_time
    await state.update_data(draft=draft)
    await advance(cb.message, state, cfg, svc, "repeat")


# ---------- шаг 4: повтор ----------
@router.callback_query(Flow.repeat, F.data.in_({"rp:0", "rp:1"}))
async def pick_repeat(cb: CallbackQuery, state: FSMContext, cfg: Config, svc: Service):
    await cb.answer()
    data = await state.get_data()
    draft = data["draft"]
    draft["repeat"] = cb.data == "rp:1"
    await state.update_data(draft=draft)
    await advance(cb.message, state, cfg, svc, "preview")


# ---------- предпросмотр ----------
@router.callback_query(Flow.preview, F.data == "pv:ok")
async def confirm(cb: CallbackQuery, state: FSMContext, cfg: Config, svc: Service):
    data = await state.get_data()
    d, edit_id = data["draft"], data.get("edit_id")
    await state.clear()  # сразу: двойное нажатие не создаст дубль
    await cb.answer()
    try:
        if edit_id:
            post = await svc.update_post(edit_id, d)
            if post is None:
                await cb.message.answer("⚠️ Пост сейчас отправляется или уже удалён — изменения не сохранены.",
                                        reply_markup=kb.main_menu())
                return
            title = f"✅ Пост #{post.id} обновлён"
        else:
            post = await svc.create_post(d)
            title = f"✅ Пост #{post.id} сохранён"
    except Exception:  # noqa: BLE001
        log.exception("Не удалось сохранить пост")
        await cb.message.answer("❌ Не удалось сохранить пост. Попробуйте ещё раз.", reply_markup=kb.main_menu())
        return
    finally:
        # фото уже лежит в БД (или пост не сохранён) — временный файл больше не нужен
        if d.get("photo_new"):
            _unlink(d.get("photo_path"))
    await cb.message.answer(
        f"{title}\n📅 {WEEKDAYS[post.weekday]} · ⏰ {post.send_time} ({tz_label(cfg.timezone)})\n"
        f"Ближайшая отправка: <b>{fmt_local(post.next_run_at, cfg.tz)}</b>"
        f"{' · 🔁 каждую неделю' if post.repeat_weekly else ''}",
        reply_markup=kb.main_menu())


@router.callback_query(Flow.preview, F.data == "pv:edit")
async def preview_edit(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("✏️ <b>Что изменить?</b>", reply_markup=kb.edit_menu_kb())


@router.callback_query(Flow.preview, F.data.regexp(r"^ed:(text|topic|day|repeat|back)$"))
async def edit_field(cb: CallbackQuery, state: FSMContext, cfg: Config, svc: Service):
    await cb.answer()
    field = cb.data.split(":")[1]
    if field == "back":
        await show_preview(cb.message, state, cfg, svc)
        return
    await state.update_data(from_preview=True)
    step = {"text": "material", "topic": "topic", "day": "weekday", "repeat": "repeat"}[field]
    await show_step(cb.message, state, step, cfg, svc)


# ---------- отмена ----------
@router.callback_query(F.data == "fl:cancel")
async def cancel(cb: CallbackQuery, state: FSMContext):
    await discard_draft(state)
    await cb.answer("Отменено")
    await cb.message.answer("✖️ Отменено. Выберите действие в меню.", reply_markup=kb.main_menu())
