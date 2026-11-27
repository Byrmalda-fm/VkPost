"""Бизнес-логика: создание постов, планировщик отправки, отчёты, диагностика VK."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import redact
from .config import Config
from .db import Database, Post
from .reporting import esc, format_report
from .timeutil import (
    from_iso, fmt_local, latest_daily_boundary, next_occurrence, parse_time, to_iso, utcnow,
    window_for_boundary,
)
from .vk import SendResult, VKAmbiguousError, VKClient, VKError

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[bool]]


class Service:
    def __init__(self, db: Database, vk: VKClient, cfg: Config, notify: Notify,
                 now: Callable[[], datetime] = utcnow):
        self.db, self.vk, self.cfg, self.notify, self._now = db, vk, cfg, notify, now
        self._tick_lock = asyncio.Lock()
        self._report_lock = asyncio.Lock()
        self._bg: set[asyncio.Future] = set()

    # ---------- посты ----------
    def _next_run(self, weekday: int, send_time: str) -> str:
        hm = parse_time(send_time)
        assert hm is not None
        return to_iso(next_occurrence(weekday, hm[0], hm[1], self.cfg.tz, self._now()))

    def next_run_for(self, weekday: int, send_time: str) -> str:
        return self._next_run(weekday, send_time)

    def _advance(self, post: Post) -> str:
        """Следующее вхождение для еженедельного поста (строго после текущего момента)."""
        return self._next_run(post.weekday, post.send_time)

    @staticmethod
    def _read_photo(path: str | None) -> bytes | None:
        """Фото из временного файла черновика -> байты для хранения в БД."""
        if not path:
            return None
        return Path(path).read_bytes()

    async def create_post(self, d: dict[str, Any]) -> Post:
        send_time = self.cfg.send_time  # время отправки единое для всех постов (по умолчанию 11:00 МСК)
        post_id = await self.db.create_post(
            topic=d.get("topic"), text=d["text"], photo_data=self._read_photo(d.get("photo_path")),
            photo_file_id=d.get("photo_file_id"), weekday=d["weekday"], send_time=send_time,
            repeat_weekly=bool(d.get("repeat")), next_run_at=self._next_run(d["weekday"], send_time),
        )
        post = await self.db.get_post(post_id)
        assert post is not None
        return post

    async def update_post(self, post_id: int, d: dict[str, Any]) -> Post | None:
        """Сохраняет правки; пост возвращается в очередь (pending) с новым временем."""
        send_time = self.cfg.send_time
        fields: dict[str, Any] = {
            "topic": d.get("topic"), "text": d["text"],
            "photo_file_id": d.get("photo_file_id"), "weekday": d["weekday"],
            "send_time": send_time, "repeat_weekly": bool(d.get("repeat")),
            "status": "pending", "last_error": None,
            "next_run_at": self._next_run(d["weekday"], send_time),
        }
        if d.get("photo_new") and d.get("photo_path"):
            fields["photo_data"] = self._read_photo(d["photo_path"])   # загружено новое фото
        elif not d.get("photo_file_id"):
            fields["photo_data"] = None                                 # фото убрали
        # иначе фото не менялось — байты в БД остаются как были
        ok = await self.db.update_post(post_id, fields)
        return await self.db.get_post(post_id) if ok else None

    async def retry_now(self, post_id: int) -> bool:
        post = await self.db.get_post(post_id)
        if post is None or post.status not in ("error", "unknown", "sent"):
            return False
        return await self.db.update_post(post_id, {
            "status": "pending", "next_run_at": to_iso(self._now()), "last_error": None,
        })

    async def delete_post(self, post_id: int) -> bool:
        return await self.db.delete_post(post_id)

    def kick(self) -> None:
        """Запускает проверку расписания «прямо сейчас» в фоне (например, после «Отправить ещё раз»)."""
        task = asyncio.ensure_future(self.tick())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # ---------- отправка ----------
    async def tick(self) -> None:
        """Периодическая задача планировщика: отправить всё, что созрело."""
        async with self._tick_lock:
            claimed = await self.db.claim_due(to_iso(self._now()))
            for post, log_id in claimed:
                try:
                    await self._process(post, log_id)
                except Exception:  # noqa: BLE001
                    log.exception("Сбой обработки поста %s", post.id)

    async def _process(self, post: Post, log_id: int) -> None:
        now = self._now()
        scheduled = from_iso(post.next_run_at)
        grace = timedelta(minutes=self.cfg.missed_grace_minutes)
        vk_post_id: int | None = None
        note = ""

        if now - scheduled > grace:
            result = "missed"
            error = (f"Время отправки ({fmt_local(post.next_run_at, self.cfg.tz)}) прошло, пока "
                     f"сервер был недоступен; запись не отправлена")
        else:
            result, error = "error", None
            try:
                photo = await self.db.get_photo(post.id) if post.has_photo else None
                res: SendResult = await self.vk.suggest_post(post.text, photo)
                vk_post_id, note = res.post_id, res.note
                if res.state == "confirmed":
                    result = "confirmed"
                elif res.state == "unconfirmed":
                    result, error = "unconfirmed", note
                else:  # direct
                    result = "error"
                    error = (f"ВНИМАНИЕ: {note}. Запись уже опубликована на стене (id {res.post_id}) — "
                             f"удалите её вручную")
            except VKAmbiguousError as e:
                result, error = "unknown", str(e)
            except VKError as e:
                result, error = "error", str(e)
            except Exception as e:  # noqa: BLE001
                log.exception("Неожиданная ошибка отправки поста %s", post.id)
                result, error = "error", redact.clean(f"Внутренняя ошибка: {type(e).__name__}: {e}")[:300]

        next_run = self._advance(post) if post.repeat_weekly else None
        await self.db.finish_send(log_id=log_id, post_id=post.id, result=result,
                                  vk_post_id=vk_post_id, error=error, next_run_at=next_run)
        await self.notify(self._result_message(post, result, vk_post_id, error, note))

    @staticmethod
    def _result_message(post: Post, result: str, vk_post_id: int | None,
                        error: str | None, note: str) -> str:
        title = esc(post.preview)
        if result == "confirmed":
            return f"✅ <b>Отправлено в предложенные</b>\n{title}\nVK id: {vk_post_id} · {esc(note)}"
        if result == "unconfirmed":
            return (f"⚠️ <b>VK принял запись, но подтвердить не удалось</b>\n{title}\n"
                    f"VK id: {vk_post_id}. Проверьте раздел «Предложенные» в сообществе.")
        if result == "unknown":
            return (f"❓ <b>Результат отправки неизвестен</b>\n{title}\n{esc(error or '')}\n"
                    f"Проверьте предложку VK, прежде чем отправлять повторно.")
        if result == "missed":
            return f"⏭ <b>Публикация пропущена</b>\n{title}\n{esc(error or '')}"
        return f"❌ <b>Не удалось отправить</b>\n{title}\nПричина: {esc(error or 'неизвестно')}"

    # ---------- отчёты ----------
    async def daily_report(self) -> bool:
        """Отчёт за сутки, закончившиеся в последнюю отметку REPORT_TIME. Не дублируется."""
        async with self._report_lock:
            now = self._now()
            boundary = latest_daily_boundary(now, self.cfg.report_hour, self.cfg.report_minute, self.cfg.tz)
            key = to_iso(boundary)
            last = await self.db.get_meta("report_last")
            if last and last >= key:
                return False
            start, end = window_for_boundary(boundary, self.cfg.tz)
            entries = await self.db.logs_between(to_iso(start), to_iso(end))
            text = format_report(entries, start, end, self.cfg.tz)
            if await self.notify(text):
                await self.db.set_meta("report_last", key)
                return True
            return False

    async def day_report_text(self) -> str:
        end = self._now()
        start = end - timedelta(hours=24)
        entries = await self.db.logs_between(to_iso(start), to_iso(end))
        return format_report(entries, start, end, self.cfg.tz, title="Отправки за последние 24 часа")

    # ---------- запуск ----------
    async def startup(self) -> None:
        interrupted = await self.db.recover_interrupted(self._advance)
        if interrupted:
            names = "\n".join(f"• {esc(p.preview)}" for p in interrupted)
            await self.notify(
                "⚠️ <b>Сервер был перезапущен во время отправки</b>\n"
                f"{names}\n\nАвтоматического повтора не будет, чтобы не создать дубль. "
                "Проверьте «Предложенные» в VK и при необходимости повторите отправку вручную."
            )
        now = self._now()
        boundary = latest_daily_boundary(now, self.cfg.report_hour, self.cfg.report_minute, self.cfg.tz)
        if await self.db.get_meta("report_last") is None:
            await self.db.set_meta("report_last", to_iso(boundary))  # первый запуск: без «пустого» отчёта
        else:
            await self.daily_report()  # догоняем пропущенный отчёт, если сервер не работал в 11:00

    async def backup(self) -> None:
        try:
            path = await self.db.backup(self.cfg.backup_dir)
            if path:
                log.info("Резервная копия БД: %s", path)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось создать резервную копию БД")

    # ---------- диагностика VK ----------
    async def vk_diagnose(self) -> str:
        try:
            acc = await self.vk.check_access(use_cache=False)
        except VKError as e:
            return f"❌ <b>Проверка VK не пройдена</b>\n{esc(str(e))}"
        wall = {0: "выключена", 1: "открытая", 2: "ограниченная", 3: "закрытая"}.get(acc.wall, "неизвестно")
        role = "руководитель (запись будет опубликована, а не предложена!)" if acc.is_manager else "обычный аккаунт"
        problems = []
        try:
            self.vk.assert_can_suggest(acc)
        except VKError as e:
            problems.append(str(e))
        verify = "токеном администратора" if self.vk.can_verify_with_admin else "токеном автора (если VK разрешит)"
        lines = [
            f"{'❌' if problems else '✅'} <b>Проверка VK</b>",
            f"Сообщество: {esc(acc.name)}",
            f"Роль аккаунта токена: {role}",
            f"Стена: {wall}",
            f"Проверка принятия: {verify}",
        ]
        if problems:
            lines += ["", *[f"⚠️ {esc(p)}" for p in problems]]
        else:
            lines += ["", "Формальные проверки пройдены. Окончательно убедиться можно командой /vktest."]
        return "\n".join(lines)

    async def vk_test(self) -> str:
        try:
            res = await self.vk.suggest_post("🧪 Тест GRAY Editor: эту запись можно удалить из предложенных.")
        except VKAmbiguousError as e:
            return f"❓ Результат неизвестен: {esc(str(e))}"
        except VKError as e:
            return f"❌ Не получилось: {esc(str(e))}"
        icon = {"confirmed": "✅", "unconfirmed": "⚠️", "direct": "🚨"}[res.state]
        extra = "\nЗапись опубликована на стене — удалите её вручную!" if res.state == "direct" else \
            "\nУдалите тестовую запись из «Предложенных» в VK."
        return f"{icon} VK id {res.post_id}: {esc(res.note)}{extra}"
