# Карта репозитория

## Статус документа

Этот документ описывает фактическую структуру репозитория, границы компонентов, входы, выходы, состояние и основные ошибки. Его нужно обновлять при любом изменении кода, которое меняет поведение, публичный контракт, структуру модулей, конфигурацию, хранилище, события, скрипты, тестовые границы или runtime-зависимости.

Если меняется только текстовая документация без изменения поведения кода, обновлять эту карту не обязательно. Если документация фиксирует новый фактический статус кода, карту нужно синхронизировать.

## Общая схема

Репозиторий содержит MVP системы покупки через браузер:

- `buyer`: FastAPI-сервис, который принимает задачу покупки, ведет состояние сессии, управляет агентным шагом `codex exec`, запускает Playwright-скрипты, отправляет callbacks и пишет trace-артефакты.
- `browser`: sidecar-контейнер с Chromium, Xvfb, x11vnc, noVNC и CDP endpoint для управления браузером.
- `micro-ui`: временный `middle`: принимает callbacks от `buyer`, показывает события, проксирует запуск задач и ответы пользователя.
- `postgres`: долговременное состояние `buyer` в локальном compose-окружении.
- `docs`: пользовательские и архитектурные контракты.

Основной runtime-flow:

1. `openclaw` или `micro-ui` вызывает `POST /v1/tasks` у `buyer`.
2. `buyer` создает `SessionState`, переводит ее в `running` и запускает фоновую задачу `_run_session`.
3. `_run_session` отправляет `session_started`, добавляет память агента, подготавливает SberId auth-контекст, затем пробует быстрый purchase-скрипт для allowlist-домена.
4. Если быстрый скрипт не завершил сценарий, `buyer` запускает generic цикл `AgentRunner.run_step()` через `codex exec`.
5. Агент управляет browser-sidecar через `buyer/tools/cdp_tool.py`.
6. `buyer` отправляет callback-события в `middle`/`micro-ui`.
7. При `needs_user_input` сессия переходит в `waiting_user`; ответ приходит через `POST /v1/replies`.
8. При успехе `buyer` отправляет `payment_ready` с `order_id`, затем `scenario_finished` только после domain-specific SberPay verifier; неподдерживаемые домены без verifier не могут завершиться `completed`/`payment_ready`.
9. После финального callback асинхронно запускается post-session анализ знаний и сохраняет внутренние draft-артефакты.

## Корень репозитория

| Путь | Ответственность | Входы | Выходы и эффекты | Ошибки и риски |
| --- | --- | --- | --- | --- |
| `README.md` | Быстрый обзор MVP, запуск compose, основные ограничения. | Нет runtime-входов. | Документирует команды, endpoints, trace-файлы, external cookies env и ограничения. | Может устареть при изменении API, env или compose. |
| `AGENTS.md` | Правила работы агентов в репозитории. | Изменения процессов и договоренностей. | Локальные инструкции для Codex и журнал изменений. | Любое изменение требует записи в журнале. |
| `docker-compose.yml` | Локальный стек `postgres` + `browser` + `buyer` + `micro-ui` + `eval_service`. | `.env`, env `EVAL_CALLBACK_SECRET`, `TRUSTED_CALLBACK_URLS`, `SBER_AUTH_SOURCE`, `SBER_COOKIES_API_URL`, `SBER_COOKIES_API_TIMEOUT_SEC`, `SBER_COOKIES_API_RETRIES`, bind mounts `CODEX_AUTH_JSON_PATH`, `USER_BUYER_INFO_PATH`, `EVAL_AUTH_PROFILES_HOST_DIR`. | Host-порты только на loopback: `127.0.0.1:5432`, `127.0.0.1:6901`, `127.0.0.1:8000`, `127.0.0.1:8080`, `127.0.0.1:8090`; CDP `9223` доступен только внутри docker-сети как `http://browser:9223`; volume `buyer-postgres-data`; `CODEX_AUTH_JSON_PATH` монтируется в `buyer` и `eval_service` как `/run/codex/host-auth`; `buyer` может получать SberId cookies из внешнего сервиса при `SBER_AUTH_SOURCE=external_cookies_api`; eval auth-профили читаются из host-директории и монтируются в `/run/eval/auth-profiles`. | Неверные env/mounts ломают авторизацию Codex, профиль пользователя, external cookies source или eval callbacks; отсутствующая host-директория или файл `<auth_profile>.json` приводит eval-case к `skipped_auth_missing`; недоступный `browser` блокирует агентный шаг; удаленный доступ к loopback-портам требует VPN/SSH tunnel/authenticated reverse proxy. |
| `docker-compose.openclaw.yml` | Standalone compose для развертывания рядом с `openclaw`: только `postgres`, `browser`, `buyer`, без `eval_service` и временного `micro-ui`. | `.env`, обязательные `MIDDLE_CALLBACK_URL` и `SBER_COOKIES_API_URL`, bind mounts `CODEX_AUTH_JSON_PATH`, `USER_BUYER_INFO_PATH`. | `buyer` публикуется на `${BUYER_BIND_ADDR:-127.0.0.1}:${BUYER_PORT:-8000}`, noVNC на `${NOVNC_BIND_ADDR:-127.0.0.1}:${NOVNC_PORT:-6901}`, Postgres на `${POSTGRES_BIND_ADDR:-127.0.0.1}:${POSTGRES_PORT:-5432}`; callbacks отправляются во внешний `middle`; SberId cookies берутся из external cookies API по умолчанию; `buyer` получает `host.docker.internal` для доступа к сервисам host-машины. | Неверный `MIDDLE_CALLBACK_URL` ломает доставку событий в middle; неверный `SBER_COOKIES_API_URL` переводит auth в guest-flow; открытые bind addr требуют доверенного периметра. |
| `pytest.ini` | Общая настройка pytest. | Запуск pytest из корня. | Добавляет `pythonpath = .`. | Не нужен отдельный `PYTHONPATH=.`. |
| `LICENSE` | Лицензия проекта. | Нет. | Правовой артефакт. | Не влияет на runtime. |
| `skills/openclaw-buyer/SKILL.md` | Скилл для агента `openclaw`: как формировать задачу для `buyer` и технически читать статус сессии. | HTTP API `buyer`, роли `openclaw`/`middle`/`buyer`. | Процедура запуска задач из `openclaw` без знаний про auth/callbacks; правила read-only проверки статуса. | Может устареть при изменении API или роли `middle`. |
| `extensions/openclaw-buyer/` | Минимальная metadata/runtime-обвязка OpenClaw plugin для skill-only extension. | `openclaw.plugin.json`, `package.json`, `index.js`. | Дает OpenClaw plugin discovery распознать `openclaw-buyer` и загрузить skill-директорию `skills`. | Без package entrypoint или `configSchema` OpenClaw считает config entry stale или помечает plugin ошибочным. |
| `scripts/install-openclaw-buyer-skill.sh` | Копирует repo-local skill `skills/openclaw-buyer` и plugin metadata в extension-директорию `openclaw`. | Аргумент `<openclaw-buyer-extension-dir>` или `OPENCLAW_BUYER_EXTENSION_DIR`; по умолчанию `~/.openclaw/extensions/openclaw-buyer`; исходные `skills/openclaw-buyer` и `extensions/openclaw-buyer`. | Создает/обновляет `<target>/package.json`, `<target>/openclaw.plugin.json`, `<target>/index.js`, `<target>/skills/openclaw-buyer/SKILL.md` и `<target>/agents/openai.yaml`. | Не удаляет устаревшие файлы в целевой директории; неверный target может установить extension не туда. |

## Документация и контракты

