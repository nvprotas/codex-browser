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
- SberId `scripts-first` для allowlist-доменов с retry auth-пакета и fallback в эвристику/handoff.
- Локальный runtime auth-скриптов в `buyer/scripts` (`tsx + playwright-core` через `npm ci` в image).
- Быстрый `purchase scripts-first` для `litres.ru`: если скрипт надежно доходит до `orderId`, generic `codex exec` не запускается.
- Структурные CDP-команды (`exists`, `attr`, `links`, `snapshot`) и ограничение raw HTML, чтобы не отправлять мегабайтные DOM-дампы в модель.
- Ограничение MVP: только 1 активная сессия одновременно.
- Post-session Codex-анализ знаний: после доставки `scenario_finished` buyer асинхронно анализирует trace завершенной сессии и сохраняет черновики знаний как внутренние артефакты.

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

# Домены SberId allowlist и retry-бюджет auth-пакета
# SBERID_ALLOWLIST=litres.ru,brandshop.ru,kuper.ru,samokat.ru,okko.tv
# SBERID_AUTH_RETRY_BUDGET=1

# Параметры запуска TS auth-скриптов
# AUTH_SCRIPTS_DIR=/app/scripts
# AUTH_SCRIPT_TIMEOUT_SEC=90

# Быстрые purchase-скрипты до generic codex-flow
# PURCHASE_SCRIPT_ALLOWLIST=litres.ru
# PURCHASE_SCRIPT_TIMEOUT_SEC=120
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
    "auth": {
      "provider": "sberid",
      "storageState": {
        "cookies": [],
        "origins": []
      }
    },
    "metadata": {
      "budget": 2500,
      "city": "Москва"
    }
  }' | jq
```

2. `buyer` отправляет callbacks в `micro-ui` (`/callbacks`), в панели появляются события.

3. В `micro-ui` форму запуска задачи можно передавать `auth` JSON (опционально), чтобы запускать SberId-flow без `curl`.

4. Если приходит `ask_user`, оператор в `micro-ui` вводит ответ. Панель отправляет его в `buyer` как:

```json
{
  "session_id": "...",
  "reply_id": "...",
  "message": "..."
}
```

5. После завершения `buyer` отправляет `scenario_finished`.

## Playwright через sidecar

`buyer` передает в Codex endpoint `BROWSER_CDP_ENDPOINT` и рекомендует использовать:

```bash
python /app/tools/cdp_tool.py --endpoint http://browser:9223 goto --url https://example.com
```

Доступные команды CLI: `goto`, `click`, `fill`, `press`, `wait`, `text`, `title`, `url`, `exists`, `attr`, `links`, `snapshot`, `screenshot`, `html`.

Для анализа DOM предпочтительны структурные команды:

```bash
python /app/tools/cdp_tool.py --endpoint http://browser:9223 snapshot --selector body --limit 120
python /app/tools/cdp_tool.py --endpoint http://browser:9223 links --selector body --limit 80
python /app/tools/cdp_tool.py --endpoint http://browser:9223 exists --selector '[data-testid="book__addToCartButton"]'
python /app/tools/cdp_tool.py --endpoint http://browser:9223 attr --selector 'a[href*="/book/"]' --name href
```

Команда `text` предназначена для точечных селекторов. По умолчанию stdout ограничен 4 000 символами и возвращаются поля `text_size`/`truncated`.
Для кастомного лимита используйте `text --max-chars <n>`, где `0` означает без ограничения; полный stdout доступен только через явный `text --full`.
`text --selector body` стоит использовать только как fallback, когда структурных команд недостаточно.

Команда `html` без `--path` возвращает только превью до 20 000 символов и поля `html_size`/`truncated`.
Полный HTML предпочтительно сохранять в файл через `html --path <file>` и анализировать локальными командами.
Если полный HTML нужен именно в stdout, доступен явный escape hatch `html --full`; для кастомного лимита используйте `html --max-chars <n>`, где `0` означает без ограничения.

`cdp_tool.py` автоматически пробует fallback-адреса (`localhost`, `127.0.0.1`, `host.docker.internal`) на том же порту, если исходный hostname (например `browser`) не резолвится в текущем окружении.
Для `resolve/connect` используется retry-окно (`CDP_RECOVERY_WINDOW_SEC`, по умолчанию 20с) и интервал (`CDP_RECOVERY_INTERVAL_MS`, по умолчанию 500мс).
Для read-команд `title/text/url` добавлены повторы при transient-ошибках контекста (`Execution context was destroyed`, закрытие page/context/browser).

Если `buyer` получает transient CDP-failure от агента, он не завершает сессию мгновенно: в пределах recovery-окна шаг перезапускается с системным маркером `[CDP_RECOVERY_RESTART_FROM_START_URL]`, и агент должен начать шаг заново с `goto start_url`.

Файлы observability по шагам пишутся в `BUYER_TRACE_DIR/YYYY-MM-DD/HH-MM-SS/<session_id>/`:

- `step-XXX-prompt.txt` — prompt, с которым запущен `codex`.
- `step-XXX-browser-actions.jsonl` — действия браузера (`goto/click/fill/...`) от `cdp_tool.py`.
- `step-XXX-trace.json` — сводка шага (`preflight`, команда `codex`, длительность, tails stdout/stderr, хвост browser actions) и агрегаты `command_duration_ms`, `inter_command_idle_ms`, `html_commands`, `html_bytes`, `command_breakdown`.
- `knowledge-analysis-prompt.txt` — отдельный prompt post-session analyzer после финального callback.
- `knowledge-analysis.json` — внутренний артефакт с draft-кандидатами знаний (`navigation_hints`, `pitfalls`, `site_overview_plain`, `playbook_candidate`).
- `knowledge-analysis-trace.json` — статус выполнения analyzer, команда, stdout/stderr tail и ссылка на артефакт.

Post-session анализ не отправляет дополнительный callback в `middle`, не влияет на `SessionStatus` и не должен сохранять auth-пакеты, cookies, `storageState`, токены или одноразовые платежные данные. Все кандидаты знаний имеют статус `draft` и не используются автоматически в следующих прогонах.

## Быстрые purchase-скрипты

После SberId-подготовки `buyer` проверяет `PURCHASE_SCRIPT_ALLOWLIST`. Для `litres.ru` он запускает `buyer/scripts/purchase/litres.ts` до generic `codex exec`.

Скрипт принимает `--endpoint`, `--start-url`, `--task`, `--output-path`, извлекает запрос из формата `Ищи книгу <query>`, открывает поиск Litres, выбирает релевантную книгу, добавляет ее в корзину и переходит только до страницы оплаты. Финальную оплату скрипт не выполняет. Если скрипт не нашел запрос, товар, кнопку корзины или `orderId`, он возвращает failed-результат, а `buyer` продолжает текущим generic browser-flow.

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
- `handoff_requested`
- `handoff_resumed`
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
