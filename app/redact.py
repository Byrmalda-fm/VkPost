"""Скрытие секретов в логах и сообщениях об ошибках."""
from __future__ import annotations

import logging

_secrets: set[str] = set()


def register(*values: str | None) -> None:
    for v in values:
        if v and len(v) >= 8:
            _secrets.add(v)


def clean(text: str) -> str:
    for s in _secrets:
        if s in text:
            text = text.replace(s, "***")
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        cleaned = clean(msg)
        if cleaned != msg:
            record.msg = cleaned
            record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            # текст исключения очищаем при форматировании ниже
            record.exc_text = None
        return True


class RedactFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return clean(super().format(record))


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