| Путь | Ответственность | Что синхронизировать |
| --- | --- | --- |
| `docs/openapi.yaml` | Канонический HTTP API `buyer`. | Endpoints, модели request/response, HTTP-коды. |
| `docs/callbacks.openapi.yaml` | Канонический callback envelope и payload-ы событий. | Новые события, поля payload, idempotency semantics. |
| `docs/buyer.md` | Согласованная v1-спецификация домена `buyer`. | Граница SberPay, lifecycle, auth, handoff, knowledge-analysis. |
| `docs/architecture-decisions.md` | Decision log обязательных архитектурных решений. | Сначала обновлять его при новых требованиях, затем остальные документы. |
| `docs/buyer-roadmap.md` | Приоритизированный roadmap и ссылки на Linear. | При изменении roadmap синхронизировать Linear issue. |
| `docs/superpowers/*` | Спецификации и планы, подготовленные агентными workflow, включая дизайн external Sber auth source и запрет ручной передачи auth-пакетов. | Исторический контекст планов; не является runtime-контрактом. |
| `docs/repository-map.md` | Эта карта репозитория. | Любые изменения кода, контрактов, ошибок, структуры или runtime-зависимостей. |

## `buyer`: HTTP API и сервисная сборка

### `buyer/app/main.py`

FastAPI-точка входа `buyer`.

| Endpoint | Вход | Выход | Ошибки |
| --- | --- | --- | --- |
| `GET /healthz` | Нет. | `{"status": "ok"}`. | Не оборачивает внутренние зависимости. |
| `POST /v1/tasks` | `TaskCreateRequest`: `task`, `start_url`, optional `callback_url` без query/fragment, ephemeral `callback_token`, `metadata`, `auth`. | `201 TaskCreateResponse`: `session_id`, `status`, `novnc_url`. | `409` при `SessionConflictError`; `422` при невалидном payload или нарушении URL policy. |
| `GET /v1/sessions` | Нет. | Список `SessionView`. | Ошибки repository пробрасываются как `500`. |
| `GET /v1/sessions/{session_id}` | `session_id` в path. | `SessionDetail` с событиями. | `404` при `SessionNotFoundError`. |
| `POST /v1/replies` | `SessionReplyRequest`: `session_id`, `reply_id`, `message`. | `SessionReplyResponse`: `accepted=true`, текущий статус. | `404` при неизвестной сессии; `409` при `ReplyValidationError`; `422` при пустом `message`. |

На старте `main.py` собирает зависимости:

- `SessionStore` поверх `InMemorySessionRepository` или `PostgresSessionRepository`.
- `CallbackClient`.
- `AgentRunner`.
- `PostSessionKnowledgeAnalyzer`.
- `SberIdScriptRunner`.
- `PurchaseScriptRunner`.
- `ExternalSberCookiesClient`, только если `SBER_AUTH_SOURCE=external_cookies_api`.
- `BuyerService`.

Lifecycle:

- `startup`: `store.initialize()`, для Postgres создает pool и миграции.
- `shutdown`: отменяет post-session analysis, закрывает external auth client при наличии, HTTP client callbacks и repository.

### `buyer/app/url_policy.py`

Централизует URL policy для task input.

Правила:

- `start_url`: только http/https, hostname обязателен, userinfo запрещен, hostname/IP не должен быть loopback/private/link-local/metadata/internal.
- `callback_url`: task-provided URL не должен содержать query string или fragment; публичный callback должен быть https.
- Internal callback по http разрешен только при точном совпадении scheme/host/port/path с `MIDDLE_CALLBACK_URL` или `TRUSTED_CALLBACK_URLS`; query/fragment в default/trusted entries запрещены и не участвуют в allowlist.
- Секрет callback receiver передается отдельно как ephemeral `callback_token`, а не в persisted `callback_url`. Legacy query-token может приниматься входящим eval receiver, но `buyer` такой callback URL не allowlist-ит.

### `buyer/app/models.py`

Pydantic-контракты API и внутренних результатов.

Ключевые модели:

- `SessionStatus`: `created`, `running`, `waiting_user`, `completed`, `failed`.
- `TaskAuthPayload`: `provider`, `storageState`/`storage_state`; пользовательские replies не должны переносить auth-пакеты.
- `TaskCreateRequest`: задача, стартовый URL, callback URL, ephemeral callback token, metadata, auth.
- `SessionReplyRequest`: ответ пользователя на конкретный `reply_id`.
- `EventEnvelope`: callback envelope.
- `PaymentEvidence`: сейчас только `source="litres_payecom_iframe"` и `url`.
- `AgentOutput`: структурированный ответ `codex exec`: `status`, `message`, `order_id`, `payment_evidence`, `profile_updates`, `artifacts`.

Ошибки валидации генерирует Pydantic и FastAPI возвращает их как `422`.

### `buyer/app/payment_verifier.py`

Доменные проверки платежной границы перед `payment_ready` и `completed`.

Входы:

- `start_url` сессии;
- `AgentOutput` generic runner или адаптированный `PurchaseScriptResult`.

Выходы:

- `PaymentVerificationResult(accepted, failure_reason)`;
- `PaymentEvidence` из purchase-script artifacts, если найден валидный Litres iframe.

Правила:

- Для Litres принимается только `order_id`, подтвержденный `payment_evidence`/`payment_frame_src` с точным URL `https://payecom.ru/pay_ru?orderId=<тот же order_id>`.
- `http://payecom.ru`, subdomain вроде `evil.payecom.ru`, path prefix вроде `/pay_ru_malicious`, несколько `orderId` или mismatch `orderId` отклоняются.
- Для доменов без domain-specific verifier любой `completed` отклоняется, поэтому `BuyerService` не отправляет `payment_ready` и завершает сценарий как failed вместо success.

### `buyer/app/settings.py`

Читает `.env` и environment variables через `pydantic-settings`.

Группы настроек:

- callbacks: `MIDDLE_CALLBACK_URL`, `TRUSTED_CALLBACK_URLS`, retries, timeout, backoff;
- browser/CDP: `BROWSER_CDP_ENDPOINT`, `CDP_RECOVERY_*`;
- Codex: `CODEX_BIN`, `CODEX_MODEL`, sandbox, reasoning, web search, timeout;
- trace и user profile: `BUYER_TRACE_DIR`, `BUYER_USER_INFO_PATH`;
- SberId и scripts-first: allowlist, timeouts, scripts dir;
- external Sber cookies API: `SBER_AUTH_SOURCE`, `SBER_COOKIES_API_URL`, `SBER_COOKIES_API_TIMEOUT_SEC`, `SBER_COOKIES_API_RETRIES`;
- state: `STATE_BACKEND`, `DATABASE_URL`, pool sizes;
- runtime limit: `MAX_ACTIVE_SESSIONS`.

Ошибки: Pydantic может отклонить enum/числа вне границ; неверные значения env проявятся на startup или при запуске конкретной зависимости.

## `buyer`: состояние и persistence

### `buyer/app/state.py`

Содержит runtime-модель сессии и интерфейс repository.

Компоненты:

- `SessionState`: dataclass с задачей, URL, callback, статусом, events, agent memory, waiting reply, runtime task и wake event.
- `SessionRepository`: protocol для memory/Postgres backends.
- `InMemorySessionRepository`: lock-protected storage для тестов и локальной отладки.
- `SessionStore`: синхронизирует repository и in-process runtime state.

Входы:

- создание сессии: `task`, `start_url`, `callback_url`, `novnc_url`, `metadata`, `auth`;
- переходы статусов;
- события callback;
- agent memory;
- пользовательские replies.

Выходы:

