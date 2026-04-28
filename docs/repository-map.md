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
8. При успехе `buyer` отправляет `payment_ready` с `order_id`, затем `scenario_finished`.
9. После финального callback асинхронно запускается post-session анализ знаний и сохраняет внутренние draft-артефакты.

## Корень репозитория

| Путь | Ответственность | Входы | Выходы и эффекты | Ошибки и риски |
| --- | --- | --- | --- | --- |
| `README.md` | Быстрый обзор MVP, запуск compose, основные ограничения. | Нет runtime-входов. | Документирует команды, endpoints, trace-файлы и ограничения. | Может устареть при изменении API, env или compose. |
| `AGENTS.md` | Правила работы агентов в репозитории. | Изменения процессов и договоренностей. | Локальные инструкции для Codex и журнал изменений. | Любое изменение требует записи в журнале. |
| `docker-compose.yml` | Локальный стек `postgres` + `browser` + `buyer` + `micro-ui`. | `.env`, bind mounts `CODEX_AUTH_JSON_PATH`, `USER_BUYER_INFO_PATH`. | Порты: `5432`, `6901`, `9223`, `8000`, `8080`; volume `buyer-postgres-data`. | Неверные env/mounts ломают авторизацию Codex или профиль пользователя; недоступный `browser` блокирует агентный шаг. |
| `pytest.ini` | Общая настройка pytest. | Запуск pytest из корня. | Добавляет `pythonpath = .`. | Не нужен отдельный `PYTHONPATH=.`. |
| `LICENSE` | Лицензия проекта. | Нет. | Правовой артефакт. | Не влияет на runtime. |

## Документация и контракты

| Путь | Ответственность | Что синхронизировать |
| --- | --- | --- |
| `docs/openapi.yaml` | Канонический HTTP API `buyer`. | Endpoints, модели request/response, HTTP-коды. |
| `docs/callbacks.openapi.yaml` | Канонический callback envelope и payload-ы событий. | Новые события, поля payload, idempotency semantics. |
| `docs/buyer.md` | Согласованная v1-спецификация домена `buyer`. | Граница SberPay, lifecycle, auth, handoff, knowledge-analysis. |
| `docs/architecture-decisions.md` | Decision log обязательных архитектурных решений. | Сначала обновлять его при новых требованиях, затем остальные документы. |
| `docs/buyer-roadmap.md` | Приоритизированный roadmap и ссылки на Linear. | При изменении roadmap синхронизировать Linear issue. |
| `docs/superpowers/*` | Спецификации и планы, подготовленные агентными workflow. | Исторический контекст планов; не является runtime-контрактом. |
| `docs/repository-map.md` | Эта карта репозитория. | Любые изменения кода, контрактов, ошибок, структуры или runtime-зависимостей. |

## `buyer`: HTTP API и сервисная сборка

### `buyer/app/main.py`

FastAPI-точка входа `buyer`.

| Endpoint | Вход | Выход | Ошибки |
| --- | --- | --- | --- |
| `GET /healthz` | Нет. | `{"status": "ok"}`. | Не оборачивает внутренние зависимости. |
| `POST /v1/tasks` | `TaskCreateRequest`: `task`, `start_url`, optional `callback_url`, `metadata`, `auth`. | `201 TaskCreateResponse`: `session_id`, `status`, `novnc_url`. | `409` при `SessionConflictError`; `422` от Pydantic при невалидном payload. |
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
- `BuyerService`.

Lifecycle:

- `startup`: `store.initialize()`, для Postgres создает pool и миграции.
- `shutdown`: отменяет post-session analysis, закрывает HTTP client callbacks и repository.

### `buyer/app/models.py`

Pydantic-контракты API и внутренних результатов.

Ключевые модели:

