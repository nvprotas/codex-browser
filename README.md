# buyer MVP: Codex + Playwright + browser-sidecar + microUI

Минимальная версия системы из сервисов:

- `buyer` (FastAPI): принимает задачу от `openclaw`, запускает `codex exec`, оркестрирует шаги и отправляет callback-события.
- `browser` (отдельный sidecar): держит Chromium + Xvfb + x11vnc + noVNC и отдает CDP endpoint для Playwright.
- `micro-ui` (FastAPI + HTML/JS): дополнительный debug-модуль/локальный observer, принимает callbacks, показывает ленту событий, noVNC и форму ответа пользователя (`reply_id`).
- `postgres`: хранит durable состояние `buyer`.
- `eval_service`: запускает eval-кейсы, принимает callbacks от `buyer` и готовит judge/dashboard артефакты.

Roadmap развития после MVP: `docs/buyer-roadmap.md`.

OpenAPI-контракт HTTP API buyer: `docs/openapi.yaml`.
OpenAPI-контракт callback-событий buyer: `docs/callbacks.openapi.yaml`.

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
- Static-инструкции generic buyer-agent вынесены в `docs/buyer-agent/*`; per-step prompt является коротким bootstrap с manifest-ами файлов.
- Динамический контекст generic-agent пишется в trace step dir как стабильный набор `task.json`, `metadata.json`, `memory.json`, `latest-user-reply.md`, `user-profile.md` и sanitized `auth-state.json`; scalar-строки проходят redaction, пустые файлы означают отсутствие optional данных.
- Скрытый automatic purchase-script путь не настраивается в app runtime: после SberId-подготовки покупка идет через generic Codex-agent.
- Litres и Brandshop покупаются через generic Codex-agent после SberId-подготовки; `payment_ready` разрешен только после domain-specific verifier.
- Payment verifier разделяет provider parsers PayEcom/YooMoney и merchant policy; outcome бывает `accepted`, `rejected` или `unverified`.
- Persistent state в Postgres для сессий, событий, ответов, agent memory, auth metadata и ссылок на артефакты.
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

# Абсолютный путь на host к user-buyer-info.md с постоянной информацией о пользователе.
# Пример: /Users/<you>/Documents/user-buyer-info.md
# Файл монтируется только в runtime и не попадает в image.
USER_BUYER_INFO_PATH=

# Режим sandbox для codex внутри buyer.
# Для CDP-доступа к browser-sidecar используйте danger-full-access.
# CODEX_SANDBOX_MODE=danger-full-access

# Режим Codex CLI для generic buyer-flow: low reasoning + отключенный image generation tool.
# CODEX_REASONING_EFFORT=low
# CODEX_IMAGE_GENERATION=disabled

# Опционально: окно/интервал CDP recovery (hotfix устойчивости)
# CDP_RECOVERY_WINDOW_SEC=20
# CDP_RECOVERY_INTERVAL_MS=500

# Куда писать trace-логи buyer (примонтированная папка)
# BUYER_TRACE_DIR=/workspace/.tmp/buyer-observability

# Домены, где buyer пытается SberId scripts-first
# SBERID_ALLOWLIST=litres.ru,brandshop.ru

# Источник SberId cookies. Для развертывания рядом с openclaw включите внешний сервис.
# SBER_AUTH_SOURCE=external_cookies_api
# SBER_COOKIES_API_URL=http://<cookies-service-host>:<port>/cookies
# SBER_COOKIES_API_TIMEOUT_SEC=5
# SBER_COOKIES_API_RETRIES=1

# Параметры запуска TS auth-скриптов
# AUTH_SCRIPTS_DIR=/app/scripts
# AUTH_SCRIPT_TIMEOUT_SEC=90