- актуальный `SessionState`;
- wake-up runner через `asyncio.Event`;
- persisted state через repository.

Основные ошибки:

- `SessionConflictError`: достигнут `max_active_sessions` в текущем runtime.
- `SessionNotFoundError`: неизвестный `session_id`.
- `ReplyValidationError`: сессия не ждет ответ, неверный `reply_id`, нет активного runner после рестарта, либо ответ уже потреблен.

Важное ограничение: auth-пакет живет в `_runtime_auth`; persistent backend не должен восстанавливать cookies/localStorage после рестарта. Пользовательский reply не должен быть auth-source.
Передача `storageState` через чат запрещена; external cookies API является только машинным source для текущей сессии и не отменяет это ограничение.

### `buyer/app/persistence.py`

Postgres repository и inline migrations.

Таблицы:

- `buyer_sessions`: основные поля сессии.
- `buyer_events`: callback envelope, позиция, delivery status/error.
- `buyer_replies`: pending/answered/consumed ответы пользователя.
- `buyer_artifacts`: ссылки и очищенная metadata артефактов.
- `buyer_auth_context`: sanitized auth summary без `storageState`.
- `buyer_agent_memory`: история сообщений агента.
- `buyer_schema_migrations`: примененные миграции.

Входы: `SessionState`, auth summary, artifacts, event delivery status.

Выходы: восстановленные `SessionState`, синхронизированные таблицы, sanitized JSON.

Основные ошибки:

- `RuntimeError('Postgres connection pool is not initialized.')`, если pool не поднялся после `initialize()`.
- Ошибки `asyncpg` при недоступной БД, неверном `DATABASE_URL`, SQL/constraint failures.
- `json.JSONDecodeError` может возникнуть при чтении поврежденного JSON из БД через `_json_dict`.

Защита данных:

- `_sanitize_auth_context` удаляет `storageState`, cookies, localStorage, auth/token/password-like ключи.
- `_sanitize_persistent_metadata` дополнительно удаляет stdout/stderr/prompt preview.
- `_sanitize_reply_or_memory_text` и legacy helper `summarize_sberid_auth_reply` остаются защитным редактированием на persistence-границе, но `BuyerService` больше не запрашивает и не принимает auth-пакеты через пользовательский reply.
- `_iter_artifact_paths` не сохраняет path к `storageState`/cookies/localStorage.

### `buyer/app/external_auth.py`

Машинный source SberId cookies из внешнего API.

Входы:

- полный cookies endpoint, timeout и retry budget из настроек;
- `GET` по полному URL из `SBER_COOKIES_API_URL`;
- JSON payload с `cookies`, optional `updatedAt` и `count`.

Выходы:

- `ExternalSberCookiesResult(reason_code, storage_state, metadata, message)`;
- Playwright `storageState` вида `{"cookies": [...], "origins": []}`;
- sanitized metadata: `cookie_count`, `domains`, `updated_at`, `attempts` без cookie values.

Основные reason-коды:

- `auth_external_loaded`: payload валиден и преобразован в runtime auth;
- `auth_external_empty_payload`: сервис вернул пустой массив cookies;
- `auth_external_invalid_payload`: JSON shape или cookie shape невалидны;
- `auth_external_timeout`: исчерпан timeout/retry budget;
- `auth_external_unavailable`: URL не настроен, HTTP/network error или невалидный JSON.

Ограничения: client не реализует write-path внешнего cookies API, не пишет cookies в persistent state и не должен логировать cookie values.

## `buyer`: orchestration

### `buyer/app/service.py`

Главный orchestrator сессии.

Публичные методы:

- `create_session()`: создает состояние, переводит в `running`, запускает `_run_session`.
- `get_session()`, `list_sessions()`: чтение состояния.
- `submit_reply()`: применяет пользовательский ответ.
- `wait_for_post_session_analysis()`, `shutdown_post_session_analysis()`: lifecycle фонового analyzer.

Основной `_run_session`:

1. Отправляет `session_started`.
2. Добавляет `Start URL` и задачу в agent memory.
3. Запускает `_run_sberid_auth_flow`, где `_resolve_session_auth` выбирает inline auth или external cookies API.
4. Запускает `_run_purchase_script_flow`.
5. Если быстрый скрипт не завершил сценарий, циклически вызывает `AgentRunner.run_step`.
6. Обрабатывает статусы `needs_user_input`, `completed`, `failed`.
7. Для transient CDP-сбоев повторяет шаг в пределах `CDP_RECOVERY_WINDOW_SEC` с системным маркером `[CDP_RECOVERY_RESTART_FROM_START_URL]`.
8. Финализирует через `_handle_completed` или `_handle_failed`.

Callback-события:

- `session_started`
- `agent_step_started`
- `agent_stream_event`
- `agent_step_finished`
- `ask_user`
- `handoff_requested`
- `handoff_resumed`
- `payment_ready`
- `scenario_finished`

Логи контейнера:

- ключевые этапы сценария пишутся строками `buyer_progress` с `session_id`, `stage`, `step`, `status`, `order_id` и коротким русскоязычным описанием;
- browser-actions из trace tail пишутся в stdout только как компактные строки `browser_action` с командой, длительностью, URL без query/fragment, селектором и коротким текстовым превью;
- полные JSONL browser-actions и подробные результаты `snapshot`/`html` остаются в trace-артефактах, но не печатаются целиком в логи контейнера.

Доменные проверки:

- Для Litres `completed` принимается только с `order_id` и `payment_evidence` из точного `https://payecom.ru/pay_ru?...orderId=...`.
- Для неподдерживаемых доменов `completed` без domain-specific verifier отклоняется и не приводит к `payment_ready`.
- СБП/SBP/FPS не считается SberPay.

Основные ошибки и реакция:

- `CallbackDeliveryError`: сессия переводится в `failed`, в store пишется fallback `scenario_finished`.
- `SessionNotFoundError`, `SessionConflictError`, `ReplyValidationError`: runner тихо завершает текущую задачу, потому что состояние уже недоступно/невалидно.
- Любое другое исключение: `_handle_failed(..., "Непредвиденная ошибка: ...")`; если callback тоже падает, пишется fallback event и status `failed`.
- Ошибка quick purchase script не валит сценарий: добавляется `[PURCHASE_SCRIPT_FALLBACK]`, затем generic flow.
- Невалидный inline `auth.storageState` не вызывает `ask_user` и `handoff`: auth summary получает `reason_code='auth_inline_invalid_payload'`, `mode='guest'`, `path='guest'`.
- Если inline auth отсутствует и подключен external client, `BuyerService` вызывает внешний cookies API, сохраняет успешный пакет через `SessionStore.set_auth()` только в runtime auth и добавляет sanitized `external_auth` metadata в auth summary.
- Если external client вернул `auth_external_unavailable`, `auth_external_timeout`, `auth_external_invalid_payload` или `auth_external_empty_payload`, `BuyerService` не запускает auth script, фиксирует reason-code и продолжает guest-flow.
- Ошибки SberId script auth `auth_refresh_requested`, `auth_failed_redirect_loop` и `auth_failed_invalid_session` не просят новый auth-пакет через `/v1/replies`; сервис добавляет `[SBERID_AUTH_HEURISTIC_REQUIRED]` и продолжает через heuristic/generic путь, где handoff возможен только как ручной шаг без передачи cookies/storageState через чат.

### `buyer/app/callback.py`

HTTP-клиент доставки callback-событий.

Входы: `callback_url`, `EventEnvelope`.

Выходы: успешный HTTP POST или `CallbackDeliveryError`.

Поведение:

- `build_envelope()` создает `event_id` и `idempotency_key`.
- `deliver()` делает `callback_retries` попыток с exponential backoff и jitter.
- `response.raise_for_status()` делает любой не-2xx ответ ошибкой доставки.

Основная ошибка: `CallbackDeliveryError('Не удалось доставить callback ...')`.

### `buyer/app/runner.py`

Запускает generic агентный шаг через `codex exec`.

Входы:

- `session_id`, `step_index`;
- задача, `start_url`, metadata, auth summary;
- agent memory и последний ответ пользователя;
- callback для live stream.

Выход:

- `AgentOutput` со статусом `needs_user_input`, `completed` или `failed`;
- trace artifacts: prompt path, sha256, stdout/stderr tail, command, model strategy, browser actions metrics.

Внутренний flow:

1. Подготавливает trace context: `BUYER_TRACE_DIR/YYYY-MM-DD/HH-MM-SS/<session_id>/step-XXX-*`.
2. Делает CDP preflight через `/app/tools/cdp_tool.py url`.
3. Загружает user profile и строит prompt.
4. Проверяет `OPENAI_API_KEY` или `/root/.codex/auth.json`.
5. Формирует попытки модели: `single` или `fast_then_strong`.
6. Запускает `codex exec --json --output-schema ... -o <tmp> <prompt>`.
7. Параллельно стримит stdout/stderr и новые browser action records; stdout/stderr читаются chunk-based без лимита длины одной строки, а длинные строки внутри stream payload обрезаются перед callback/storage.
8. Парсит output JSON в `AgentOutput`.
9. При fallback на strong модель сбрасывает браузер на `start_url`, если до этого не было mutating-команд.

Основные ошибки и failure reasons:

- preflight CDP failed: возвращается `AgentOutput(status='failed')` с описанием недоступности browser-sidecar.
- нет Codex auth: `fallback_reason='no_api_key_or_oauth'`.
- `FileNotFoundError` при запуске `CODEX_BIN`: `failure_reason='binary_missing'`.
- timeout: `failure_reason='timeout'`.
- non-zero return code: `failure_reason='process_failed'`, включая 401 и 429 с отдельными сообщениями.
- невалидный JSON/output schema: `failure_reason='parse_output_failed'`.
- неподдерживаемый `status`: `failure_reason='invalid_status'`.
- невозможный внутренний случай без попыток: `RuntimeError('codex step finished without attempts')`.

### `buyer/app/prompt_builder.py`

Строит prompt для `codex exec`.

Входы: задача, start URL, CDP endpoint, preflight summary, metadata, redacted auth payload, auth context, user profile, memory, последний reply.

Выход: текст prompt с правилами управления `cdp_tool.py`, SberPay-boundary, Litres-specific payment evidence и schema-only ответом.

Ошибки явно не выбрасывает; риски связаны с устаревшими инструкциями, если меняется доменный контракт.

### `buyer/app/user_profile.py`

Работает с долговременным профилем пользователя.

Входы: путь к markdown-файлу, max chars, `profile_updates`.

Выходы:

- `UserProfileSnapshot(text, truncated, missing)`;
- дописанные строки в профиль пользователя.

Ошибки:

- `FileNotFoundError` при чтении трактуется как пустой профиль.
- `OSError`/ошибки записи при append не перехватываются полностью на верхнем уровне `append_profile_updates`; `BuyerService._persist_profile_updates` не оборачивает их отдельно.
- Нормализация отбрасывает пустые и дублирующиеся updates.

### `buyer/app/_utils.py`

Небольшие helpers: `tail_text`, `head_text`, безопасное удаление файла, duration ms, имена trace date/time.

Ошибки: `remove_file_quietly` гасит `FileNotFoundError`; остальные функции ошибок обычно не генерируют.

## `buyer`: scripts-first runtime

### `buyer/app/script_runtime.py`

Общие helpers для TypeScript-скриптов.

Входы: registry `ScriptSpec`, output path, stdout/stderr.

Выходы:

- snapshot registry;
- JSON payload из output-файла или stdout;
- уникальный путь output-файла для script-runner попытки;
- best-effort удаление stale script output;
- trimmed stdio artifacts.

Ошибки чтения output-файла, JSON decode и Unicode decode гасятся; при невалидном файле используется stdout fallback.

### `buyer/app/auth_scripts.py`

Python-runner SberId TypeScript-скриптов.

Registry:

- published: `brandshop.ru`, `litres.ru`;
- draft: `kuper.ru`, `samokat.ru`, `okko.tv`.

Входы:

- `session_id`, домен, `start_url`, Playwright `storageState`, номер попытки;
- настройки scripts dir, CDP endpoint, timeout, trace dir.

Выход:

- `AuthScriptResult(status, reason_code, message, artifacts)`;
- trace/output files в `BUYER_TRACE_DIR/<session_id>/`: runner удаляет legacy stale `auth-script-result.json` и `auth-script-result-attempt-XX.json` перед запуском и передает скрипту уникальный `auth-script-result-attempt-XX-<uuid>.json` для текущей попытки;
- входной Playwright `storageState` передается TypeScript-скрипту через временный файл вне workspace с правами `0600` и удаляется в `finally`, поэтому raw auth state не остается в `BUYER_TRACE_DIR`.

Reason codes:

- `auth_ok`
- `auth_failed_payload`
- `auth_failed_redirect_loop`
- `auth_failed_invalid_session`
- `auth_refresh_requested`

Основные ошибки и результаты:

- нет скрипта в registry: `auth_failed_invalid_session`.
- lifecycle не `publish`: `auth_refresh_requested`.
- нет файла скрипта или TSX runtime: `auth_failed_invalid_session`.
- CDP endpoint не резолвится: `auth_failed_invalid_session`.
- Node.js отсутствует: `auth_failed_invalid_session`.
- timeout скрипта: `auth_failed_invalid_session`.
- любой non-zero exit code: `auth_failed_invalid_session`; JSON payload из output/stdout может сохраняться только как диагностический `script_result_payload` в artifacts и не может стать успешным `auth_ok`.
- process failed без валидного payload: `auth_failed_invalid_session`.
- невалидный JSON payload: `auth_failed_invalid_session`.
- `_resolve_single_http_endpoint` может поднять `RuntimeError`, если `/json/version` не содержит `webSocketDebuggerUrl`.
- `resolve_cdp_endpoint` может поднять `RuntimeError`, если не удалось подключиться ни к одному fallback endpoint.

### `buyer/app/purchase_scripts.py`

Python-runner быстрых purchase-скриптов.

Registry:

- published: `litres.ru` -> `purchase/litres.ts`.

Входы:

- `session_id`, домен, `start_url`, текст задачи;
- scripts dir, CDP endpoint, timeout, trace dir.

Выход:

- `PurchaseScriptResult(status, reason_code, message, order_id, artifacts)`.
- trace/output files в `BUYER_TRACE_DIR/<session_id>/`: runner удаляет legacy stale `purchase-script-result.json` перед запуском и передает скрипту уникальный `purchase-script-result-<uuid>.json` для текущей попытки.

Reason codes:

- `purchase_script_not_registered`
- `purchase_script_not_published`
- `purchase_script_missing`
- `purchase_script_runtime_missing`
- `purchase_script_cdp_resolve_failed`
- `purchase_script_timeout`
- `purchase_script_process_failed`
- `purchase_script_invalid_json`
- script-specific коды из TypeScript payload.

Любой non-zero exit code возвращается как `purchase_script_process_failed`: JSON payload из output/stdout может сохраняться только как диагностический `script_result_payload` в artifacts и не может стать успешным `completed`. Остальные ошибки runner возвращает как failed-result, а `BuyerService` делает generic fallback.

### `buyer/scripts/*`

