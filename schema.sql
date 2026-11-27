-- GRAY Editor: схема базы данных (SQLite). Применяется автоматически при старте (идемпотентно).

PRAGMA foreign_keys = ON;

-- Запланированные публикации.
-- weekday: 0 = понедельник ... 6 = воскресенье.
-- next_run_at: ближайший момент отправки, UTC, формат YYYY-MM-DDTHH:MM:SSZ.
-- status: pending (ждёт), sending (отправляется), sent (отправлено),
--         error (ошибка), unknown (результат неизвестен — проверить вручную).
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic         TEXT,
    text          TEXT    NOT NULL,
    photo_data    BLOB,   -- байты фото (хранятся в БД: файловая система на бесплатном Render не постоянная)
    photo_file_id TEXT,
    weekday       INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    send_time     TEXT    NOT NULL,
    repeat_weekly INTEGER NOT NULL DEFAULT 0 CHECK (repeat_weekly IN (0, 1)),
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'sending', 'sent', 'error', 'unknown')),
    next_run_at   TEXT    NOT NULL,
    last_error    TEXT,
    deleted       INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_due
    ON posts (status, next_run_at) WHERE deleted = 0;

-- История отправок (журнал). Строка создаётся в момент «захвата» поста
-- планировщиком (result = 'sending') и закрывается по итогу.
-- result: confirmed   — VK принял, запись найдена в предложенных;
--         unconfirmed — VK вернул post_id, но проверить не удалось;
--         error       — отправка не удалась (причина в error);
--         missed      — время прошло, пока сервер не работал;
--         unknown     — исход неизвестен (обрыв сети/рестарт во время отправки).
CREATE TABLE IF NOT EXISTS send_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id       INTEGER REFERENCES posts (id) ON DELETE SET NULL,
    preview       TEXT    NOT NULL,
    scheduled_for TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    result        TEXT    NOT NULL
                  CHECK (result IN ('sending', 'confirmed', 'unconfirmed', 'error', 'missed', 'unknown')),
    vk_post_id    INTEGER,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_started ON send_log (started_at);

-- Защита от повторной отправки: одно и то же вхождение поста (post_id + время)
-- не может быть «захвачено» дважды, если оно уже отправлялось или отправляется.
CREATE UNIQUE INDEX IF NOT EXISTS uq_log_occurrence
    ON send_log (post_id, scheduled_for)
    WHERE result IN ('sending', 'confirmed', 'unconfirmed', 'unknown');

-- Служебные значения (например, дата последнего отправленного отчёта).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

PRAGMA user_version = 1;