# Долговременное состояние buyer
# STATE_BACKEND=postgres
# DATABASE_URL=postgresql://buyer:buyer@postgres:5432/buyer
# POSTGRES_DB=buyer
# POSTGRES_USER=buyer
# POSTGRES_PASSWORD=buyer
```

`CODEX_AUTH_JSON_PATH` монтируется в `buyer` и `eval_service` только на этапе runtime и не попадает в image.
`USER_BUYER_INFO_PATH` монтируется в `buyer` только на этапе runtime и не попадает в image.
`buyer` читает `user-buyer-info.md` на каждом агентном шаге и передает его generic-agent через dynamic context file, а не как большой inline-блок prompt-а.
Если агент возвращает `profile_updates`, `buyer` дописывает эти новые факты в конец `user-buyer-info.md`.

Auth-профили для `eval_service` монтируются из host-директории `EVAL_AUTH_PROFILES_HOST_DIR` в `/run/eval/auth-profiles`.
Имя файла совпадает с `auth_profile` в `eval/cases/*.yaml` плюс расширение `.json`: для текущих Litres/Brandshop-кейсов нужны `litres_sberid.json` и `brandshop_sberid.json`.
Например:

```bash
mkdir -p /Users/nikolay/Desktop/eval-auth-profiles
cp /Users/nikolay/Desktop/sber-cookies.json /Users/nikolay/Desktop/eval-auth-profiles/litres_sberid.json
cp /Users/nikolay/Desktop/sber-cookies.json /Users/nikolay/Desktop/eval-auth-profiles/brandshop_sberid.json
```

```bash
docker compose up --build
```

Для развертывания рядом с `openclaw` без eval-сервиса и debug-модуля `micro-ui` используйте отдельный compose-файл. Он поднимает только `postgres`, `browser` и `buyer`; callbacks уходят во внешний `middle` по `MIDDLE_CALLBACK_URL`.

```bash
MIDDLE_CALLBACK_URL=https://middle.example/callbacks \
SBER_COOKIES_API_URL=http://cookies-service:8080/cookies \
docker compose -f docker-compose.openclaw.yml up --build
```

Если `middle` или cookies-сервис запущены на той же host-машине, из контейнера `buyer` можно обращаться к ним через `host.docker.internal`.

Чтобы скопировать extension для `openclaw` в стандартную директорию extensions:

```bash
scripts/install-openclaw-buyer-skill.sh
```

По умолчанию скрипт устанавливает extension в `~/.openclaw/extensions/openclaw-buyer`. Внутри него создаются `package.json`, `openclaw.plugin.json`, `index.js`, `agents/openai.yaml` и `skills/openclaw-buyer/SKILL.md`, как в текущем layout OpenClaw extension на сервере. Для нестандартного пути передайте директорию первым аргументом или задайте `OPENCLAW_BUYER_EXTENSION_DIR`.

Обычный локальный compose рассчитан на доверенную VPS с закрытым периметром: host-порты `5432`, `6901`, `8000`, `8080` и `8090` публикуются только на `127.0.0.1`, а CDP `9223` не публикуется на host вообще и доступен только внутри docker-сети как `http://browser:9223`.
Не открывайте эти endpoints напрямую в интернет. Для удаленного доступа используйте VPN, SSH tunnel или reverse proxy с аутентификацией и TLS. Минимальный SSH tunnel для операторского UI и noVNC:

```bash
ssh -L 8080:127.0.0.1:8080 \
  -L 8000:127.0.0.1:8000 \
  -L 6901:127.0.0.1:6901 \
  -L 8090:127.0.0.1:8090 \
  user@trusted-vps
```

После запуска:

- `buyer` API: `http://localhost:8000` (loopback host или SSH tunnel)
- `micro-ui`: `http://localhost:8080` (loopback host или SSH tunnel; можно запускать новую сессию прямо из UI)
- noVNC (из sidecar): `http://localhost:6901/vnc.html?autoconnect=1&resize=scale` (loopback host или SSH tunnel)
- `eval_service`: `http://localhost:8090` (loopback host или SSH tunnel)
- CDP endpoint sidecar: только внутри docker-сети, `http://browser:9223`

В `docker-compose.openclaw.yml` доступны только:

- `buyer` API: `http://localhost:${BUYER_PORT:-8000}`
- noVNC: `http://localhost:${NOVNC_PORT:-6901}/vnc.html?autoconnect=1&resize=scale`
- Postgres: `localhost:${POSTGRES_PORT:-5432}` для локальной диагностики

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

3. В `micro-ui` форму запуска задачи можно использовать для ручной отладки. Auth JSON через UI/чат передавать не нужно: при `SBER_AUTH_SOURCE=external_cookies_api` cookies берутся из внешнего сервиса.

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

С host-машины CDP `9223` по умолчанию недоступен. Для диагностики запускайте CDP-команды из контейнера `buyer` или используйте одноразовый override, который также привязывает порт только к `127.0.0.1`.

Доступные команды CLI: `goto`, `click`, `fill`, `press`, `wait`, `wait-url`, `wait-selector`, `text`, `title`, `url`, `exists`, `attr`, `links`, `snapshot`, `screenshot`, `html`.

Для анализа DOM предпочтительны структурные команды:

```bash
python /app/tools/cdp_tool.py --endpoint http://browser:9223 snapshot --selector body --limit 60
python /app/tools/cdp_tool.py --endpoint http://browser:9223 links --selector body --limit 50
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

Файлы observability по шагам пишутся в `BUYER_TRACE_DIR/YYYY-MM-DD/HH-MM-SS/<session_id>/`; per-step context files лежат в поддиректории конкретного шага `step-XXX/`:

- `auth-script-litres-trace.jsonl` / `auth-script-brandshop-trace.jsonl` — trace SberId auth-скрипта, включая auth-навигации и cleanup-закрытия page/context/browser.
- `auth-script-result-attempt-XX-<uuid>.json` — JSON-результат конкретной попытки auth-скрипта.
- `step-XXX-prompt.txt` — bootstrap prompt, с которым запущен `codex`: hard rules, task, CDP endpoint и manifest-ы файлов.
- `step-XXX/task.json`, `step-XXX/metadata.json`, `step-XXX/memory.json`, `step-XXX/latest-user-reply.md`, `step-XXX/user-profile.md`, `step-XXX/auth-state.json` — dynamic context files текущего шага; `auth-state.json` содержит только sanitized summary.
- `step-XXX-browser-actions.jsonl` — действия браузера (`goto/click/fill/...`) от `cdp_tool.py`.
- `step-XXX-trace.json` — сводка шага (`preflight`, команда `codex`, модель/стратегия, длительность, tails stdout/stderr, хвост browser actions) и агрегаты `command_duration_ms`, `inter_command_idle_ms`, `browser_busy_union_ms`, `post_browser_idle_ms`, `command_errors`, `codex_tokens_used`, `html_commands`, `html_bytes`, `command_breakdown`.
- `knowledge-analysis-prompt.txt` — отдельный prompt post-session analyzer после финального callback.
- `knowledge-analysis.json` — внутренний артефакт с draft-кандидатами знаний (`navigation_hints`, `pitfalls`, `site_overview_plain`, `playbook_candidate`).
- `knowledge-analysis-trace.json` — статус выполнения analyzer, команда, stdout/stderr tail и ссылка на артефакт.

Post-session анализ не отправляет дополнительный callback в `middle`, не влияет на `SessionStatus` и не должен сохранять auth-пакеты, cookies, `storageState`, токены или одноразовые платежные данные. Все кандидаты знаний имеют статус `draft` и не используются автоматически в следующих прогонах.

## Generic покупка и verifier

После SberId-подготовки app-wired `buyer` не запускает скрытый automatic purchase script: `main.py` не настраивает purchase runner/allowlist, а `PURCHASE_SCRIPT_ALLOWLIST` не является runtime setting. Generic Codex-agent управляет браузером через CDP tool, читает static-инструкции из `docs/buyer-agent/*` и dynamic context files из текущей trace step dir.

Для успешного `payment_ready` результат generic-agent должен пройти verifier со статусом `accepted`: Litres принимает только PayEcom iframe с `orderId`, Brandshop принимает только YooMoney SberPay contract URL с `orderId`. Если provider evidence PayEcom/YooMoney выглядит валидным, но merchant policy для домена неизвестна, `buyer` отправляет `payment_unverified` и `scenario_finished.status=unverified` без payment CTA.

Чтобы смотреть это в реальном времени в логах контейнера:

```bash
docker compose logs -f buyer | grep -E "codex_step|agent_step|agent_stream|session_|payment_ready"
```

Строки логов `buyer` начинаются с имени logger-а в квадратных скобках, например `[app.service]` или `[app.runner]`, чтобы было видно, какой компонент пишет событие.

Ошибки auth-навигаций и закрытий дополнительно попадают в логи контейнера как `auth_script_stderr ...`; полный успешный auth trace остается в JSONL-файлах внутри dated trace-директории.

`micro-ui` также показывает live-поток `agent_stream_event` через общий SSE `/api/events/stream`: туда попадают JSONL-события `codex exec --json`, stderr-диагностика и новые записи `step-XXX-browser-actions.jsonl`. UI обновляется по callback-событиям от `buyer`; периодический polling для списка сессий и событий не используется.
MVP `micro-ui` не добавляет отдельную аутентификацию на SSE endpoint; compose публикует UI только на `127.0.0.1`, а удаленный доступ предполагает trusted контур через VPN/SSH tunnel/authenticated reverse proxy.

## Контракт callback (MVP)

Каноническая OpenAPI-спецификация callback-вызовов находится в `docs/callbacks.openapi.yaml`.

Секреты callback receiver не передаются в query string. Если receiver требует shared secret, `POST /v1/tasks` принимает ephemeral `callback_token`; `buyer` отправляет его как `X-Eval-Callback-Token` и не сохраняет в Postgres/session view.

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
- `agent_stream_event`
- `agent_step_finished`
- `ask_user`
- `handoff_requested`
- `handoff_resumed`
- `payment_ready`
- `payment_unverified`
- `scenario_finished`

`payment_ready` появляется только после verifier status `accepted`. `payment_unverified` означает review-needed/non-success: provider evidence распознан, но merchant policy не подтвердила домен; `micro-ui` и eval не должны показывать платежный CTA или считать такой outcome успешным.

## Важные ограничения MVP

- Состояние задач, сессий, событий, ответов, agent memory и ссылок на артефакты хранится в Postgres при `STATE_BACKEND=postgres`.
- После перезапуска контейнера `buyer` восстанавливает сохраненные статусы и историю, но не автопродолжает активный runner и не восстанавливает утраченную browser page.
- Следующий этап runtime заменяет Redis-подход на Postgres task queue и browser-slot manager: `waiting_user` освобождает agent runner, browser slot удерживается только до TTL ожидания, после timeout сессия завершается без resume.
- Playwright `storageState`, cookies, tokens и localStorage не сохраняются в Postgres; auth-пакет остается session-bound и живет только в памяти текущего процесса.
- noVNC поднят всегда и без пароля (только для MVP), поэтому compose публикует его только на `127.0.0.1`; удаленный доступ должен идти через VPN/SSH tunnel/authenticated reverse proxy.
- `buyer` ожидает доступность CLI `codex` внутри контейнера (`CODEX_BIN`, по умолчанию `codex`).
- `buyer` и LLM-judge в `eval_service` требуют авторизацию `codex`: либо `OPENAI_API_KEY`, либо `CODEX_AUTH_JSON_PATH` с OAuth `auth.json`.
- Режим sandbox для `codex` в `buyer` управляется `CODEX_SANDBOX_MODE` (по умолчанию `danger-full-access` для стабильного CDP-доступа к `browser-sidecar`).
- Полноценный `middle` не поднимается; `micro-ui` остается debug-модулем для локального наблюдения и ручной отладки.