TypeScript Playwright-скрипты, запускаемые через `tsx` и `playwright-core`.

Общий CLI-контракт auth-скриптов:

- `--endpoint`
- `--start-url`
- `--storage-state-path`
- `--output-path`

Общий выход auth-скриптов:

- JSON `status`, `reason_code`, `message`, `artifacts`;
- stdout дублирует payload;
- trace JSONL рядом с output.

`buyer/scripts/sberid/litres.ts`:

- добавляет cookies из `storageState` в существующий browser context;
- открывает login/profile pages Litres;
- ищет Sber ID entry, следит за redirect loop на `id.sber.ru`;
- проверяет авторизацию по markers `Мои книги` и `Профиль`;
- возвращает `auth_ok` только при подтвержденной авторизации.

`buyer/scripts/sberid/brandshop.ts`:

- готовит контекст Brandshop;
- ищет profile/login/Sber ID controls;
- валидирует возврат на ожидаемый host и auth markers;
- возвращает те же auth reason codes.

`buyer/scripts/sberid/kuper.ts`, `samokat.ts`, `okko.ts`:

- находятся в registry как draft и не запускаются автоматически до publish.

Общий CLI-контракт purchase-скрипта Litres:

- `--endpoint`
- `--start-url`
- `--task`
- `--output-path`
- optional `--trace-path`.

`buyer/scripts/purchase/litres.ts`:

- извлекает поисковый запрос из русскоязычной задачи;
- открывает поиск Litres;
- выбирает релевантную книгу;
- добавляет в корзину;
- проверяет, что корзина содержит ровно одну целевую книгу;
- переходит к оплате;
- выбирает "Российская карта";
- ждет iframe `payecom.ru/pay_ru` и извлекает `orderId`;
- не выполняет финальный платеж.

Script-specific failure codes Litres:

- `purchase_script_query_missing`
- `purchase_script_no_candidates`
- `purchase_script_add_to_cart_missing`
- `purchase_script_cart_ambiguous`
- `purchase_script_checkout_missing`
- `purchase_script_sberpay_unavailable`
- `purchase_script_russian_card_missing`
- `purchase_script_continue_missing`
- `purchase_script_order_missing`
- `purchase_script_failed`
- `purchase_script_unhandled`

## `buyer`: browser tooling

### `buyer/tools/cdp_tool.py`

CLI-утилита управления browser-sidecar через Playwright CDP. Это основной инструмент, который получает агент внутри prompt.

Команды:

- mutating: `goto`, `click`, `fill`, `press`, `wait`, `screenshot`, `html --path`;
- read/inspect: `title`, `url`, `text`, `exists`, `attr`, `links`, `snapshot`, `html`.

Общие входы:

- `--endpoint`, default из `BROWSER_CDP_ENDPOINT`;
- `--timeout-ms`;
- `--recovery-window-sec`;
- `--recovery-interval-ms`;
- command-specific arguments.

Выход:

- JSON в stdout;
- exit code `0`, если `ok=true`, иначе `1`;
- action log JSONL в `BUYER_CDP_ACTIONS_LOG_PATH`, если переменная задана.

Поведение:

- HTTP endpoint резолвится через `/json/version` в websocket endpoint.
- При недоступном hostname пробуются fallback: `localhost`, `127.0.0.1`, `host.docker.internal`.
- `ensure_page()` выбирает существующую не пустую страницу нейтрально: сначала HTTP(S), затем прочие non-blank, а среди равных кандидатов последнюю по `context_index/page_index`; hardcoded доменного приоритета нет.
- `goto --url` до подключения к Playwright валидирует URL той же public http/https policy, что `start_url`: без userinfo, loopback/private/link-local/metadata hosts, `host.docker.internal` и внутренних suffixes.
- После подключения к странице `cdp_tool` ставит Playwright `context.route("**/*", ...)` guard на время команды: document/navigation requests, включая redirects и iframe-навигации, проходят через ту же URL policy и при нарушении abort-ятся `blockedbyclient`; ненавигационные asset/XHR requests не блокируются этим guard.
- read-команды ретраятся при transient context errors.
- `text` и `html` ограничивают stdout по умолчанию.
- `snapshot` собирает не только базовые интерактивные и текстовые элементы (`a`, `button`, `input`, `textarea`, `select`, `[role]`, `[data-testid]`, заголовки, `label`, `p`), но и option-like элементы товара на `div`/`span`/`li` с ограниченными признаками варианта: классы `product-plate`/`size`/`variant`/`option`/`sku`/`swatch`, allowlist `data-size`/`data-value`/`data-variant`/`data-sku`/`data-color`/`data-option`, `aria-selected`/`aria-checked`/`aria-disabled`/`disabled`. Чтобы не раздувать ответ, диагностические поля `class`, `id`, `disabled`, `aria_selected`, `aria_checked`, `aria_disabled` и allowlist `data` добавляются прежде всего к option-like item и только при полезных состояниях у обычных элементов.

Основные ошибки:

- `CDP_CONFIG_ERROR`: отрицательное recovery window или interval <= 0.
- `CDP_CONNECT_ERROR`: не удалось подключиться к CDP в пределах recovery window.
- `CDP_COMMAND_TIMEOUT`: Playwright timeout.
- `CDP_COMMAND_ERROR`: ошибка команды/селектора/страницы или нарушение URL policy в `goto`.
- `CDP_TRANSIENT_ERROR`: transient закрытие page/context/browser или destroyed execution context.
- `RuntimeError('CDP endpoint не вернул webSocketDebuggerUrl.')`.
- `RuntimeError('Не удалось подключиться к browser-sidecar ни по одному CDP endpoint...')`.

## `buyer`: post-session knowledge analysis

### `buyer/app/knowledge_analyzer.py`

Асинхронный analyzer, который запускается после доставки финального `scenario_finished`.

Входы:

- `PostSessionAnalysisSnapshot`: session metadata, outcome, message, optional order id, artifacts, events;
- trace directory с step traces и browser actions.

Выходы:

- `knowledge-analysis-prompt.txt`;
- `knowledge-analysis.json`;
- `knowledge-analysis-trace.json`;
- return status dict: `completed`, `failed` или `skipped`.

Особенности запуска Codex:

- prompt записывается в `knowledge-analysis-prompt.txt` и передается в `codex exec` через stdin, чтобы большой snapshot не попадал в argv процесса;
- `knowledge_analysis_schema.json` использует строгие object schemas без произвольных дополнительных полей для совместимости со Structured Outputs;
- перед запуском пишется `knowledge_analysis_prompt_prepared` с размерами prompt и основных секций входного JSON без логирования полного содержимого.

Security boundaries:

- analyzer работает в sandbox `read-only`;
- output paths ограничены фиксированными именами внутри `session_dir`;
- выполняется redaction auth, cookies, tokens, order/payment ids, localStorage, sensitive URL query/path segments.

Основные статусы/ошибки:

- `skipped/no_api_key_or_oauth`: нет авторизации Codex.
- `failed/codex_binary_missing`: `CODEX_BIN` не найден.
- `failed/timeout`: превышен `CODEX_TIMEOUT_SEC`.
- `failed/codex_failed`: non-zero return code.
- `failed/parse_failed`: невалидный JSON output.
- `ValueError('Не удалось подобрать безопасную директорию trace-сессии.')`.
- `ValueError('Небезопасная директория trace-сессии.')`.
- `ValueError('Директория trace-сессии должна находиться внутри trace_root.')`.
- `ValueError('Директория trace-сессии не должна проходить через symlink.')`.
- `ValueError('Недопустимое имя файла knowledge analysis.')`.
- `ValueError('Файл knowledge analysis должен находиться внутри session_dir.')`.
- `ValueError('Файл knowledge analysis не может быть директорией.')`.
- `ValueError('Записанный файл knowledge analysis вышел за пределы session_dir.')`.

