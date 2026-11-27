"""Конфигурация из переменных окружения (и необязательного файла .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .timeutil import format_hhmm, parse_time


class ConfigError(Exception):
    pass


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    vk_user_token: str
    vk_group_id: int
    vk_admin_token: str | None
    vk_api_version: str
    vk_allow_open_wall: bool
    timezone: str
    db_path: str
    media_dir: str
    backup_dir: str
    report_hour: int
    report_minute: int
    send_time: str              # единое время отправки постов (ЧЧ:ММ по timezone)
    send_hour: int
    send_minute: int
    tick_seconds: int
    missed_grace_minutes: int
    log_level: str
    editor_name: str
    database_url: str | None = None   # если задан — PostgreSQL вместо SQLite (нужно на бесплатном Render)
    port: int | None = None           # если задан (Render выставляет PORT) — поднимается HTTP /health
    self_ping_url: str | None = None  # публичный адрес сервиса для самопроверки (RENDER_EXTERNAL_URL)
    startup_notify: bool = True       # писать владельцу «GRAY Editor запущен» при каждом старте

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, dotenv: str | None = ".env") -> "Config":
        if env is None:
            if dotenv:
                _load_dotenv(Path(dotenv))
            env = dict(os.environ)

        missing = [k for k in ("BOT_TOKEN", "OWNER_ID", "VK_USER_TOKEN", "VK_GROUP_ID")
                   if not env.get(k, "").strip()]
        if missing:
            raise ConfigError("Не заданы переменные окружения: " + ", ".join(missing))

        try:
            owner_id = int(env["OWNER_ID"])
            group_id = abs(int(env["VK_GROUP_ID"]))
        except ValueError as e:
            raise ConfigError("OWNER_ID и VK_GROUP_ID должны быть числами") from e

        tz_name = env.get("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
        try:
            ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(f"Неизвестный часовой пояс TIMEZONE={tz_name}") from e

        rt = parse_time(env.get("REPORT_TIME", "11:00"))
        if rt is None:
            raise ConfigError("REPORT_TIME должен быть в формате ЧЧ:ММ")

        st = parse_time(env.get("SEND_TIME", "11:00"))
        if st is None:
            raise ConfigError("SEND_TIME должен быть в формате ЧЧ:ММ")

        try:
            tick = int(env.get("TICK_SECONDS", "30"))
            grace = int(env.get("MISSED_GRACE_MINUTES", "120"))
        except ValueError as e:
            raise ConfigError("TICK_SECONDS и MISSED_GRACE_MINUTES должны быть числами") from e
        if tick < 5 or tick > 60 or 60 % tick:
            raise ConfigError("TICK_SECONDS должен делить 60 нацело и быть не меньше 5 (5, 6, 10, 12, 15, 20, 30, 60)")

        port_raw = env.get("PORT", "").strip()
        try:
            port = int(port_raw) if port_raw else None
        except ValueError as e:
            raise ConfigError("PORT должен быть числом") from e
        self_ping = (env.get("SELF_PING_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")

        data_dir = env.get("DATA_DIR", "data").strip() or "data"
        return cls(
            bot_token=env["BOT_TOKEN"].strip(),
            owner_id=owner_id,
            vk_user_token=env["VK_USER_TOKEN"].strip(),
            vk_group_id=group_id,
            vk_admin_token=(env.get("VK_ADMIN_TOKEN", "").strip() or None),
            vk_api_version=env.get("VK_API_VERSION", "5.199").strip(),
            vk_allow_open_wall=_bool(env.get("VK_ALLOW_OPEN_WALL")),
            timezone=tz_name,
            db_path=env.get("DB_PATH", f"{data_dir}/gray_editor.sqlite3"),
            media_dir=env.get("MEDIA_DIR", f"{data_dir}/media"),
            backup_dir=env.get("BACKUP_DIR", f"{data_dir}/backups"),
            report_hour=rt[0],
            report_minute=rt[1],
            send_time=format_hhmm(*st),
            send_hour=st[0],
            send_minute=st[1],
            tick_seconds=tick,
            missed_grace_minutes=grace,
            log_level=env.get("LOG_LEVEL", "INFO"),
            editor_name=env.get("EDITOR_NAME", "Winston_Goodman"),
            database_url=(env.get("DATABASE_URL", "").strip() or None),
            port=port,
            self_ping_url=self_ping or None,
            startup_notify=_bool(env.get("STARTUP_NOTIFY"), True),
        )
