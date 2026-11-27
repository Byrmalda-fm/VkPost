"""Точка входа: python -m app.main"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta

from . import redact
from .config import Config, ConfigError
from .db import Database
from .service import Service
from .vk import AiohttpTransport, VKClient
from .web import self_ping, start_web

log = logging.getLogger("gray_editor")


async def run() -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import BotCommand
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger

    from .bot.handlers import active, create, fallback, menu, reports
    from .bot.middleware import OwnerOnlyMiddleware

    cfg = Config.from_env()
    redact.register(cfg.bot_token, cfg.vk_user_token, cfg.vk_admin_token)
    redact.setup_logging(cfg.log_level)

    # Render проверяет, что сервис слушает порт, — поднимаем HTTP первым делом, до БД и Telegram
    web_runner = await start_web(cfg.port) if cfg.port else None
    ping_task = (asyncio.create_task(self_ping(cfg.self_ping_url))
                 if cfg.port and cfg.self_ping_url else None)

    db = Database(cfg.db_path, url=cfg.database_url)
    await db.open()
    log.info("База данных: %s", "PostgreSQL" if cfg.database_url else f"SQLite ({cfg.db_path})")

    transport = AiohttpTransport()
    vk = VKClient(user_token=cfg.vk_user_token, group_id=cfg.vk_group_id,
                  api_version=cfg.vk_api_version, admin_token=cfg.vk_admin_token,
                  allow_open_wall=cfg.vk_allow_open_wall, transport=transport)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def notify(text: str) -> bool:
        try:
            await bot.send_message(cfg.owner_id, text)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить уведомление владельцу")
            return False

    svc = Service(db, vk, cfg, notify)

    dp = Dispatcher(storage=MemoryStorage(), cfg=cfg, db=db, svc=svc)
    owner_only = OwnerOnlyMiddleware(cfg.owner_id)
    dp.message.outer_middleware(owner_only)
    dp.callback_query.outer_middleware(owner_only)
    # порядок важен: меню → сценарий → список → отчёты → запасные
    dp.include_routers(menu.router, create.router, active.router, reports.router, fallback.router)

    scheduler = AsyncIOScheduler(timezone=cfg.timezone)
    # Посты уходят в одно время (SEND_TIME, по умолчанию 11:00 МСК), поэтому базу опрашиваем только
    # в окне после этого времени (2 часа ≈ MISSED_GRACE_MINUTES) — вне окна Postgres может «спать».
    # Всё, что не отправилось из-за простоя сервиса, подхватывает проверка при старте.
    h = cfg.send_hour
    window_hours = f"{h},{(h + 1) % 24}"
    scheduler.add_job(svc.tick,
                      CronTrigger(hour=window_hours, minute="*", second=f"*/{cfg.tick_seconds}",
                                  timezone=cfg.timezone),
                      id="tick_window", max_instances=1, coalesce=True, misfire_grace_time=60)
    scheduler.add_job(svc.daily_report,
                      CronTrigger(hour=cfg.report_hour, minute=cfg.report_minute, timezone=cfg.timezone),
                      id="daily_report", max_instances=1, coalesce=True, misfire_grace_time=3600)
    scheduler.add_job(svc.backup, CronTrigger(hour=3, minute=30, timezone=cfg.timezone),
                      id="backup", max_instances=1, coalesce=True, misfire_grace_time=3600)

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="help", description="Как пользоваться"),
            BotCommand(command="vkcheck", description="Проверить доступ к VK"),
            BotCommand(command="vktest", description="Тестовая запись в предложку"),
        ])
        await svc.startup()          # восстановление после рестарта, догоняющий отчёт
        scheduler.add_job(svc.tick, DateTrigger(run_date=datetime.now(cfg.tz) + timedelta(seconds=5)),
                          id="tick_startup", max_instances=1, misfire_grace_time=60)
        scheduler.start()            # расписание берётся из БД на каждом тике
        log.info("GRAY Editor запущен (часовой пояс %s)", cfg.timezone)
        if cfg.startup_notify:
            await notify("🩶 <b>GRAY Editor запущен</b>. Расписание восстановлено из базы.")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        if ping_task is not None:
            ping_task.cancel()
        await transport.close()
        await bot.session.close()
        await db.close()
        if web_runner is not None:
            await web_runner.cleanup()


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        sys.exit(2)
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
