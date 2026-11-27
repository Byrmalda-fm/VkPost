"""Доступ только для владельца (по Telegram user_id)."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject

log = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int):
        self.owner_id = owner_id

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.owner_id:
            log.warning("Отказано в доступе: user_id=%s", getattr(user, "id", None))
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ Нет доступа", show_alert=True)
            return None  # сообщения чужих пользователей молча игнорируются
        return await handler(event, data)