### JSON-схемы

| Путь | Назначение | Кто использует |
| --- | --- | --- |
| `buyer/app/codex_output_schema.json` | Schema для structured output generic `codex exec`. | `AgentRunner`. |
| `buyer/app/knowledge_analysis_schema.json` | Schema для post-session analysis. | `PostSessionKnowledgeAnalyzer`. |

## `eval_service`

`eval_service` запускает eval cases, принимает callbacks от `buyer`, хранит manifests и готовит данные для judge/dashboard.

Runtime auth-профили берутся из host-директории `EVAL_AUTH_PROFILES_HOST_DIR`, которая bind-mounted в контейнер как `/run/eval/auth-profiles` и внутри сервиса читается через `EVAL_AUTH_PROFILES_DIR`. Для `auth_profile: litres_sberid` ожидается файл `litres_sberid.json`. Эти файлы являются секретами, не входят в image и не должны храниться в repo.

### `eval_service/app/orchestrator.py`

Создает eval run и оркестрирует последовательный запуск case через `buyer`.

Поведение:

- `POST /runs` создает `manifest.json`, переводит run в `running`, планирует выполнение case в фоновой задаче и сразу возвращает текущий manifest; первый case на момент ответа может еще оставаться `pending`;
- execution идет последовательно до `waiting_user` или terminal state; после `payment_ready` выдерживается grace period перед `finished`;
- для тестов и управляемого запуска поддерживается injectable `orchestrator_run_scheduler`, который может выполнить run inline или только собрать coroutine;
- operator reply продолжает запускать resume orchestration через отдельный scheduler из callback слоя.

Ошибки:

- неизвестный selected case возвращает `422`;
- ошибка `buyer.create_task` переводит текущий case в `failed` и позволяет перейти к следующему case;
- timeout ожидания case переводит case в `timeout`;
- необработанная ошибка фоновой задачи переводит run в `failed`.

### `eval_service/app/case_registry.py`

Загружает YAML templates из `eval/cases/*.yaml` и разворачивает variants в `EvalCase`.

Поведение:

- файл с `enabled: false` полностью пропускается registry и не попадает в selectable eval cases;
- `brandshop_purchase_smoke.yaml` временно отключен до появления domain-specific SberPay verifier для `brandshop.ru`;
- активные executable smoke-case сейчас: `litres_purchase_book_001`, `litres_purchase_book_002`, `litres_purchase_book_003`.

### `eval_service/app/callback_urls.py`

`build_buyer_callback_url()` строит callback URL из `EVAL_CALLBACK_BASE_URL` без секретов в query string. `build_buyer_callback_token()` возвращает `EVAL_CALLBACK_SECRET` отдельно; eval orchestrator передает его в `buyer` как `callback_token`, а `buyer` отправляет callback header `X-Eval-Callback-Token`.

### `eval_service/app/callbacks.py`

FastAPI endpoints:

| Endpoint | Вход | Выход | Ошибки |
| --- | --- | --- | --- |
| `POST /callbacks/buyer` | `BuyerCallbackEnvelope`, optional `token` query или `X-Eval-Callback-Token`. | `CallbackAcceptedResponse` с eval ids и состоянием case. | `401` при неверном callback token; `404` для неизвестного run/case/session; `409` при mismatch session; `422` при malformed terminal payload. |
| `POST /runs/{eval_run_id}/cases/{eval_case_id}/reply` | `OperatorReplyRequest`: `message`, optional `reply_id`. | `OperatorReplyResponse` и resume orchestration. | `404` для неизвестного run/case; `409` если case не ждет reply или `reply_id` не совпал; ошибки buyer/restart resume пробрасываются. |

Callback state rules:

- `ask_user` переводит case в `waiting_user` и сохраняет `waiting_reply_id`;
- `agent_step_started` и `handoff_resumed` очищают stale waiting context и возвращают case в `running`;
- `payment_ready` требует `payload.order_id` и `payload.message`, иначе возвращает `422` без изменения manifest;
- `scenario_finished` требует `payload.status` `completed|failed` и `payload.message`, иначе возвращает `422` без изменения manifest;
- поздние валидные callbacks для terminal case дописываются в manifest без изменения terminal state.

### `eval_service/app/run_store.py`

Файловое хранилище eval runs.

Входы: `EvalRunManifest`, case updates, callback events.

Выходы: `manifest.json`, `summary.json`, поиск case по `session_id`.

Защита данных:

- callback events перед записью в manifest сохраняются в redacted форме;
- `idempotency_key` заменяется стабильным `sha256:<digest>` и продолжает участвовать в дедупликации;
- payload очищается от raw token/query/payment/order secrets через eval redaction sanitizer;
- raw `order_id`, payment URL и callback token не должны попадать в durable `manifest.json`.

### `eval_service/app/api.py`

HTTP API для eval UI: cases/runs/run detail/judge/dashboard. Run detail дополнительно sanitizes callbacks и artifact paths перед отдачей наружу; waiting question извлекается из `ask_user.payload.message` с legacy fallback на `question`.

Judge flow:

- `POST /runs/{eval_run_id}/judge` по умолчанию синхронно собирает judge input из case, callbacks и trace summary; при payload/query `async=true` помечает еще не оцененные terminal cases как `judge_pending`, планирует фоновый judge-job и сразу возвращает `202`;
- `write_judge_input()` записывает полный redacted `judge-input.json` без дополнительной фильтрации/обрезки evidence; дополнительно добавляет `evidence_files` с путями к `manifest.json`, самому judge input, будущему evaluation output, trace JSON, browser actions JSONL и screenshots;
- `JudgeRunner` вызывает `codex exec --output-schema ... -o <evaluation.json>` без передачи prompt в argv;
- judge prompt передается в `codex exec` через stdin, остается коротким и содержит инструкции, краткое описание структуры evidence-файлов, идентификаторы case и пути к ним; содержимое `judge-input.json`, callbacks и traces не инлайнится в prompt;
- `evaluation_schema.json` совместима со strict response_format: все object `properties` перечислены в `required`; логически optional поля `evidence_ref` передаются как nullable значения;
- после успешной schema/identity validation `JudgeRunner` перезаписывает `judge_metadata` серверными значениями `backend=codex_exec` и `model=EVAL_JUDGE_MODEL`, чтобы metadata не зависела от сгенерированного model output;
- при timeout, non-zero exit, невалидном JSON/schema mismatch или identity mismatch пишется fallback evaluation со skipped/failed checks.
- async judge-job пишет промежуточный `judge_input` в `artifact_paths`, после каждого case обновляет state на `judged`/`judge_failed`, пересчитывает `summary.json`, а повторный запуск пропускает уже `judged` cases с валидным evaluation artifact.

## `micro-ui`

`micro-ui` временно выполняет роль `middle` для локального MVP.

### `micro-ui/app/main.py`

FastAPI endpoints:

