FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY buyer/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Для MVP пытаемся поставить Codex CLI. Если установка недоступна,
# можно передать альтернативный бинарь через CODEX_BIN.
RUN npm install -g @openai/codex || true

COPY buyer/scripts/package.json buyer/scripts/package-lock.json /app/scripts/
RUN cd /app/scripts && npm ci

COPY buyer /app
RUN chmod +x /app/docker/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