- `SessionStatus`: `created`, `running`, `waiting_user`, `completed`, `failed`.
- `TaskAuthPayload`: `provider`, `storageState`/`storage_state`.
- `TaskCreateRequest`: задача, стартовый URL, callback, metadata, auth.
- `SessionReplyRequest`: ответ пользователя на конкретный `reply_id`.
- `EventEnvelope`: callback envelope.
- `PaymentEvidence`: сейчас только `source="litres_payecom_iframe"` и `url`.
- `AgentOutput`: структурированный ответ `codex exec`: `status`, `message`, `order_id`, `payment_evidence`, `profile_updates`, `artifacts`.

Ошибки валидации генерирует Pydantic и FastAPI возвращает их как `422`.

### `buyer/app/settings.py`

Читает `.env` и environment variables через `pydantic-settings`.

Группы настроек:

- callbacks: `MIDDLE_CALLBACK_URL`, retries, timeout, backoff;
- browser/CDP: `BROWSER_CDP_ENDPOINT`, `CDP_RECOVERY_*`;
- Codex: `CODEX_BIN`, `CODEX_MODEL`, sandbox, reasoning, web search, timeout;
- trace и user profile: `BUYER_TRACE_DIR`, `BUYER_USER_INFO_PATH`;
- SberId и scripts-first: allowlist, timeouts, scripts dir;
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

Важное ограничение: storageState/auth-пакет живет в `_runtime_auth`; persistent backend не должен восстанавливать cookies/localStorage после рестарта.

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
- `_iter_artifact_paths` не сохраняет path к `storageState`/cookies/localStorage.

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
3. Запускает `_run_sberid_auth_flow`.
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

Доменные проверки:

- Для Litres `completed` принимается только с `order_id` и `payment_evidence` из `https://payecom.ru/pay_ru?...orderId=...`.
- СБП/SBP/FPS не считается SberPay.

Основные ошибки и реакция:

- `CallbackDeliveryError`: сессия переводится в `failed`, в store пишется fallback `scenario_finished`.
- `SessionNotFoundError`, `SessionConflictError`, `ReplyValidationError`: runner тихо завершает текущую задачу, потому что состояние уже недоступно/невалидно.
- Любое другое исключение: `_handle_failed(..., "Непредвиденная ошибка: ...")`; если callback тоже падает, пишется fallback event и status `failed`.
- Ошибка quick purchase script не валит сценарий: добавляется `[PURCHASE_SCRIPT_FALLBACK]`, затем generic flow.

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
7. Параллельно стримит stdout/stderr и новые browser action records.
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
- trace/output files в `BUYER_TRACE_DIR/<session_id>/`.

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

Ошибки runner возвращает как failed-result, а `BuyerService` делает generic fallback.

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
- `ensure_page()` выбирает существующую не пустую страницу с приоритетом для `litres.ru`.
- read-команды ретраятся при transient context errors.
- `text` и `html` ограничивают stdout по умолчанию.

Основные ошибки:

- `CDP_CONFIG_ERROR`: отрицательное recovery window или interval <= 0.
- `CDP_CONNECT_ERROR`: не удалось подключиться к CDP в пределах recovery window.
- `CDP_COMMAND_TIMEOUT`: Playwright timeout.
- `CDP_COMMAND_ERROR`: ошибка команды/селектора/страницы.
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
| `POST /api/tasks` | `TaskCreateRequest`. | Ответ `buyer /v1/tasks`. | HTTP status от buyer пробрасывается; любые другие ошибки -> `502`. |
| `POST /api/reply` | `ReplySubmitRequest`. | `{forwarded: true, buyer_response}`. | HTTP status от buyer пробрасывается; любые другие ошибки -> `502`. |

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
- `ask_question` ищется по payload key `question`, но текущий `buyer` отправляет `message`, поэтому UI summary может не показать текст вопроса через `ask_question`, хотя `last_message` будет заполнен.

### `micro-ui/app/models.py` и `settings.py`

Pydantic-модели callback, task proxy, reply proxy и session summary. `BUYER_BASE_URL` по умолчанию `http://buyer:8000`.

### Frontend assets