| Endpoint | Вход | Выход | Ошибки |
| --- | --- | --- | --- |
| `GET /healthz` | Нет. | `{"status": "ok"}`. | Нет явных. |
| `GET /` | HTTP request. | HTML shell `templates/index.html`. | Ошибки шаблона/статики как `500`. |
| `POST /callbacks` | `EventEnvelope`. | `CallbackAck(accepted, duplicate)`. | `422` при невалидном envelope. |
| `GET /api/events` | optional `session_id`. | Список events. | Нет явных. |
| `GET /api/events/stream` | optional `session_id`, SSE request. | SSE `data: <EventEnvelope>` и keepalive. | Disconnect завершает generator; queue overflow управляется store. |
| `GET /api/sessions` | Нет. | Сводки сессий. | Нет явных. |
| `/api/eval/{path}` | GET/POST proxy к `eval_service`. | Ответ `eval_service`. | HTTP status от `eval_service` пробрасывается; network/timeout ошибки -> `502`. Для `POST /runs` и `POST /runs/{eval_run_id}/judge` используется длинный timeout 650s, так как запуск eval и LLM-judge могут занимать минуты. |
| `POST /api/tasks` | `TaskCreateRequest`. | Ответ `buyer /v1/tasks`. | HTTP status от buyer пробрасывается; любые другие ошибки -> `502`. |
| `POST /api/reply` | `ReplySubmitRequest`. | `{forwarded: true, buyer_response}`. | HTTP status от buyer пробрасывается; любые другие ошибки -> `502`. |

Eval tab вызывает `POST /runs/{eval_run_id}/judge` в async-режиме и продолжает обновлять run detail polling-ом, пока есть cases в `judge_pending`.

### `micro-ui/app/store.py`

In-memory callback store.

Входы: callback envelope от `buyer`.

Выходы:

- дедупликация по `event_id` и `idempotency_key`;
- список events;
- список session summaries;
- SSE delivery в session/global subscribers.

Ошибки и ограничения:

- состояние не переживает рестарт `micro-ui`;
- queue ограничена `maxsize=200`; при переполнении самый старый элемент выкидывается;
- `ask_question` берется из `ask_user.payload.message`, а `question` поддерживается только как legacy fallback;
- waiting context очищается на `agent_step_started`, `handoff_resumed`, `payment_ready`, `operator_reply` и `scenario_finished`.

### `micro-ui/app/models.py` и `settings.py`

Pydantic-модели callback, task proxy, reply proxy и session summary. `BUYER_BASE_URL` по умолчанию `http://buyer:8000`.

### Frontend assets

- `micro-ui/app/templates/index.html`: HTML shell.
- `micro-ui/app/static/app.js`: запуск задач, отправка replies, SSE stream, UI state.
- `micro-ui/app/static/eval.js`: eval shell; при инициализации загружает cases, последний eval run через `GET /runs` + `GET /runs/{eval_run_id}`, dashboard и operator reply; operator reply отправляет в eval_service только `reply_id` и `message`, без лишнего `session_id`; активный running eval-run периодически обновляется через `GET /runs/{eval_run_id}` до ожидания пользователя или terminal state; в Run detail группирует `agent_stream_event` по `source/stream`, показывает компактные последние summary и счетчики, а raw payload/details оставляет в раскрываемом блоке.
- `micro-ui/app/static/app.css`: стили панели; блок сессий и единая лента событий полноширинные, лента событий имеет ограниченную высоту, фильтры по всем известным `event_type` и прокручивается на уровне списка без внутренней прокрутки payload-карточек.

При изменении callback payload или session summary нужно синхронизировать Python store, JS и OpenAPI callback contract.

## `browser`

### `browser/Dockerfile`

Собирает sidecar на Python slim bookworm с Chromium, Xvfb, x11vnc, fluxbox, noVNC, socat, websockify.

Выходные порты:

- `6901`: noVNC websockify; в compose публикуется на host только как `127.0.0.1:6901`.
- `9223`: CDP proxy через socat; в compose не публикуется на host и доступен соседним контейнерам через docker-сеть как `http://browser:9223`.

### `browser/entrypoint.sh`

Runtime flow:

1. Поднимает Xvfb на `DISPLAY=:99`.
2. Ждет X11 socket.
3. Запускает fluxbox.
4. Запускает x11vnc на loopback `5900`.
5. Запускает Chromium с remote debugging port `9222`.
6. Ждет `/json/version`.
7. Прокидывает `9223 -> 127.0.0.1:9222` через socat.
8. Запускает websockify/noVNC на `6901`.
9. Ждет завершения Chromium.

Ошибки:

- Xvfb socket не появился: пишет tail `/tmp/xvfb.log` и выходит `1`.
- noVNC entrypoint не найден: пишет listing и выходит `1`.
- Если Chromium не готов, compose healthcheck `browser` будет failing.

## Docker-образы сервисов

| Путь | Ответственность | Важные детали |
| --- | --- | --- |
| `buyer/Dockerfile` | Python 3.12 image для `buyer`. | Ставит Python deps, Node/npm, пробует `npm install -g @openai/codex`, делает `npm ci` в `/app/scripts`, копирует `buyer`. |
| `buyer/docker/entrypoint.sh` | Подготовка OAuth auth.json и запуск uvicorn. | Копирует mounted `/run/codex/host-auth` в `/root/.codex/auth.json`, валидирует непустой файл, запускает uvicorn с `--no-access-log`, чтобы healthcheck/access-log не забивал контейнерные логи. |
| `eval_service/docker/entrypoint.sh` | Подготовка OAuth auth.json и запуск команды контейнера. | Копирует mounted `/run/codex/host-auth` в `/root/.codex/auth.json`, чтобы LLM-judge мог запускать `codex exec` через OAuth auth.json. |
| `micro-ui/Dockerfile` | Python 3.12 image для `micro-ui`. | Ставит deps и запускает uvicorn на `8080` с `--no-access-log`. |
| `eval_service/Dockerfile` | Python 3.12 image для `eval_service`. | Ставит deps, Node/npm и Codex CLI, запускает uvicorn на `8090` через entrypoint с `--no-access-log`. |
| `browser/Dockerfile` | Browser sidecar image. | См. раздел `browser`. |

## Данные, состояние и артефакты

| Данные | Где живут | Что содержит | Что нельзя сохранять |
| --- | --- | --- | --- |
| Runtime auth payload | Память `SessionStore._runtime_auth` | `TaskAuthPayload` с `storageState`; пакет может прийти inline из `POST /v1/tasks` или быть получен из external cookies API и преобразован в storage state. | Нельзя переносить в Postgres или передавать через пользовательский чат. |
| Persistent sessions | Postgres `buyer_sessions` | Статус, task, URL, callback, metadata. | Cookies/localStorage/tokens. |
| Events | Postgres `buyer_events`, память `micro-ui`, eval `manifest.json` | Callback envelope и sanitized payload; eval manifest хранит redacted callback events. | Raw stdout/stderr/auth secrets в persistent metadata; raw callback/payment/order secrets не должны попадать в eval manifest. |
| Agent memory | Postgres `buyer_agent_memory` | Последние сообщения для prompt context; auth-пакеты не должны попадать через пользовательские replies. | Нужно следить за утечками чувствительных данных при новых источниках. |
| Replies | Postgres `buyer_replies` | Pending/answered/consumed ответы пользователя; legacy auth-like текст дополнительно редактируется на persistence-границе. | Не предназначено для auth-payload reuse. |
| Trace artifacts | `BUYER_TRACE_DIR` | prompts, browser actions JSONL, step trace JSON, script traces, knowledge analysis. | Knowledge analysis дополнительно редактирует auth/payment/order secrets. |
| User profile | `BUYER_USER_INFO_PATH` | Долговременные пользовательские факты. | Auth, cookies, storageState, платежные данные, одноразовые детали заказа. |

## Внешние зависимости

