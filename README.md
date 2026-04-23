# buyer MVP: Codex + Playwright + browser-sidecar + microUI

Минимальная версия системы из трех сервисов:

- `buyer` (FastAPI): принимает задачу от `openclaw`, запускает `codex exec`, оркестрирует шаги и отправляет callback-события.
- `browser` (отдельный sidecar): держит Chromium + Xvfb + x11vnc + noVNC и отдает CDP endpoint для Playwright.
- `micro-ui` (FastAPI + HTML/JS): временный `middle`, принимает callbacks, показывает ленту событий, noVNC и форму ответа пользователя (`reply_id`).

## Что уже реализовано

- HTTP старт задачи: `POST /v1/tasks`.
- HTTP статус сессии: `GET /v1/sessions/{session_id}`.
- HTTP ответ пользователя в сессию: `POST /v1/replies`.
- Callback envelope от `buyer` в `micro-ui`.
- Дедупликация callback-событий в `micro-ui` (`event_id` и `idempotency_key`).
- Отдельный контейнер `browser` с noVNC + CDP (`http://browser:9223`).
- Утилита `buyer/tools/cdp_tool.py` для управления sidecar-браузером через Playwright.
- Трассировка шагов `codex`: сохраняются prompt, stdout/stderr tail, итог шага и лог браузерных команд.
- Ограничение MVP: только 1 активная сессия одновременно.

## Запуск

Перед запуском задайте авторизацию для `codex` (любой один вариант):

```bash
cp .env.example .env
# Вариант 1: API key
# OPENAI_API_KEY=...

# Вариант 2: OAuth auth.json с host-машины
# CODEX_AUTH_JSON_PATH=/absolute/path/to/auth.json

# Режим sandbox для codex внутри buyer.
# Для CDP-доступа к browser-sidecar используйте danger-full-access.
# CODEX_SANDBOX_MODE=danger-full-access

# Опционально: окно/интервал CDP recovery (hotfix устойчивости)
# CDP_RECOVERY_WINDOW_SEC=20
# CDP_RECOVERY_INTERVAL_MS=500

# Куда писать trace-логи buyer (примонтированная папка)
# BUYER_TRACE_DIR=/workspace/.tmp/buyer-observability
```

`CODEX_AUTH_JSON_PATH` монтируется в `buyer` только на этапе runtime и не попадает в image.

```bash
docker compose up --build
```

После запуска:

- `buyer` API: `http://localhost:8000`
- `micro-ui`: `http://localhost:8080` (можно запускать новую сессию прямо из UI)
- noVNC (из sidecar): `http://localhost:6901/vnc.html?autoconnect=1&resize=scale`
- CDP endpoint sidecar (с host-машины): `http://localhost:9223`

## Пример сценария

1. `openclaw` запускает задачу:

```bash
curl -sS -X POST http://localhost:8000/v1/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "task": "Открой сайт и подготовь путь до шага оплаты без реального платежа",
    "start_url": "https://www.litres.ru/",
    "metadata": {
      "budget": 2500,
      "city": "Москва"
    }
  }' | jq
```

2. `buyer` отправляет callbacks в `micro-ui` (`/callbacks`), в панели появляются события.

3. Если приходит `ask_user`, оператор в `micro-ui` вводит ответ. Панель отправляет его в `buyer` как:

```json
{
  "session_id": "...",
  "reply_id": "...",
  "message": "..."
}
```

4. После завершения `buyer` отправляет `scenario_finished`.

## Playwright через sidecar

`buyer` передает в Codex endpoint `BROWSER_CDP_ENDPOINT` и рекомендует использовать:

```bash
python /app/tools/cdp_tool.py --endpoint http://browser:9223 goto --url https://example.com
```

Доступные команды CLI: `goto`, `click`, `fill`, `press`, `wait`, `text`, `title`, `url`, `screenshot`, `html`.

`cdp_tool.py` автоматически пробует fallback-адреса (`localhost`, `127.0.0.1`, `host.docker.internal`) на том же порту, если исходный hostname (например `browser`) не резолвится в текущем окружении.
Для `resolve/connect` используется retry-окно (`CDP_RECOVERY_WINDOW_SEC`, по умолчанию 20с) и интервал (`CDP_RECOVERY_INTERVAL_MS`, по умолчанию 500мс).
Для read-команд `title/text/url` добавлены повторы при transient-ошибках контекста (`Execution context was destroyed`, закрытие page/context/browser).

Если `buyer` получает transient CDP-failure от агента, он не завершает сессию мгновенно: в пределах recovery-окна шаг перезапускается с системным маркером `[CDP_RECOVERY_RESTART_FROM_START_URL]`, и агент должен начать шаг заново с `goto start_url`.

Файлы observability по шагам пишутся в `BUYER_TRACE_DIR/<session_id>/`:

- `step-XXX-prompt.txt` — prompt, с которым запущен `codex`.
- `step-XXX-browser-actions.jsonl` — действия браузера (`goto/click/fill/...`) от `cdp_tool.py`.
- `step-XXX-trace.json` — сводка шага (`preflight`, команда `codex`, длительность, tails stdout/stderr, хвост browser actions).

Чтобы смотреть это в реальном времени в логах контейнера:

```bash
docker compose logs -f buyer | grep -E "codex_step|agent_step|session_|payment_ready"
```

## Контракт callback (MVP)

Общий envelope:

- `event_id`
- `session_id`
- `event_type`
- `occurred_at`
- `idempotency_key`
- `payload`

Текущие типы событий:

- `session_started`
- `agent_step_started`
- `agent_step_finished`
- `ask_user`
- `payment_ready`
- `scenario_finished`

## Важные ограничения MVP

- Состояние хранится только в памяти (`in-memory`).
- После перезапуска контейнеров активные сессии теряются.
- noVNC поднят всегда и без пароля (только для MVP).
- `buyer` ожидает доступность CLI `codex` внутри контейнера (`CODEX_BIN`, по умолчанию `codex`).
- `buyer` требует авторизацию `codex`: либо `OPENAI_API_KEY`, либо `CODEX_AUTH_JSON_PATH` с OAuth `auth.json`.
- Режим sandbox для `codex` в `buyer` управляется `CODEX_SANDBOX_MODE` (по умолчанию `danger-full-access` для стабильного CDP-доступа к `browser-sidecar`).
- Полноценный `middle` не поднимается, его роль выполняет `micro-ui`.
