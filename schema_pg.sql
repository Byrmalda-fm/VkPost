-- GRAY Editor: схема базы данных (PostgreSQL). Применяется автоматически при старте (идемпотентно).
-- Смысл таблиц и полей — как в schema.sql (SQLite). Время хранится строкой ISO в UTC, как и в SQLite.

CREATE TABLE IF NOT EXISTS posts (
    id            BIGSERIAL PRIMARY KEY,
    topic         TEXT,
    text          TEXT    NOT NULL,
    photo_data    BYTEA,
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

CREATE TABLE IF NOT EXISTS send_log (
    id            BIGSERIAL PRIMARY KEY,
    post_id       BIGINT REFERENCES posts (id) ON DELETE SET NULL,
    preview       TEXT    NOT NULL,
    scheduled_for TEXT    NOT NULL,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    result        TEXT    NOT NULL
                  CHECK (result IN ('sending', 'confirmed', 'unconfirmed', 'error', 'missed', 'unknown')),
    vk_post_id    BIGINT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_started ON send_log (started_at);

-- Защита от повторной отправки одного и того же вхождения поста.
CREATE UNIQUE INDEX IF NOT EXISTS uq_log_occurrence
    ON send_log (post_id, scheduled_for)
    WHERE result IN ('sending', 'confirmed', 'unconfirmed', 'unknown');

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
