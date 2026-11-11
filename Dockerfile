FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 1. зависимости проекта (отдельным слоем, чтобы кеш не сбивался при правках кода)
COPY requirements.txt .
RUN pip install -r requirements.txt

# 2. локальный поисковый сервер Jiro
RUN pip install jirosearch

# 3. код проекта
COPY . .

# Jiro слушает 8000 только внутри контейнера (127.0.0.1), снаружи он недоступен.
# Основное приложение НЕ может быть на 8000 (порт занят Jiro): берём $PORT платформы (Render/Railway), по умолчанию 10000.
ENV PORT=10000
EXPOSE 10000

# Jiro стартует в фоне (&), uvicorn запускается основным процессом (exec), чтобы он получал сигналы остановки.
CMD ["sh", "-c", "jiro serve --host 127.0.0.1 --port 8000 > /tmp/jiro.log 2>&1 & exec uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
