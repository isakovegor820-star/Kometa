# =============================================================================
#  Kometa — образ бота (aiogram 3 + веб-слой ссылки-подписки)
# =============================================================================
#  Сборка:   docker compose build
#  Запуск:   docker compose up -d
#  Внутри:   python -m app.main  (long polling Telegram + uvicorn на 8080)
#
#  Секретов в образе нет: всё приходит переменными окружения из .env
#  (см. env_file в docker-compose.yml). .env в образ не копируется.
# =============================================================================

FROM python:3.12-slim

# PYTHONUNBUFFERED — логи видны сразу (docker compose logs);
# PYTHONDONTWRITEBYTECODE — не мусорим .pyc в томе ./data.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Moscow

WORKDIR /app

# ca-certificates — HTTPS к api.telegram.org и API панели;
# tzdata — правильное местное время для планировщика (напоминания за 3 и 1 день).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# Сначала зависимости — слой кэшируется и не пересобирается при правках кода.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Затем код. Копируем только app/ — тесты, docs и скрипты в образе не нужны.
COPY app ./app

# Каталог для SQLite (в compose сюда монтируется ./data).
RUN mkdir -p /app/data

# Порт веб-слоя /sub/<token>. Наружу его публикует docker-compose.yml.
EXPOSE 8080

# Простой процесс без супервизора: упал — docker поднимет (restart: unless-stopped).
CMD ["python", "-m", "app.main"]