- `micro-ui/app/templates/index.html`: HTML shell.
- `micro-ui/app/static/app.js`: запуск задач, отправка replies, SSE stream, UI state.
- `micro-ui/app/static/app.css`: стили панели.

При изменении callback payload или session summary нужно синхронизировать Python store, JS и OpenAPI callback contract.

## `browser`

### `browser/Dockerfile`

Собирает sidecar на Python slim bookworm с Chromium, Xvfb, x11vnc, fluxbox, noVNC, socat, websockify.

Выходные порты:

- `6901`: noVNC websockify.
- `9223`: CDP proxy через socat.

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
| `buyer/docker/entrypoint.sh` | Подготовка OAuth auth.json и запуск uvicorn. | Копирует mounted `/run/codex/host-auth` в `/root/.codex/auth.json`, валидирует непустой файл. |
| `micro-ui/Dockerfile` | Python 3.12 image для `micro-ui`. | Ставит deps и запускает uvicorn на `8080`. |
| `browser/Dockerfile` | Browser sidecar image. | См. раздел `browser`. |

## Данные, состояние и артефакты

| Данные | Где живут | Что содержит | Что нельзя сохранять |
| --- | --- | --- | --- |
| Runtime auth payload | Память `SessionStore._runtime_auth` | `TaskAuthPayload` с `storageState`. | Нельзя переносить в Postgres. |
| Persistent sessions | Postgres `buyer_sessions` | Статус, task, URL, callback, metadata. | Cookies/localStorage/tokens. |
| Events | Postgres `buyer_events`, память `micro-ui` | Callback envelope и sanitized payload. | Raw stdout/stderr/auth secrets в persistent metadata. |
| Agent memory | Postgres `buyer_agent_memory` | Последние сообщения для prompt context. | Нужно следить за утечками чувствительных данных при новых источниках. |
| Replies | Postgres `buyer_replies` | Pending/answered/consumed ответы пользователя. | Не предназначено для auth-payload reuse. |
| Trace artifacts | `BUYER_TRACE_DIR` | prompts, browser actions JSONL, step trace JSON, script traces, knowledge analysis. | Knowledge analysis дополнительно редактирует auth/payment/order secrets. |
| User profile | `BUYER_USER_INFO_PATH` | Долговременные пользовательские факты. | Auth, cookies, storageState, платежные данные, одноразовые детали заказа. |

## Внешние зависимости

| Зависимость | Кто использует | Назначение | Типовые отказы |
| --- | --- | --- | --- |
| OpenAI/Codex auth | `AgentRunner`, `PostSessionKnowledgeAnalyzer` | Запуск `codex exec`. | Нет `OPENAI_API_KEY` и `/root/.codex/auth.json`; 401; 429. |
| Codex CLI | `AgentRunner`, analyzer | Structured agent step и post-session analysis. | `CODEX_BIN` не найден; timeout; non-zero return. |
| browser-sidecar CDP | `cdp_tool.py`, script runners | Управление Chromium. | CDP connect/command/transient errors. |
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

- `auth_failed_payload`: битый или невалидный `storageState`.
- `auth_failed_redirect_loop`: цикл на SberId.
- `auth_failed_invalid_session`: нет скрипта/runtime/CDP/result или сессия не подтверждена.
- `auth_refresh_requested`: нужен новый auth-пакет или fallback.
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
| `buyer/tests/test_script_runtime.py` | Чтение script output с fallback на stdout. |
| `buyer/tests/test_knowledge_analyzer.py` | Sanitization, safe paths, trace refs, analysis payload/output. |
| `buyer/tests/test_cdp_recovery.py` | CDP recovery markers, retries, transient behavior. |
| `buyer/tests/test_observability_and_cdp_tool.py` | Trace/browser action metrics, CDP tool output limits и observability. |
| `micro-ui/tests/test_store_stream.py` | CallbackStore, дедупликация, SSE queue behavior. |
| `micro-ui/tests/test_design_handoff.py` | UI/handoff design expectations. |

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
