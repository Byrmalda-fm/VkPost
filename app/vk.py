"""Клиент официального VK API: отправка записи в «Предложенные новости» сообщества.

Как это работает (wall.post): если передать owner_id = -ID_сообщества и аккаунт, чей
ПОЛЬЗОВАТЕЛЬСКИЙ токен используется, НЕ является руководителем сообщества, то запись
попадает в «предложенные». Токен сообщества для wall.post недоступен. Если же токен
принадлежит руководителю (админ/редактор/модератор), VK опубликует запись сразу на стене —
поэтому перед отправкой клиент проверяет роль аккаунта и после отправки проверяет, что
запись действительно предложенная (post_type = suggest).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from . import redact

log = logging.getLogger(__name__)

API_URL = "https://api.vk.com/method/"

ERROR_HINTS = {
    5: "Токен недействителен или истёк — получите новый (см. README)",
    6: "Слишком много запросов к VK",
    9: "Слишком много однотипных действий (flood control)",
    14: "VK требует ввести капчу — автоматическая отправка невозможна",
    15: "Доступ запрещён: у токена нет нужных прав или стена закрыта",
    17: "VK требует подтверждения действия в приложении",
    27: "Этот метод недоступен с токеном сообщества — нужен токен пользователя",
    29: "Достигнут лимит запросов VK",
    100: "VK отклонил параметры запроса",
    214: "VK запретил добавить запись (лимит записей, дубликат или нет доступа к предложке)",
    219: "VK: рекламная запись уже недавно добавлялась",
    222: "VK запретил ссылки в тексте записи",
}


class TransportError(Exception):
    """Сетевая ошибка при обращении к VK (исход запроса может быть неизвестен)."""


class VKError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class VKAmbiguousError(VKError):
    """Запрос на создание записи мог дойти до VK, но ответа мы не получили."""


class Transport(Protocol):
    async def post(self, url: str, data: dict[str, Any]) -> dict[str, Any]: ...
    async def upload(self, url: str, field: str, data: bytes) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class AiohttpTransport:
    def __init__(self, total_timeout: float = 45.0):
        self._timeout = total_timeout
        self._session: Any = None

    async def _get(self) -> Any:
        import aiohttp
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        return self._session

    async def post(self, url: str, data: dict[str, Any]) -> dict[str, Any]:
        import aiohttp
        try:
            s = await self._get()
            async with s.post(url, data=data) as r:
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise TransportError(redact.clean(f"{type(e).__name__}: {e}")) from None

    async def upload(self, url: str, field: str, data: bytes) -> dict[str, Any]:
        import aiohttp
        try:
            s = await self._get()
            form = aiohttp.FormData()
            form.add_field(field, data, filename="photo.jpg", content_type="image/jpeg")
            async with s.post(url, data=form) as r:
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as e:
            raise TransportError(redact.clean(f"{type(e).__name__}: {e}")) from None

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


@dataclass(slots=True)
class GroupAccess:
    name: str
    is_manager: bool
    admin_level: int
    wall: int | None
    is_member: bool | None
    can_post: bool | None


@dataclass(slots=True)
class SendResult:
    post_id: int
    state: str  # confirmed | unconfirmed | direct
    note: str


class VKClient:
    def __init__(self, *, user_token: str, group_id: int, api_version: str = "5.199",
                 admin_token: str | None = None, allow_open_wall: bool = False,
                 transport: Transport | None = None, min_interval: float = 0.35,
                 sleep=asyncio.sleep):
        self._token = user_token
        self._gid = abs(group_id)
        self._v = api_version
        self._admin = admin_token
        self._allow_open_wall = allow_open_wall
        self._t: Transport = transport or AiohttpTransport()
        self._min_interval = min_interval
        self._sleep = sleep
        self._last_call = 0.0
        self._throttle_lock = asyncio.Lock()
        self._access: tuple[float, GroupAccess] | None = None
        self._blocked: str | None = None
        redact.register(user_token, admin_token)

    @property
    def can_verify_with_admin(self) -> bool:
        return self._admin is not None

    # ---------- низкий уровень ----------
    async def _throttle(self) -> None:
        async with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await self._sleep(wait)
            self._last_call = time.monotonic()

    async def call(self, method: str, params: dict[str, Any], *, token: str | None = None,
                   idempotent: bool = True) -> Any:
        """Вызов метода VK API.

        idempotent=False (wall.post): при обрыве сети НЕ повторяем — запись могла быть создана.
        """
        body = {**params, "v": self._v, "access_token": token or self._token}
        net_attempts = 0
        rate_attempts = 0
        while True:
            await self._throttle()
            try:
                data = await self._t.post(API_URL + method, body)
            except TransportError as e:
                if idempotent and net_attempts < 2:
                    net_attempts += 1
                    await self._sleep(1.5 * net_attempts)
                    continue
                if idempotent:
                    raise VKError(f"Сетевая ошибка при обращении к VK: {e}") from None
                raise VKAmbiguousError(
                    f"Нет ответа от VK при создании записи ({e}); запись могла быть создана"
                ) from None
            if not isinstance(data, dict):
                raise VKError("VK вернул неожиданный ответ")
            if "error" in data:
                err = data["error"] or {}
                code = err.get("error_code")
                if code in (6, 9) and rate_attempts < 3:
                    rate_attempts += 1
                    await self._sleep(1.0 + rate_attempts)
                    continue
                hint = ERROR_HINTS.get(code, "Ошибка VK API")
                raise VKError(f"{hint} (код {code}: {err.get('error_msg', '')})", code=code)
            return data.get("response")

    # ---------- проверки ----------
    async def check_access(self, use_cache: bool = True) -> GroupAccess:
        if use_cache and self._access and time.monotonic() - self._access[0] < 300:
            return self._access[1]
        resp = await self.call("groups.getById", {
            "group_id": str(self._gid),
            "fields": "is_admin,admin_level,is_member,can_post,wall",
        })
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        if not groups:
            raise VKError("VK не вернул данные сообщества — проверьте VK_GROUP_ID")
        g = groups[0]
        acc = GroupAccess(
            name=g.get("name", f"club{self._gid}"),
            is_manager=bool(g.get("is_admin")) or int(g.get("admin_level") or 0) > 0,
            admin_level=int(g.get("admin_level") or 0),
            wall=g.get("wall"),
            is_member=None if g.get("is_member") is None else bool(g.get("is_member")),
            can_post=None if g.get("can_post") is None else bool(g.get("can_post")),
        )
        self._access = (time.monotonic(), acc)
        return acc

    def assert_can_suggest(self, acc: GroupAccess) -> None:
        if self._blocked:
            raise VKError(self._blocked)
        if acc.is_manager:
            raise VKError(
                "Токен принадлежит руководителю сообщества (админ/редактор/модератор): VK опубликует "
                "запись сразу на стене, а не в предложенных. Используйте токен обычного аккаунта, "
                "который не является руководителем сообщества (см. README)"
            )
        if acc.wall == 0:
            raise VKError("Стена сообщества выключена — предложить запись нельзя")
        if acc.wall == 1 and not self._allow_open_wall:
            raise VKError(
                "Стена сообщества «открытая»: записи обычных участников, скорее всего, публикуются сразу. "
                "Переведите стену в режим «ограниченная» (Управление → Разделы → Стена) либо, если вы "
                "проверили поведение, задайте VK_ALLOW_OPEN_WALL=true"
            )

    # ---------- фото ----------
    async def upload_wall_photo(self, photo: bytes) -> str:
        srv = await self.call("photos.getWallUploadServer", {})
        if not isinstance(srv, dict) or not srv.get("upload_url"):
            raise VKError("VK не выдал адрес для загрузки фото")
        try:
            up = await self._t.upload(srv["upload_url"], "photo", photo)
        except TransportError as e:
            raise VKError(f"Не удалось загрузить фото в VK: {e}") from None
        if not up.get("photo") or up.get("photo") in ("[]", "{}"):
            raise VKError("VK отклонил загруженное фото (проверьте размер и формат)")
        saved = await self.call("photos.saveWallPhoto", {
            "server": up["server"], "photo": up["photo"], "hash": up["hash"],
        })
        if not saved:
            raise VKError("VK не сохранил загруженное фото")
        p = saved[0]
        att = f"photo{p['owner_id']}_{p['id']}"
        if p.get("access_key"):
            att += f"_{p['access_key']}"
        return att

    # ---------- отправка ----------
    async def suggest_post(self, text: str, photo: bytes | None = None) -> SendResult:
        acc = await self.check_access()
        self.assert_can_suggest(acc)

        params: dict[str, Any] = {"owner_id": -self._gid, "message": text}
        if photo:
            params["attachments"] = await self.upload_wall_photo(photo)
        # ВАЖНО: from_group и signed не передаём — иначе это была бы обычная публикация.
        resp = await self.call("wall.post", params, idempotent=False)

        post_id = resp.get("post_id") if isinstance(resp, dict) else None
        if not post_id:
            raise VKAmbiguousError("VK не вернул post_id — принятие записи не подтверждено")
        state, note = await self._verify(int(post_id))
        if state == "direct":
            self._blocked = (
                "Ранее VK опубликовал запись сразу на стене, а не в предложенных. Отправка остановлена "
                "до перезапуска бота; исправьте роль аккаунта/режим стены (см. README)"
            )
        return SendResult(int(post_id), state, note)

    async def _verify(self, post_id: int) -> tuple[str, str]:
        """Проверяет, что запись создана именно как предложенная."""
        token = self._admin
        if token:
            try:
                resp = await self.call("wall.get", {"owner_id": -self._gid, "filter": "suggests",
                                                    "count": 100}, token=token)
                items = (resp or {}).get("items", [])
                if any(int(i.get("id", -1)) == post_id for i in items):
                    return "confirmed", "найдена в предложенных (проверено токеном администратора)"
            except VKError as e:
                log.warning("Проверка через wall.get(suggests) не удалась: %s", e)
        try:
            resp = await self.call("wall.getById", {"posts": f"-{self._gid}_{post_id}"}, token=token)
            items = resp.get("items") if isinstance(resp, dict) else resp
            if items:
                ptype = items[0].get("post_type")
                if ptype == "suggest":
                    return "confirmed", "VK подтвердил: post_type = suggest"
                if ptype == "post":
                    return "direct", "VK опубликовал запись сразу на стене (post_type = post)"
        except VKError as e:
            log.warning("Проверка через wall.getById не удалась: %s", e)
        return "unconfirmed", "VK вернул post_id, но проверить запись в предложенных не удалось"
