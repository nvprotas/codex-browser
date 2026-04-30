FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY eval_service/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

RUN npm install -g @openai/codex

COPY eval_service ./eval_service
RUN chmod +x /workspace/eval_service/docker/entrypoint.sh

EXPOSE 8090

ENTRYPOINT ["/workspace/eval_service/docker/entrypoint.sh"]
CMD ["uvicorn", "eval_service.app.main:app", "--host", "0.0.0.0", "--port", "8090"]
