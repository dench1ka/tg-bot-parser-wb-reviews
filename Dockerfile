FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости ставим первыми — для кеширования слоя
COPY requirements.txt .
RUN pip install -r requirements.txt

# Код приложения
COPY . .

# Папки для данных и non-root пользователь
RUN mkdir -p /app/data/files /app/data/logs && \
    useradd --create-home --shell /bin/bash botuser && \
    chown -R botuser:botuser /app

USER botuser

CMD ["python", "bot.py"]
