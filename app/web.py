"""Мини-HTTP-сервер для Render и самопроверка.

Бесплатный Web Service на Render обязан слушать порт $PORT и засыпает через 15 минут без
входящих HTTP-запросов. Поэтому:
  * /health (и /) отвечает «ok» и НЕ обращается к базе (чтобы не будить Postgres зря);
  * `self_ping` раз в несколько минут обращается к собственному публичному адресу, пока сервис бодрствует.
Если сервис всё же уснул, разбудить его может только внешний запрос — настройте пинг (см. README).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

PING_INTERVAL_SECONDS = 4 * 60


async def _health(request: Any) -> Any:
    from aiohttp import web
    return web.Response(text="ok", content_type="text/plain")


def build_app() -> Any:
    from aiohttp import web
    app = web.Application()
    app.router.add_route("*", "/", _health)
    app.router.add_route("*", "/health", _health)
    return app


async def start_web(port: int, host: str = "0.0.0.0") -> Any:
    """Запускает сервер и возвращает runner (его нужно закрыть `await runner.cleanup()`)."""
    from aiohttp import web
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("HTTP /health слушает порт %s", port)
    return runner


async def self_ping(url: str, interval: float = PING_INTERVAL_SECONDS) -> None:
    """Периодически обращается к своему публичному адресу, чтобы Render не усыплял сервис."""
    import aiohttp
    target = url.rstrip("/") + "/health"
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(interval)
            try:
                async with session.get(target) as r:
                    log.debug("self-ping %s -> %s", target, r.status)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("self-ping не удался: %s", type(e).__name__)
