FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY schema.sql schema_pg.sql ./

# непривилегированный пользователь; /app/data нужен только для SQLite (локальный запуск) и временных файлов
RUN useradd --create-home --uid 10001 bot && mkdir -p /app/data && chown -R bot:bot /app
USER bot

# На Render порт приходит в переменной PORT; сервис отвечает на /health
EXPOSE 10000

CMD ["python", "-m", "app.main"]
