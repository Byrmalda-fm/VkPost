"""Слой доступа к данным: SQLite (локально) или PostgreSQL (Render и другие облака).

Вся логика запросов написана один раз против небольшого адаптера соединения (`Conn`);
бэкенд выбирается по наличию DATABASE_URL. Время везде хранится строкой ISO (UTC).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .timeutil import to_iso, utcnow

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
SCHEMA_PG_PATH = Path(__file__).resolve().parent.parent / "schema_pg.sql"

POST_FIELDS = {
    "topic", "text", "photo_data", "photo_file_id", "weekday", "send_time",
    "repeat_weekly", "status", "next_run_at", "last_error",
}
# Столбцы поста БЕЗ самих байтов фото: списки не должны тянуть мегабайты.
POST_COLS = ("id, topic, text, photo_file_id, (photo_data IS NOT NULL) AS has_photo, weekday, "
             "send_time, repeat_weekly, status, next_run_at, last_error, created_at, updated_at")
OK_RESULTS = ("confirmed", "unconfirmed")


class IntegrityError(Exception):
    """Нарушение ограничения БД (одинаково для SQLite и PostgreSQL)."""


def make_preview(topic: str | None, text: str, limit: int = 60) -> str:
    base = topic.strip() if topic and topic.strip() else " ".join(text.split())
    return base if len(base) <= limit else base[: limit - 1] + "…"


@dataclass(slots=True)
class Post:
    id: int
    topic: str | None
    text: str
    has_photo: bool
    photo_file_id: str | None
    weekday: int
    send_time: str
    repeat_weekly: bool
    status: str
    next_run_at: str
    last_error: str | None
    created_at: str
    updated_at: str

    @property
    def preview(self) -> str:
        return make_preview(self.topic, self.text)


@dataclass(slots=True)
class LogEntry:
    id: int
    post_id: int | None
    preview: str
    scheduled_for: str
    started_at: str
    finished_at: str | None
    result: str
    vk_post_id: int | None
    error: str | None


def _post(row: Any) -> Post:
    return Post(
        id=row["id"], topic=row["topic"], text=row["text"], has_photo=bool(row["has_photo"]),
        photo_file_id=row["photo_file_id"], weekday=row["weekday"], send_time=row["send_time"],
        repeat_weekly=bool(row["repeat_weekly"]), status=row["status"],
        next_run_at=row["next_run_at"], last_error=row["last_error"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _log(row: Any) -> LogEntry:
    return LogEntry(
        id=row["id"], post_id=row["post_id"], preview=row["preview"],
        scheduled_for=row["scheduled_for"], started_at=row["started_at"],
        finished_at=row["finished_at"], result=row["result"],
        vk_post_id=row["vk_post_id"], error=row["error"],
    )


# ---------- адаптеры соединений ----------
def to_pg_sql(sql: str) -> str:
    """'?' -> '$1, $2, ...' (в наших запросах '?' в строковых литералах не встречается)."""
    out, n = [], 0
    for ch in sql:
        if ch == "?":
            n += 1
            out.append(f"${n}")
        else:
            out.append(ch)
    return "".join(out)


def clean_dsn(url: str) -> str:
    """Приводит строку подключения к виду, понятному asyncpg.

    * postgres:// -> postgresql://
    * убирает параметры, которых asyncpg не знает (channel_binding у Neon и т. п.).
    """
    parts = urlsplit(url.strip())
    scheme = "postgresql" if parts.scheme in ("postgres", "postgresql", "postgresql+asyncpg") else parts.scheme
    drop = {"channel_binding"}
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in drop])
    return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))


class _SqliteConn:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    async def execute(self, sql: str, params: tuple = ()) -> int:
        def work() -> int:
            try:
                return self._c.execute(sql, params).rowcount
            except sqlite3.IntegrityError as e:
                raise IntegrityError(str(e)) from e
        return await asyncio.to_thread(work)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        return await asyncio.to_thread(lambda: self._c.execute(sql, params).fetchall())

    async def fetchone(self, sql: str, params: tuple = ()) -> Any | None:
        return await asyncio.to_thread(lambda: self._c.execute(sql, params).fetchone())

    async def insert(self, sql: str, params: tuple = ()) -> int:
        """INSERT ... RETURNING id -> id."""
        def work() -> int:
            try:
                # fetchall(): курсор с RETURNING должен быть дочитан до COMMIT
                return int(self._c.execute(sql, params).fetchall()[0][0])
            except sqlite3.IntegrityError as e:
                raise IntegrityError(str(e)) from e
        return await asyncio.to_thread(work)


class _PgConn:
    def __init__(self, conn: Any):
        self._c = conn

    @staticmethod
    def _reraise(e: Exception) -> None:
        import asyncpg
        if isinstance(e, asyncpg.IntegrityConstraintViolationError):
            raise IntegrityError(str(e)) from e
        raise e

    async def execute(self, sql: str, params: tuple = ()) -> int:
        try:
            status = await self._c.execute(to_pg_sql(sql), *params)
        except Exception as e:  # noqa: BLE001
            self._reraise(e)
        # asyncpg возвращает строку статуса вида 'UPDATE 1' / 'INSERT 0 1'
        tail = (status or "").rsplit(" ", 1)[-1]
        return int(tail) if tail.isdigit() else 0

    async def fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        return await self._c.fetch(to_pg_sql(sql), *params)

    async def fetchone(self, sql: str, params: tuple = ()) -> Any | None:
        return await self._c.fetchrow(to_pg_sql(sql), *params)

    async def insert(self, sql: str, params: tuple = ()) -> int:
        try:
            return int(await self._c.fetchval(to_pg_sql(sql), *params))
        except Exception as e:  # noqa: BLE001
            self._reraise(e)
            raise  # недостижимо: _reraise всегда бросает исключение


Fn = Callable[[Any], Awaitable[Any]]


class _SqliteBackend:
    kind = "sqlite"

    def __init__(self, path: str):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._connect)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate(conn)
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Старые базы (до переезда на хранение фото в БД): добавить photo_data и забрать фото из файлов."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
        if "photo_data" not in cols:
            conn.execute("ALTER TABLE posts ADD COLUMN photo_data BLOB")
            if "photo_path" in cols:
                for r in conn.execute("SELECT id, photo_path FROM posts WHERE photo_path IS NOT NULL").fetchall():
                    try:
                        data = Path(r["photo_path"]).read_bytes()
                    except OSError:
                        continue
                    conn.execute("UPDATE posts SET photo_data=? WHERE id=?", (data, r["id"]))

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    async def run(self, fn: Fn, write: bool) -> Any:
        assert self._conn is not None, "База данных не открыта"
        c = _SqliteConn(self._conn)
        async with self._lock:
            if not write:
                return await fn(c)
            await asyncio.to_thread(self._conn.execute, "BEGIN IMMEDIATE")
            try:
                result = await fn(c)
                await asyncio.to_thread(self._conn.execute, "COMMIT")
                return result
            except BaseException:
                await asyncio.to_thread(self._conn.execute, "ROLLBACK")
                raise

    async def backup(self, dest_dir: str, keep: int) -> str | None:
        directory = Path(dest_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        target = directory / f"gray_editor-{stamp}.sqlite3"

        def work() -> None:
            assert self._conn is not None
            dst = sqlite3.connect(target)
            try:
                self._conn.backup(dst)
            finally:
                dst.close()

        async with self._lock:
            await asyncio.to_thread(work)
        for old in sorted(directory.glob("gray_editor-*.sqlite3"))[:-keep]:
            old.unlink(missing_ok=True)
        return str(target)


class _PgBackend:
    kind = "postgres"
    SCHEMA_LOCK_KEY = 7263541  # чтобы два одновременно стартующих экземпляра не создавали таблицы наперегонки

    def __init__(self, url: str):
        self.dsn = clean_dsn(url)
        self._pool: Any = None

    async def open(self) -> None:
        import asyncpg
        self._pool = await asyncpg.create_pool(
            self.dsn, min_size=1, max_size=3, command_timeout=30,
            # Neon/pgbouncer: без кэша prepared-statements; простаивающие соединения
            # закрываем сами раньше, чем это сделает сервер (Neon усыпляет базу через 5 минут).
            statement_cache_size=0, max_inactive_connection_lifetime=60,
        )
        schema = SCHEMA_PG_PATH.read_text(encoding="utf-8")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", self.SCHEMA_LOCK_KEY)
                await conn.execute(schema)

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.close()

    async def run(self, fn: Fn, write: bool) -> Any:
        import asyncpg
        assert self._pool is not None, "База данных не открыта"
        transient = (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, ConnectionError, OSError)
        for attempt in (1, 2):
            try:
                async with self._pool.acquire() as raw:
                    c = _PgConn(raw)
                    if not write:
                        return await fn(c)
                    async with raw.transaction():
                        return await fn(c)
            except transient as e:
                # Базу могли усыпить/перезапустить: транзакция не зафиксирована, одна повторная попытка безопасна.
                if attempt == 2:
                    raise
                log.warning("Соединение с БД потеряно (%s), повторяю запрос", type(e).__name__)
                await asyncio.sleep(1)

    async def backup(self, dest_dir: str, keep: int) -> str | None:
        return None  # резервные копии/восстановление обеспечивает провайдер Postgres


class Database:
    def __init__(self, path: str | None = None, url: str | None = None):
        if url:
            self._b: _SqliteBackend | _PgBackend = _PgBackend(url)
        else:
            self._b = _SqliteBackend(path or "data/gray_editor.sqlite3")

    @property
    def kind(self) -> str:
        return self._b.kind

    async def open(self) -> None:
        await self._b.open()

    async def close(self) -> None:
        await self._b.close()

    async def _run(self, fn: Fn, write: bool = False) -> Any:
        return await self._b.run(fn, write)

    # ---------- посты ----------
    async def create_post(self, *, topic: str | None, text: str, photo_data: bytes | None,
                          photo_file_id: str | None, weekday: int, send_time: str,
                          repeat_weekly: bool, next_run_at: str) -> int:
        now = to_iso(utcnow())

        async def fn(c: Any) -> int:
            return await c.insert(
                "INSERT INTO posts (topic, text, photo_data, photo_file_id, weekday, send_time,"
                " repeat_weekly, status, next_run_at, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,'pending',?,?,?) RETURNING id",
                (topic, text, photo_data, photo_file_id, weekday, send_time,
                 int(repeat_weekly), next_run_at, now, now),
            )

        return await self._run(fn, write=True)

    async def get_post(self, post_id: int) -> Post | None:
        async def fn(c: Any) -> Post | None:
            row = await c.fetchone(f"SELECT {POST_COLS} FROM posts WHERE id=? AND deleted=0", (post_id,))
            return _post(row) if row else None
        return await self._run(fn)

    async def get_photo(self, post_id: int) -> bytes | None:
        async def fn(c: Any) -> bytes | None:
            row = await c.fetchone("SELECT photo_data FROM posts WHERE id=?", (post_id,))
            data = row["photo_data"] if row else None
            return bytes(data) if data is not None else None
        return await self._run(fn)

    async def list_posts(self, limit: int, offset: int) -> list[Post]:
        async def fn(c: Any) -> list[Post]:
            rows = await c.fetchall(
                f"SELECT {POST_COLS} FROM posts WHERE deleted=0 ORDER BY weekday, send_time, id LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return [_post(r) for r in rows]
        return await self._run(fn)

    async def count_posts(self) -> int:
        async def fn(c: Any) -> int:
            return int((await c.fetchone("SELECT COUNT(*) FROM posts WHERE deleted=0"))[0])
        return await self._run(fn)

    async def update_post(self, post_id: int, fields: dict[str, Any]) -> bool:
        """Обновляет пост. Не трогает посты, которые прямо сейчас отправляются."""
        bad = set(fields) - POST_FIELDS
        if bad:
            raise ValueError(f"Недопустимые поля: {bad}")
        if not fields:
            return False
        values = {k: (int(v) if k == "repeat_weekly" else v) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in values)

        async def fn(c: Any) -> bool:
            n = await c.execute(
                f"UPDATE posts SET {sets}, updated_at=? WHERE id=? AND deleted=0 AND status!='sending'",
                (*values.values(), to_iso(utcnow()), post_id),
            )
            return n == 1

        return await self._run(fn, write=True)

    async def delete_post(self, post_id: int) -> bool:
        async def fn(c: Any) -> bool:
            # фото удаляем сразу: в БД оно занимает место, а запись остаётся ради истории отправок
            n = await c.execute(
                "UPDATE posts SET deleted=1, photo_data=NULL, updated_at=? WHERE id=? AND deleted=0",
                (to_iso(utcnow()), post_id),
            )
            return n == 1
        return await self._run(fn, write=True)

    # ---------- отправка ----------
    async def claim_due(self, now_iso: str) -> list[tuple[Post, int]]:
        """Атомарно «захватывает» созревшие посты: pending -> sending + строка в журнале.

        Пост, захваченный одним вызовом, никогда не будет захвачен другим, поэтому
        повторное выполнение задачи (в том числе вторым экземпляром сервиса во время
        деплоя) не приводит к повторной отправке.
        """
        async def fn(c: Any) -> list[tuple[Post, int]]:
            rows = await c.fetchall(
                f"SELECT {POST_COLS} FROM posts WHERE deleted=0 AND status='pending' AND next_run_at<=?"
                " ORDER BY next_run_at, id",
                (now_iso,),
            )
            claimed: list[tuple[Post, int]] = []
            for row in rows:
                post = _post(row)
                await c.execute("SAVEPOINT claim")
                try:
                    n = await c.execute(
                        "UPDATE posts SET status='sending', updated_at=? WHERE id=? AND status='pending'",
                        (now_iso, post.id),
                    )
                    if n != 1:
                        await c.execute("ROLLBACK TO claim")
                        await c.execute("RELEASE claim")
                        continue
                    log_id = await c.insert(
                        "INSERT INTO send_log (post_id, preview, scheduled_for, started_at, result)"
                        " VALUES (?,?,?,?, 'sending') RETURNING id",
                        (post.id, post.preview, post.next_run_at, now_iso),
                    )
                    await c.execute("RELEASE claim")
                    claimed.append((post, log_id))
                except IntegrityError:
                    # это вхождение уже отправлялось — повторно не отправляем
                    await c.execute("ROLLBACK TO claim")
                    await c.execute("RELEASE claim")
                    await c.execute(
                        "UPDATE posts SET status='error', last_error=?, updated_at=? WHERE id=? AND status='pending'",
                        ("Это вхождение уже отправлялось ранее; повторная отправка заблокирована",
                         now_iso, post.id),
                    )
            return claimed

        return await self._run(fn, write=True)

    async def finish_send(self, *, log_id: int, post_id: int, result: str, vk_post_id: int | None,
                          error: str | None, next_run_at: str | None) -> None:
        """Закрывает запись журнала и обновляет пост.

        Для еженедельного поста статус возвращается в pending с новой датой next_run_at,
        для разового — sent / error / unknown.
        """
        now = to_iso(utcnow())

        async def fn(c: Any) -> None:
            await c.execute(
                "UPDATE send_log SET result=?, vk_post_id=?, error=?, finished_at=? WHERE id=?",
                (result, vk_post_id, error, now, log_id),
            )
            row = await c.fetchone("SELECT repeat_weekly FROM posts WHERE id=?", (post_id,))
            if row is None:
                return
            ok = result in OK_RESULTS
            if row["repeat_weekly"] and next_run_at:
                await c.execute(
                    "UPDATE posts SET status='pending', next_run_at=?, last_error=?, updated_at=?"
                    " WHERE id=? AND status='sending'",
                    (next_run_at, None if ok else error, now, post_id),
                )
            else:
                status = "sent" if ok else ("unknown" if result == "unknown" else "error")
                await c.execute(
                    "UPDATE posts SET status=?, last_error=?, updated_at=? WHERE id=? AND status='sending'",
                    (status, None if ok else error, now, post_id),
                )

        await self._run(fn, write=True)

    async def recover_interrupted(self, advance: Callable[[Post], str]) -> list[Post]:
        """После рестарта: всё, что было в статусе sending, помечается как неизвестное.

        Автоматической повторной отправки НЕТ — исход мог быть успешным; владелец
        проверяет предложку VK вручную. Еженедельные посты переходят к следующей неделе.
        """
        now = to_iso(utcnow())
        msg = "Сервер перезапущен во время отправки; результат неизвестен — проверьте предложку VK"

        async def fn(c: Any) -> list[Post]:
            rows = await c.fetchall(f"SELECT {POST_COLS} FROM posts WHERE status='sending'")
            posts = [_post(r) for r in rows]
            for p in posts:
                if p.repeat_weekly:
                    await c.execute(
                        "UPDATE posts SET status='pending', next_run_at=?, last_error=?, updated_at=? WHERE id=?",
                        (advance(p), msg, now, p.id),
                    )
                else:
                    await c.execute(
                        "UPDATE posts SET status='unknown', last_error=?, updated_at=? WHERE id=?",
                        (msg, now, p.id),
                    )
            await c.execute(
                "UPDATE send_log SET result='unknown', error=?, finished_at=? WHERE result='sending'",
                (msg, now),
            )
            return posts

        return await self._run(fn, write=True)

    # ---------- журнал ----------
    async def logs_between(self, start_iso: str, end_iso: str) -> list[LogEntry]:
        async def fn(c: Any) -> list[LogEntry]:
            rows = await c.fetchall(
                "SELECT * FROM send_log WHERE started_at>=? AND started_at<? ORDER BY started_at, id",
                (start_iso, end_iso),
            )
            return [_log(r) for r in rows]
        return await self._run(fn)

    async def list_logs(self, limit: int, offset: int) -> list[LogEntry]:
        async def fn(c: Any) -> list[LogEntry]:
            rows = await c.fetchall(
                "SELECT * FROM send_log ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return [_log(r) for r in rows]
        return await self._run(fn)

    async def count_logs(self) -> int:
        async def fn(c: Any) -> int:
            return int((await c.fetchone("SELECT COUNT(*) FROM send_log"))[0])
        return await self._run(fn)

    # ---------- meta ----------
    async def get_meta(self, key: str) -> str | None:
        async def fn(c: Any) -> str | None:
            row = await c.fetchone("SELECT value FROM meta WHERE key=?", (key,))
            return row["value"] if row else None
        return await self._run(fn)

    async def set_meta(self, key: str, value: str) -> None:
        async def fn(c: Any) -> None:
            await c.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self._run(fn, write=True)

    # ---------- резервная копия ----------
    async def backup(self, dest_dir: str, keep: int = 7) -> str | None:
        """Копия файла SQLite. Для PostgreSQL возвращает None (копии делает провайдер)."""
        return await self._b.backup(dest_dir, keep)