| Зависимость | Кто использует | Назначение | Типовые отказы |
| --- | --- | --- | --- |
| OpenAI/Codex auth | `AgentRunner`, `PostSessionKnowledgeAnalyzer` | Запуск `codex exec`. | Нет `OPENAI_API_KEY` и `/root/.codex/auth.json`; 401; 429. |
| Codex CLI | `AgentRunner`, analyzer | Structured agent step и post-session analysis. | `CODEX_BIN` не найден; timeout; non-zero return. |
| browser-sidecar CDP | `cdp_tool.py`, script runners | Управление Chromium. | CDP connect/command/transient errors. |
| External Sber cookies API | `ExternalSberCookiesClient`, `BuyerService._resolve_session_auth` | Машинная загрузка cookies через `GET` по полному URL из `SBER_COOKIES_API_URL` при `SBER_AUTH_SOURCE=external_cookies_api` и отсутствии inline auth. | Timeout, HTTP/network error, invalid JSON, invalid/empty cookies payload; все переходят в `auth_external_*` reason-code и guest-flow. |
| Postgres | `PostgresSessionRepository` | Persistent state. | Недоступная БД, pool/init/migration errors. |
| Callback receiver | `CallbackClient` | Доставка событий в `middle`. | timeout, non-2xx, network error -> `CallbackDeliveryError`. |
| Node.js + TSX | `SberIdScriptRunner`, `PurchaseScriptRunner` | Запуск TypeScript Playwright scripts. | Нет Node/TSX, timeout, process failed, invalid JSON. |
| noVNC | оператор через браузер | Handoff-наблюдение и ручные шаги. | Sidecar не поднялся, port недоступен. |

## Каталог ошибок и failure-сигналов

### HTTP-уровень `buyer`

- `409 /v1/tasks`: активная сессия уже занимает runtime slot.
- `404 /v1/sessions/{id}` и `404 /v1/replies`: сессия не найдена.
- `409 /v1/replies`: сессия не ждет ответ, `reply_id` неверный, runner потерян после рестарта или reply отсутствует.
- `422`: Pydantic validation.
- `500`: не перехваченные ошибки repository/runtime.

### Callback delivery

- `CallbackDeliveryError`: после исчерпания retries событие помечается failed, сессия переводится в `failed`.
- Stream events доставляются best-effort и не валят покупку.

### CDP и браузер

- `CDP_CONNECT_ERROR`: endpoint недоступен.
- `CDP_COMMAND_TIMEOUT`: Playwright timeout.
- `CDP_COMMAND_ERROR`: ошибка команды.
- `CDP_TRANSIENT_ERROR`: закрыт page/context/browser или разрушен execution context.
- `BuyerService` распознает transient markers и ретраит шаг в recovery window.

### Codex step

- `no_api_key_or_oauth`: нет auth для Codex.
- `binary_missing`: `CODEX_BIN` отсутствует.
- `timeout`: `codex exec` превысил timeout.
- `process_failed`: non-zero exit.
- `parse_output_failed`: output не прочитан/не JSON/не соответствует модели.
- `invalid_status`: статус не входит в `needs_user_input|completed|failed`.

### Auth scripts

- `auth_external_unavailable`: внешний cookies API не настроен, недоступен, вернул HTTP/network error или невалидный JSON.
- `auth_external_timeout`: внешний cookies API не ответил в пределах timeout/retry budget.
- `auth_external_invalid_payload`: внешний cookies API вернул JSON без валидного массива cookies или с невалидной cookie shape.
- `auth_external_empty_payload`: внешний cookies API вернул пустой массив cookies.
- `auth_external_loaded`: внешний cookies API успешно загружен и преобразован в `storageState`.
- `auth_failed_payload`: битый или невалидный `storageState`.
- `auth_failed_redirect_loop`: цикл на SberId.
- `auth_failed_invalid_session`: нет скрипта/runtime/CDP/result или сессия не подтверждена.
- `auth_refresh_requested`: auth-скрипт запросил fallback; новый auth-пакет через reply не запрашивается.
- `auth_ok`: auth context подготовлен.

### Purchase scripts

- `purchase_script_*`: быстрый путь не смог уверенно дойти до SberPay.
- Эти ошибки не должны сами завершать сессию; `BuyerService` переходит в generic flow.

### Knowledge analyzer

- `skipped/no_api_key_or_oauth`
- `failed/codex_binary_missing`
- `failed/timeout`
- `failed/codex_failed`
- `failed/parse_failed`
- path-safety `ValueError`

Ошибки analyzer не меняют итог сессии и не отправляют внешний callback.

## Тестовая карта

| Путь | Что покрывает |
| --- | --- |
| `buyer/tests/test_persistent_state.py` | Store/repository lifecycle, restore, reply validation, redaction persistent state, stale runtime sessions. |
| `buyer/tests/test_url_policy.py` | URL policy для `start_url`/`callback_url`, включая запрет query/fragment в callback URL и trusted allowlist. |
| `buyer/tests/test_auth_reply_removal.py` | MON-29 regression: inline invalid auth уходит в guest без `ask_user`, auth-script refresh/failure уходит в heuristic без запроса auth-пакета, parser reply-auth удален. |
| `buyer/tests/test_external_auth.py` | MON-30 regression: external cookies payload validation, httpx `MockTransport`, timeout mapping, source priority inline over external и guest fallback с `auth_external_*` reason-code. |
| `buyer/tests/test_auth_secret_retention.py` | Runtime-only SberId auth payload, persistence redaction legacy auth-like replies, временный storageState-файл, auth runner stale output/unique output path/non-zero diagnostics behavior. |
| `buyer/tests/test_script_runtime.py` | Чтение script output с fallback на stdout; purchase runner stale output, уникальный output path и non-zero diagnostics behavior. |
| `buyer/tests/test_knowledge_analyzer.py` | Sanitization, safe paths, trace refs, analysis payload/output. |
| `buyer/tests/test_cdp_recovery.py` | CDP recovery markers, retries, transient behavior и payment-boundary regression для Litres/unsupported domains. |
| `buyer/tests/test_observability_and_cdp_tool.py` | Trace/browser action metrics, CDP tool output limits, observability и нейтральный выбор CDP-страницы. |
| `eval_service/tests/test_callbacks.py` | Eval callback receiver, operator reply flow, malformed terminal callbacks, manifest redaction на диске. |
| `eval_service/tests/test_run_store.py` | File manifest lifecycle, callback event redaction/deduplication, atomic summary writes. |
| `eval_service/tests/test_api.py` | Eval API, run detail/dashboard/judge payloads и outward sanitization. |
| `eval_service/tests/test_orchestrator.py` | Eval run orchestration, payment_ready grace, waiting/reply resume progression. |
| `micro-ui/tests/test_store_stream.py` | CallbackStore, дедупликация, SSE queue behavior. |
| `micro-ui/tests/test_design_handoff.py` | Session summary для `ask_user`/waiting progression в `CallbackStore`. |
| `micro-ui/tests/test_eval_shell_static.py` | Поведенчески значимый proxy timeout для долгого создания eval run. |

Рекомендованный точечный запуск Python-тестов описан в `AGENTS.md`.

## Как поддерживать карту

При изменении кода проверьте:

- изменились ли endpoints, Pydantic-модели, callback events или JSON-схемы;
- добавлены ли новые env variables, Docker services, ports или mounts;
- изменились ли states, статусы, reason codes или exceptions;
- появились ли новые storage tables, artifacts, trace files или правила redaction;
- изменились ли CLI-аргументы `cdp_tool.py` или TypeScript-скриптов;
- нужно ли синхронизировать `docs/openapi.yaml`, `docs/callbacks.openapi.yaml`, `README.md`, `docs/buyer.md`, `docs/architecture-decisions.md` или roadmap.

Если изменение затрагивает только тесты, карту нужно обновлять, когда меняется тестовая граница или появляется новый тестовый слой, полезный для навигации по репозиторию.
