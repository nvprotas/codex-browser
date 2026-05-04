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
- `scripts/evolve_buyer_loop.py`: standalone research-loop CLI для MVP-A self-evolving `buyer`; запускает baseline eval через `eval_service`, создает candidate branch/worktree, применяет placeholder или external patch, готовит candidate runtime через внешний hook, запускает candidate eval и пишет delta report без auto-promotion.

Основной runtime-flow:

1. `openclaw` или `micro-ui` вызывает `POST /v1/tasks` у `buyer`.
2. `buyer` создает `SessionState`, переводит ее в `running` и запускает фоновую задачу `_run_session`.
3. `_run_session` отправляет `session_started`, где `message` включает краткое описание задачи, добавляет память агента и подготавливает SberId auth-контекст.
4. В app-wired runtime нет настроенного automatic purchase-script пути: `main.py` не передает purchase runner/allowlist, поэтому после auth-подготовки `buyer` запускает generic цикл `AgentRunner.run_step()` через `codex exec`.
5. Агент управляет browser-sidecar через `buyer/tools/cdp_tool.py`.
6. `buyer` отправляет callback-события в `middle`/`micro-ui`.
7. При `needs_user_input` сессия переходит в `waiting_user`; ответ приходит через `POST /v1/replies`.
8. При `completed` generic-agent `buyer` вызывает verifier. Только `accepted` приводит к `payment_ready` с `order_id` и verifier-approved `order_id_host`, затем `scenario_finished.status=completed`.
9. `rejected` завершает сценарий как `failed`; `unverified` отправляет `payment_unverified`, затем `scenario_finished.status=unverified` и не считается payment success.
10. После финального callback для `completed`/`failed` асинхронно запускается post-session анализ знаний и сохраняет внутренние draft-артефакты; для `unverified` completed-анализ не запускается.

## Корень репозитория

| Путь | Ответственность | Входы | Выходы и эффекты | Ошибки и риски |
| --- | --- | --- | --- | --- |
| `README.md` | Быстрый обзор MVP, запуск compose, основные ограничения. | Нет runtime-входов. | Документирует команды, endpoints, trace-файлы, external cookies env и ограничения. | Может устареть при изменении API, env или compose. |
| `AGENTS.md` | Правила работы агентов в репозитории. | Изменения процессов и договоренностей. | Локальные инструкции для Codex и журнал изменений. | Любое изменение требует записи в журнале. |
| `docker-compose.yml` | Локальный стек `postgres` + `browser` + `buyer` + `micro-ui` + `eval_service`. | `.env`, env `EVAL_CALLBACK_SECRET`, `TRUSTED_CALLBACK_URLS`, `SBER_AUTH_SOURCE`, `SBER_COOKIES_API_URL`, `SBER_COOKIES_API_TIMEOUT_SEC`, `SBER_COOKIES_API_RETRIES`, bind mounts `CODEX_AUTH_JSON_PATH`, `USER_BUYER_INFO_PATH`, `EVAL_AUTH_PROFILES_HOST_DIR`. | Host-порты только на loopback: `127.0.0.1:5432`, `127.0.0.1:6901`, `127.0.0.1:8000`, `127.0.0.1:8080`, `127.0.0.1:8090`; CDP `9223` доступен только внутри docker-сети как `http://browser:9223`; volume `buyer-postgres-data`; `CODEX_AUTH_JSON_PATH` монтируется в `buyer` и `eval_service` как `/run/codex/host-auth`; `buyer` может получать SberId cookies из внешнего сервиса при `SBER_AUTH_SOURCE=external_cookies_api`; eval auth-профили читаются из host-директории и монтируются в `/run/eval/auth-profiles`. | Неверные env/mounts ломают авторизацию Codex, профиль пользователя, external cookies source или eval callbacks; отсутствующая host-директория или файл `<auth_profile>.json` приводит eval-case к `skipped_auth_missing`; недоступный `browser` блокирует агентный шаг; удаленный доступ к loopback-портам требует VPN/SSH tunnel/authenticated reverse proxy. |
| `docker-compose.openclaw.yml` | Standalone compose для развертывания рядом с `openclaw`: только `postgres`, `browser`, `buyer`, без `eval_service` и временного `micro-ui`. | `.env`, обязательные `MIDDLE_CALLBACK_URL` и `SBER_COOKIES_API_URL`, bind mounts `CODEX_AUTH_JSON_PATH`, `USER_BUYER_INFO_PATH`. | `buyer` публикуется на `${BUYER_BIND_ADDR:-127.0.0.1}:${BUYER_PORT:-8000}`, noVNC на `${NOVNC_BIND_ADDR:-127.0.0.1}:${NOVNC_PORT:-6901}`, Postgres на `${POSTGRES_BIND_ADDR:-127.0.0.1}:${POSTGRES_PORT:-5432}`; callbacks отправляются во внешний `middle`; SberId cookies берутся из external cookies API по умолчанию; `buyer` получает `host.docker.internal` для доступа к сервисам host-машины. | Неверный `MIDDLE_CALLBACK_URL` ломает доставку событий в middle; неверный `SBER_COOKIES_API_URL` переводит auth в guest-flow; открытые bind addr требуют доверенного периметра. |
| `pytest.ini` | Общая настройка pytest. | Запуск pytest из корня. | Добавляет `pythonpath = .`. | Не нужен отдельный `PYTHONPATH=.`. |
| `LICENSE` | Лицензия проекта. | Нет. | Правовой артефакт. | Не влияет на runtime. |
| `skills/openclaw-buyer/SKILL.md` | Скилл для агента `openclaw`: как формировать задачу для `buyer` и технически читать статус сессии. | HTTP API `buyer`, роли `openclaw`/`middle`/`buyer`. | Процедура запуска задач из `openclaw` без знаний про auth/callbacks; task-шаблон с целью, критериями, ограничениями и платежной границей; правила read-only проверки статуса. | Может устареть при изменении API или роли `middle`. |
| `extensions/openclaw-buyer/` | Минимальная metadata/runtime-обвязка OpenClaw plugin для skill-only extension. | `openclaw.plugin.json`, `package.json`, `index.js`. | Дает OpenClaw plugin discovery распознать `openclaw-buyer` и загрузить skill-директорию `skills`. | Без package entrypoint или `configSchema` OpenClaw считает config entry stale или помечает plugin ошибочным. |
| `scripts/install-openclaw-buyer-skill.sh` | Копирует repo-local skill `skills/openclaw-buyer` и plugin metadata в extension-директорию `openclaw`. | Аргумент `<openclaw-buyer-extension-dir>` или `OPENCLAW_BUYER_EXTENSION_DIR`; по умолчанию `~/.openclaw/extensions/openclaw-buyer`; исходные `skills/openclaw-buyer` и `extensions/openclaw-buyer`. | Создает/обновляет `<target>/package.json`, `<target>/openclaw.plugin.json`, `<target>/index.js`, `<target>/skills/openclaw-buyer/SKILL.md` и `<target>/agents/openai.yaml`. | Не удаляет устаревшие файлы в целевой директории; неверный target может установить extension не туда. |
| `scripts/evolve_buyer_loop.py` | Standalone Python stdlib CLI для MVP-A evolve-loop `buyer`. | CLI commands `doctor`, `run`, `continue`, `compare`; `--repo`, `--reports-dir`, `--case-id`, baseline/candidate eval URLs, `--patch-mode placeholder|external-command`, optional `--patch-command`, `--candidate-prepare-command`, `--allowed-path`, explicit `--skip-candidate-eval` для baseline-only smoke. | Читает existing `eval_service` HTTP API; создает candidate branch/worktree `refs/heads/evolve/cand-*`; пишет `.tmp/evolve/**` artifacts, `summary.md`, `latest.json`, `delta_report.json`, redacted `candidate.diff`, `patch-request.json`, `patch-manifest.json`; может сделать один candidate commit. | Недоступный eval endpoint, одинаковые baseline/candidate URLs, mismatch case fingerprints, judge failure, `waiting_user`/handoff, git/patch failure, forbidden unstaged/staged path diff/path traversal, artifact/redaction failure; MVP-A не push-ит ветки и не двигает champion. |

## Документация и контракты

| Путь | Ответственность | Что синхронизировать |
| --- | --- | --- |
| `docs/openapi.yaml` | Канонический HTTP API `buyer`. | Endpoints, модели request/response, HTTP-коды. |
| `docs/callbacks.openapi.yaml` | Канонический callback envelope и payload-ы событий. | Новые события, поля payload, idempotency semantics. |
| `docs/buyer.md` | Согласованная v1-спецификация домена `buyer`. | Граница SberPay, lifecycle, auth, handoff, knowledge-analysis. |
| `docs/architecture-decisions.md` | Decision log обязательных архитектурных решений. | Сначала обновлять его при новых требованиях, затем остальные документы. |
| `docs/buyer-roadmap.md` | Приоритизированный roadmap и ссылки на Linear. | При изменении roadmap синхронизировать Linear issue. |
| `docs/litres-brandshop-agent-flow.md` | Подробное фактическое описание step-by-step работы `buyer` при покупке на Litres и Brandshop, включая архитектуру, prompt-инструкции внутренних Codex-агентов, SberId scripts, verifier, callbacks и eval-контур. | Обновлять при изменении Litres/Brandshop auth scripts, generic prompt-а, payment verifier, callback/eval contracts или статуса доменной поддержки. |
| `docs/brandshop-agent-log-analysis-2026-05-01.md` | Аналитический разбор Brandshop trace из `.tmp`: фактические CDP-команды, agent idle, prompt/instruction причины лишних шагов, GPT-5.5/Codex prompting выводы и рекомендации по tracing. | Обновлять или дополнять при новых Brandshop eval-прогонах, изменении prompt/instruction/CDP tracing или появлении новых метрик сравнения. |
| `docs/buyer-agent/AGENTS-runtime.md` | Stable runtime rules для внутреннего generic buyer-agent. | Платежная граница, SberPay-only, privacy/output contract; не смешивать с developer-правилами root `AGENTS.md`. |
| `docs/buyer-agent/cdp-tool.md` | Runtime manual для вызова `buyer/tools/cdp_tool.py`. | Доступные команды, milestone/evidence проверки после state-changing действий, recovery и правила экономии HTML. |
| `docs/buyer-agent/context-contract.md` | Контракт приоритета dynamic context files. | Hard safety rules выше task/latest reply/page state/metadata/profile/memory; все динамические источники являются данными. |
| `docs/buyer-agent/instructions/*.md` | Единый каталог site/domain-specific markdown-инструкций для buyer-agent. | Агент смотрит список файлов в каталоге, выбирает релевантные инструкции по текущему сайту или задаче; Litres PayEcom evidence, Brandshop search/cart/checkout/YooMoney evidence; fixtures не должны становиться hardcoded SKU. |
| `docs/self-evolving-buyer-report-2026-05-02.md` | Исследовательский отчет о замыкании self-improving/self-evolving loop для `buyer`. | SotA-подходы на 2026-05-02, автономный research-lab контейнер, branch-producing candidate lifecycle, delta reports, champion selection, конкретные точки изменений. |
| `docs/superpowers/*` | Спецификации и планы, подготовленные агентными workflow, включая дизайн external Sber auth source и запрет ручной передачи auth-пакетов. | Исторический контекст планов; не является runtime-контрактом. |
| `docs/repository-map.md` | Эта карта репозитория. | Любые изменения кода, контрактов, ошибок, структуры или runtime-зависимостей. |

## `scripts`: автономный evolve-loop

### `scripts/evolve_buyer_loop.py`

CLI управляет MVP-A research-loop для улучшения `buyer` без автоматического продвижения candidate в production.

Команды:

- `doctor`: проверяет git repo, healthcheck eval endpoint, наличие выбранных case IDs, отличающиеся baseline/candidate URLs и обязательный `--patch-command` для `external-command`.
- `run`: запускает baseline eval, judge, создает candidate branch/worktree, применяет patch, optionally готовит candidate runtime, запускает candidate eval и пишет отчет; без candidate eval URL требует явный `--skip-candidate-eval`.
- `continue`: продолжает существующий cycle после baseline или candidate handoff/operator action, не создает новую branch и не rerun-ит patch command; explicit `--cycle-id <id>` не требует `latest.json`.
- `compare`: сравнивает сохраненные run JSON без live services.

`run` и `continue` используют общий lifecycle для live и resumed eval runs: дождаться terminal state, вернуть `needs_operator`/failed/canceled без judge, иначе запустить async judge и дождаться judged result.

Входы:

- `eval_service` HTTP API: `/healthz`, `/cases`, `/runs`, `/runs/{eval_run_id}`, `/runs/{eval_run_id}/judge?async=1`;
- git repo и `--base-ref`;
- optional external patch command через `EVOLVE_*` env и `patch-request.json`;
- optional candidate prepare command через `EVOLVE_*` env;
- выбранные eval case IDs и allowed path globs.

Выходы:

- cycle artifacts в `.tmp/evolve/**`;
- candidate branch `refs/heads/evolve/cand-YYYYMMDDHHMMSS-NNN-<patch_slug>`;
- один candidate commit с сообщением `evolve buyer: <patch_slug>`;
- JSON stdout при `--json`, progress в stderr;
- `summary.md` для human review и next commands; summary включает verdict, candidate ref/SHA, delta report path и judge recommendations.

Ограничения:

- auto-promotion отключен;
- `git push`, удаление веток, `git reset --hard`, `git clean -fdx`, `git add .` не используются;
- `.env`, nested `.env.*`, `.git/**`, `.tmp/**`, `eval/runs/**`, `.auth/**`, nested auth/profile/browser profile paths и storageState paths всегда forbidden даже при широком `--allowed-path`;
- path traversal через `..` и absolute paths отклоняются перед `git add`;
- staged files from external patch commands валидируются вместе с unstaged/untracked/deleted files и `patch-manifest.touched_paths` перед commit;
- raw headers/body/tokens/order/payment URLs редактируются в JSON, summary, logs и `candidate.diff`;
- `--repeats-per-case != 1` и `--no-keep-worktree` в MVP-A отклоняются как не реализованные.

Основные команды:

```bash
uv run python scripts/evolve_buyer_loop.py run --repo . --eval-base-url http://127.0.0.1:8091 --candidate-eval-base-url http://127.0.0.1:8092 --case-id litres_purchase_book_001 --patch-mode placeholder --reports-dir .tmp/evolve --json
```

Baseline-only smoke без candidate eval требует явного флага:

```bash
uv run python scripts/evolve_buyer_loop.py run --repo . --eval-base-url http://127.0.0.1:8090 --case-id litres_purchase_book_001 --patch-mode placeholder --skip-candidate-eval --reports-dir .tmp/evolve --json
```

```bash
uv run python scripts/evolve_buyer_loop.py run --repo . --eval-base-url http://127.0.0.1:8091 --candidate-eval-base-url http://127.0.0.1:8092 --case-id litres_purchase_book_001 --patch-mode external-command --patch-command "uv run python tools/propose_buyer_patch.py" --candidate-prepare-command "uv run python tools/restart_candidate_buyer.py" --reports-dir .tmp/evolve --json
```

```bash
uv run python scripts/evolve_buyer_loop.py continue --repo . --reports-dir .tmp/evolve --cycle-id latest --json
```

```bash
uv run python scripts/evolve_buyer_loop.py compare --baseline-run-json .tmp/evolve/baseline-run.json --candidate-run-json .tmp/evolve/candidate-run.json --cases-json .tmp/evolve/cases.json --json
```

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
- `ExternalSberCookiesClient`, только если `SBER_AUTH_SOURCE=external_cookies_api`.
- `BuyerService`.

Lifecycle:

- `startup`: `store.initialize()`, для Postgres создает pool и миграции.
- `shutdown`: отменяет post-session analysis, закрывает external auth client при наличии, HTTP client callbacks и repository.

### `buyer/app/logging_config.py`

Настраивает формат логов контейнера `buyer`.

Поведение:

- `main.py` вызывает `configure_component_logging()` при импорте приложения;
- формат строк для uvicorn/app handlers: `[%(name)s] %(levelname)s: %(message)s`;
- application loggers `app.*` в контейнере и `buyer.*` в локальных тестах используют те же handlers и не дублируют записи через propagation;
- модули `service`, `runner`, `auth_scripts`, `knowledge_analyzer`, `user_profile` логируют через `logging.getLogger(__name__)`, поэтому в начале строки виден компонент, например `[app.runner]` или `[app.service]`.

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

- `SessionStatus`: `created`, `running`, `waiting_user`, `completed`, `failed`, `unverified`.
- `TaskAuthPayload`: `provider`, `storageState`/`storage_state`; пользовательские replies не должны переносить auth-пакеты.
- `TaskCreateRequest`: задача, стартовый URL, callback URL, ephemeral callback token, metadata, auth.
- `SessionReplyRequest`: ответ пользователя на конкретный `reply_id`.
- `EventEnvelope`: callback envelope.
- `PaymentEvidence`: только `source` и `url`; поддержанные source сейчас merchant-specific `litres_payecom_iframe`, `brandshop_yoomoney_sberpay_redirect` и provider-generic `payecom_payment_url`, `yoomoney_payment_url` для unverified unknown merchant evidence.
- `AgentOutput`: структурированный ответ `codex exec`: `status`, `message`, top-level `order_id`, `payment_evidence`, `profile_updates`, `artifacts`. `order_id_host` не входит в evidence/output, его вычисляет verifier.

Ошибки валидации генерирует Pydantic и FastAPI возвращает их как `422`.

### `buyer/app/agent_instruction_manifest.py`

Формирует статический manifest instruction files для generic buyer-agent без runtime-входов.

Выход:

- `root`: `/workspace/docs/buyer-agent/AGENTS-runtime.md`;
- `always_read`: `/workspace/docs/buyer-agent/cdp-tool.md`, `/workspace/docs/buyer-agent/context-contract.md`;
- `instructions_dir`: каталог `/workspace/docs/buyer-agent/instructions`, где runtime-agent сам смотрит список файлов и выбирает релевантные инструкции.

Основные риски: runtime path должен совпадать с docker mount репозитория в `/workspace`; новые merchant instructions требуют понятного имени файла и содержания, чтобы agent выбрал их по host/task.

### `buyer/app/agent_context_files.py`

Пишет dynamic context files в директорию текущего trace step и возвращает manifest путей для bootstrap prompt.

Входы:

- `step_dir`;
- task/start URL;
- metadata;
- нормализованная agent memory;
- latest user reply;
- user profile text;
- sanitized auth state.

Выходные файлы:

- `task.json`;
- `metadata.json`;
- `memory.json`;
- `latest-user-reply.md` (может быть пустым);
- `user-profile.md` (может быть пустым);
- `auth-state.json`.

Ограничения: файлы не должны содержать raw cookies, `storageState`, localStorage, auth tokens, payment secrets или password-like значения; task/start URL и scalar-строки также проходят redaction. Auth context передается только как allowlisted summary (`source`, `mode`, `path`, `reason_code`, attempts/status/domain flags), без artifacts/stdout/stderr/external payload.

### `buyer/app/payment_verifier.py`

Доменные проверки платежной границы перед `payment_ready` и `completed`.

Входы:

- `start_url` сессии;
- `AgentOutput` generic runner.

Выходы:

- `ProviderPaymentEvidence(provider, host, order_id, url)` из provider URL parsers;
- `PaymentVerificationResult(status, failure_reason, order_id_host, provider, evidence_url)`, где `status` равен `accepted`, `rejected` или `unverified`.

Правила:

- Provider parser `parse_payecom_payment_url()` принимает только точный HTTPS URL `https://payecom.ru/pay_ru?orderId=<order_id>` без port/path params и с ровно одним непустым `orderId`.
- Provider parser `parse_yoomoney_payment_url()` принимает только точный HTTPS URL `https://yoomoney.ru/checkout/payments/v2/contract?orderId=<order_id>` без port/path params и с ровно одним непустым `orderId`.
- Для Litres merchant policy принимает только `order_id`, подтвержденный `payment_evidence`/`payment_frame_src` с PayEcom provider evidence и совпадающим `orderId`; accepted result возвращает `order_id_host="payecom.ru"`.
- `http://payecom.ru`, subdomain вроде `evil.payecom.ru`, port/path params, path prefix вроде `/pay_ru_malicious`, несколько `orderId` или mismatch `orderId` отклоняются.
- Для Brandshop merchant policy принимает только `order_id`, подтвержденный `payment_evidence.source="brandshop_yoomoney_sberpay_redirect"` и YooMoney provider evidence с совпадающим `orderId`; accepted result возвращает `order_id_host="yoomoney.ru"`.
- Для доменов без merchant policy валидный provider evidence PayEcom/YooMoney со совпадающим top-level `order_id` возвращает `unverified`, а не success; битый/mismatch evidence возвращает `rejected`.

### `buyer/app/settings.py`

Читает `.env` и environment variables через `pydantic-settings`.

Группы настроек:

- callbacks: `MIDDLE_CALLBACK_URL`, `TRUSTED_CALLBACK_URLS`, retries, timeout, backoff;
- browser/CDP: `BROWSER_CDP_ENDPOINT`, `CDP_RECOVERY_*`;
- Codex: `CODEX_BIN`, единственная модель `CODEX_MODEL` (default `gpt-5.5`), sandbox, reasoning (default `low`), web search, timeout;
- trace и user profile: `BUYER_TRACE_DIR`, `BUYER_USER_INFO_PATH`;
- SberId scripts инфраструктура: allowlist с default `litres.ru,brandshop.ru`, timeouts, scripts dir;
- automatic purchase-script allowlist не входит в runtime settings или compose; app-wired покупка после SberId auth идет через generic-agent;
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

1. Отправляет `session_started` с `message`, `start_url` и `novnc_url`; `message` включает краткое описание задачи.
2. Добавляет `Start URL` и задачу в agent memory.
3. Запускает `_run_sberid_auth_flow`, где `_resolve_session_auth` выбирает inline auth или external cookies API.
4. Сразу переходит к generic-agent: скрытого pre-generic purchase-script шага в `BuyerService` больше нет.
5. Циклически вызывает `AgentRunner.run_step`.
6. Обрабатывает статусы `needs_user_input`, `completed`, `failed`.
7. Для `completed` вызывает verifier и различает `accepted`, `rejected`, `unverified`.
8. Для transient CDP-сбоев повторяет шаг в пределах `CDP_RECOVERY_WINDOW_SEC` с системным маркером `[CDP_RECOVERY_RESTART_FROM_START_URL]`.
9. Финализирует через completed/failed/unverified terminal handling.

Callback-события:

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

Trace в `agent_step_finished` и `scenario_finished.payload.artifacts.trace` формируется на service-границе через allowlist `TraceSummary`: `BuyerService` не пробрасывает legacy/full поля trace artifact вроде `prompt_preview`, stdout/stderr tail, browser action tail/raw HTML, prompt/browser-actions paths, command timing arrays и idle-gap diagnostics; nested `codex_attempts` тоже усечены до роли, модели, статуса и failure reason. В terminal callback обычные non-trace artifacts сохраняются, а top-level `trace` заменяется slim-сводкой.

Логи контейнера:

- ключевые этапы сценария пишутся строками `buyer_progress` с `session_id`, `stage`, `step`, `status`, `order_id` и коротким русскоязычным описанием;
- browser-actions пишутся в stdout как компактные live-строки `browser_action` с командой, длительностью, URL без query/fragment, селектором и коротким текстовым превью; источником этих логов не является callback trace summary, где `browser_actions_tail` отсутствует;
- полные JSONL browser-actions и подробные результаты `snapshot`/`html` остаются в trace-артефактах, но не печатаются целиком в логи контейнера.

Доменные проверки:

- Для Litres `completed` принимается только с `order_id` и `payment_evidence` из точного `https://payecom.ru/pay_ru?...orderId=...`; `payment_ready` получает `order_id_host="payecom.ru"`.
- Для Brandshop `completed` принимается только с `order_id` и `payment_evidence.source="brandshop_yoomoney_sberpay_redirect"` из точного `https://yoomoney.ru/checkout/payments/v2/contract?orderId=...`; `payment_ready` получает `order_id_host="yoomoney.ru"`.
- Для неподдерживаемых доменов валидный provider evidence PayEcom/YooMoney со совпадающим `order_id` приводит к `payment_unverified` и `scenario_finished.status="unverified"`, но не к `payment_ready`.
- `payment_unverified.payload` по контракту содержит `order_id`, `order_id_host`, `provider`, `message` и `reason`; runtime также может добавить diagnostic `evidence_url`.
- Битый evidence, mismatch `order_id` или неподдержанный provider отклоняются как `failed`.
- СБП/SBP/FPS не считается SberPay.

Основные ошибки и реакция:

- `CallbackDeliveryError`: сессия переводится в `failed`, в store пишется fallback `scenario_finished`.
- `SessionNotFoundError`, `SessionConflictError`, `ReplyValidationError`: runner тихо завершает текущую задачу, потому что состояние уже недоступно/невалидно.
- Любое другое исключение: `_handle_failed(..., "Непредвиденная ошибка: ...")`; если callback тоже падает, пишется fallback event и status `failed`.
- Ошибки quick purchase script не являются частью app-wired runtime-пути: hidden automatic purchase scripts retired из конфигурации, сценарий идет через generic flow.
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
- задача, `start_url`, metadata, sanitized auth summary;
- agent memory и последний ответ пользователя;
- callback для live stream.

Выход:

- `AgentOutput` со статусом `needs_user_input`, `completed` или `failed`;
- trace artifacts: prompt path, sha256, stdout/stderr tail, command, single-model strategy, browser actions metrics и путь к сохраненному structured output JSON.

Внутренний flow:

1. Подготавливает trace context: `BUYER_TRACE_DIR/YYYY-MM-DD/HH-MM-SS/<session_id>/step-XXX-*`.
2. Делает CDP preflight через `/app/tools/cdp_tool.py url`.
3. Загружает user profile.
4. Пишет dynamic context files в trace step dir через `agent_context_files`.
5. Получает instruction manifest через `agent_instruction_manifest`.
6. Строит bootstrap prompt из hard rules, task, CDP endpoint и manifest-ов файлов; latest reply передается только через context file.
7. Проверяет `OPENAI_API_KEY` или `/root/.codex/auth.json`.
8. Формирует единственную попытку модели `single` через `CODEX_MODEL` или default `gpt-5.5`; fast/strong роли в runtime отсутствуют.
9. Генерирует `attempt_id`, передает его в `BUYER_CODEX_ATTEMPT_ID` для `codex exec` и CDP action log, затем запускает `codex exec --json --output-schema ... -o <tmp> <prompt>`.
10. Параллельно стримит stdout/stderr и новые browser action records; stdout/stderr читаются chunk-based без лимита длины одной строки, а длинные строки внутри stream payload обрезаются перед callback/storage. Browser action metrics связывают start/finish по `command_id`, если он есть, и используют FIFO по имени команды только для legacy-логов.
11. Парсит output JSON в `AgentOutput`.
12. Сохраняет structured output JSON в trace-директории, чтобы путь `codex_output_path` из полного trace оставался читаемым после шага.

Основные ошибки и failure reasons:

- preflight CDP failed: возвращается `AgentOutput(status='failed')` с описанием недоступности browser-sidecar.
- нет Codex auth: `fallback_reason='no_api_key_or_oauth'`.
- `FileNotFoundError` при запуске `CODEX_BIN`: `failure_reason='binary_missing'`.
- timeout: `failure_reason='timeout'`.
- non-zero return code: `failure_reason='process_failed'`, включая 401 и 429 с отдельными сообщениями.
- невалидный JSON/output schema: `failure_reason='parse_output_failed'`.
- неподдерживаемый `status`: `failure_reason='invalid_status'`.
- structured output со статусом `failed`: `failure_reason='agent_reported_failed'`, в summary попытки сохраняется диагностический `failure_message`.
- невозможный внутренний случай без попыток: `RuntimeError('codex step finished without attempts')`.

### `buyer/app/prompt_builder.py`

Строит prompt для `codex exec`.

Входы: задача, start URL, CDP endpoint, `instruction_manifest`, `context_file_manifest`, последний reply.

Выход: короткий bootstrap prompt с hard safety rules, SberPay-only policy, context-injection boundary, путями к static instruction files, путями к dynamic context files, текущей задачей, короткой policy автономности/`needs_user_input` и schema-only ответом. Latest reply, full instructions, metadata JSON, auth payload/context, user profile, memory и verbose CDP preflight не встраиваются в prompt.

Ошибки явно не выбрасывает; риски связаны с устаревшими instruction/instruction files, если меняется доменный контракт.

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

## `buyer`: SberId auth scripts and TypeScript helpers

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

- published: `brandshop.ru`, `litres.ru`.

Входы:

- `session_id`, домен, `start_url`, Playwright `storageState`, номер попытки;
- настройки scripts dir, CDP endpoint, timeout, trace dir.

Выход:

- `AuthScriptResult(status, reason_code, message, artifacts)`;
- trace/output files в `BUYER_TRACE_DIR/YYYY-MM-DD/HH-MM-SS/<session_id>/`: runner переиспользует существующую dated session trace-директорию либо создает ее до generic-agent шага; stale `auth-script-result.json` и `auth-script-result-attempt-XX.json` удаляются в этой session dir, затем скрипту передается уникальный `auth-script-result-attempt-XX-<uuid>.json` для текущей попытки;
- входной Playwright `storageState` передается TypeScript-скрипту через временный файл вне workspace с правами `0600` и удаляется в `finally`, поэтому raw auth state не остается в `BUYER_TRACE_DIR`.
- stderr auth-скрипта логируется runner-ом как `auth_script_stderr ...` в container logs; подробные успешные trace-события остаются только в JSONL-файлах.

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

### Automatic purchase scripts

Скрытый automatic purchase-script путь удален из app wiring и `BuyerService`: `main.py` не создает purchase runner, сервис не принимает allowlist/runner и settings/compose не содержат `PURCHASE_SCRIPT_ALLOWLIST` как runtime contract.

Текущий путь покупки после SberId auth:

1. generic Codex-agent читает runtime instructions/context files;
2. управляет браузером через CDP tool;
3. возвращает structured `AgentOutput`;
4. `payment_verifier.py` решает `accepted`/`rejected`/`unverified`.

`buyer/app/purchase_scripts.py` и regression-test registry удалены. Если в будущем появятся custom scripts, они должны быть явным инструментом или отдельным контрактом, а не скрытым pre-generic shortcut. Любой результат такого инструмента все равно обязан пройти verifier и не может сам отправить `payment_ready`.

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

`buyer/scripts/sberid/auth-trace.ts`:

- общий TypeScript helper для диагностического trace вокруг auth-навигаций и закрытия Playwright pages/contexts/browser;
- пишет события `auth_navigation_started`/`auth_navigation_finished` с `stage`, `from_url`, `to_url`, final URL/host, HTTP status, duration и ошибкой при сбое;
- пишет события `auth_page_close_*`, `auth_context_close_*`, `auth_browser_close_*` с причиной закрытия, stage, page snapshots и результатом;
- при ошибках навигации или закрытия дополнительно пишет компактную JSON-строку в stderr, чтобы runner вывел ее в логи контейнера `buyer`;
- используется publish-скриптами Litres и Brandshop, чтобы отследить auth-навигации и закрытия страниц/контекстов.

`buyer/scripts/sberid/litres.ts`:

- добавляет cookies из `storageState` в существующий browser context;
- перед login-flow проверяет текущую авторизацию по profile/book markers и при успехе возвращает `already_authenticated=true`;
- открывает login/profile pages Litres;
- логирует auth-навигации и cleanup-закрытия через `auth-trace.ts`;
- ищет Sber ID entry, следит за redirect loop на `id.sber.ru`;
- проверяет авторизацию по markers `Мои книги` и `Профиль`;
- возвращает `auth_ok` только при подтвержденной авторизации.

`buyer/scripts/sberid/brandshop.ts`:

- готовит контекст Brandshop;
- перед login-flow проверяет текущую авторизацию по DOM-признаку `.header-authorize__avatar` и account/profile/logout/user markers; если текущая страница пустая или на другом host, проверяет только обычный Brandshop entrypoint `/`; при успехе возвращает `already_authenticated=true`;
- ищет profile/login/Sber ID controls;
- не использует `/account/` как auth probe; отсутствие сильных markers на текущей/entry странице означает, что нужно идти в login-flow;
- логирует auth-навигации и cleanup-закрытия через `auth-trace.ts`;
- валидирует возврат на ожидаемый host и auth markers: простая страница `/account/`, generic `Профиль` и login-индикаторы вроде `Войти` не считаются авторизацией без сильных markers `.header-authorize__avatar`, заказов, logout или данных аккаунта;
- возвращает те же auth reason codes.

Файла `buyer/scripts/purchase/litres.ts` больше нет. Покупка Litres выполняется generic-agent через prompt-инструкции и строгий Litres verifier. Brandshop также не имеет `buyer/scripts/purchase/brandshop.ts`; его путь закреплен как generic instruction.

## `buyer`: browser tooling

### `buyer/tools/cdp_tool.py`

CLI-утилита управления browser-sidecar через Playwright CDP. Это основной инструмент, который получает агент внутри prompt.

Команды:

- mutating: `goto`, `click`, `fill`, `press`, `wait`, `screenshot`, `html --path`;
- read/inspect: `title`, `url`, `text`, `exists`, `attr`, `links`, `snapshot`, `html`;
- wait/guard: `wait-url --contains/--regex`, `wait-selector --selector`, а также post-click guards `click --wait-url-contains/--wait-url-regex/--wait-selector`.

Общие входы:

- `--endpoint`, default из `BROWSER_CDP_ENDPOINT`;
- `--timeout-ms`;
- `--recovery-window-sec`;
- `--recovery-interval-ms`;
- command-specific arguments.
  Для устойчивости к ошибкам агента `--timeout-ms` также принимается после подкоманды у `goto`, `click`, `fill`, `press`, `wait-url`, `wait-selector`, `exists`, `attr`, `links`, `snapshot`, `text`, `html`, `screenshot`; `text --limit` является alias к `--max-chars`; `wait --timeout-ms 2000` совместимо интерпретируется как `wait --seconds 2`.

Выход:

- JSON в stdout;
- exit code `0`, если `ok=true`, иначе `1`;
- action log JSONL в `BUYER_CDP_ACTIONS_LOG_PATH`, если переменная задана; каждая команда получает `command_id`, а при наличии `BUYER_CODEX_ATTEMPT_ID` запись связывается с попыткой агента.

Поведение:

- HTTP endpoint резолвится через `/json/version` в websocket endpoint.
- При недоступном hostname пробуются fallback: `localhost`, `127.0.0.1`, `host.docker.internal`.
- `ensure_page()` выбирает существующую не пустую страницу нейтрально: сначала HTTP(S), затем прочие non-blank, а среди равных кандидатов последнюю по `context_index/page_index`; hardcoded доменного приоритета нет.
- `goto --url` до подключения к Playwright валидирует URL той же public http/https policy, что `start_url`: без userinfo, loopback/private/link-local/metadata hosts, `host.docker.internal` и внутренних suffixes.
- После подключения к странице `cdp_tool` ставит Playwright `context.route("**/*", ...)` guard на время команды: document/navigation requests, включая redirects и iframe-навигации, проходят через ту же URL policy и при нарушении abort-ятся `blockedbyclient`; ненавигационные asset/XHR requests не блокируются этим guard.
- read-команды ретраятся при transient context errors.
- `click` может сразу ждать URL/selector guard после клика, чтобы агент не тратил отдельный inspect-step на очевидный переход.
- `wait-url` и `wait-selector` позволяют ждать milestone напрямую и возвращают финальный URL/count/visibility как evidence.
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
- prompt помечает входной JSON как данные, задает evidence budget для чтения trace/browser-actions и калибрует confidence по силе evidence;
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

`eval_service` запускает eval cases, принимает callbacks от `buyer`, хранит manifests и готовит данные для judge/dashboard. Публичный callback contract принимает `payment_unverified` как terminal review-needed outcome, не равный `payment_ready`/успеху.

Runtime auth-профили берутся из host-директории `EVAL_AUTH_PROFILES_HOST_DIR`, которая bind-mounted в контейнер как `/run/eval/auth-profiles` и внутри сервиса читается через `EVAL_AUTH_PROFILES_DIR`. Для `auth_profile: litres_sberid` ожидается файл `litres_sberid.json`; для `auth_profile: brandshop_sberid` ожидается `brandshop_sberid.json`. Эти файлы являются секретами, не входят в image и не должны храниться в repo.

### `eval_service/app/runtime_helpers.py`

Общие runtime helpers для FastAPI-слоя eval service.

Поведение:

- лениво достает или создает `RunStore` и `BuyerClient` из `request.app.state`;
- содержит общий поиск case в manifest, terminal-state predicate и чтение поля из dict/object response;
- используется `callbacks.py`, `orchestrator.py` и `api.py`, чтобы не держать несколько локальных копий одинаковых helper-функций.

### `eval_service/app/orchestrator.py`

Создает eval run и оркестрирует последовательный запуск case через `buyer`.

Поведение:

- `POST /runs` создает `manifest.json`, переводит run в `running`, планирует выполнение case в фоновой задаче и сразу возвращает текущий manifest; первый case на момент ответа может еще оставаться `pending`;
- execution идет последовательно до `waiting_user` или terminal state; после `payment_ready` выдерживается grace period перед `finished`; `unverified` считается terminal state без payment-ready grace;
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
- активные executable smoke-case зависят от `enabled` в YAML. Litres variants включены; Brandshop `brandshop_purchase_smoke_001` тоже включен и появляется в registry при наличии файла `eval/cases/brandshop_purchase_smoke.yaml`;
- `brandshop_purchase_smoke.yaml` стартует через Brandshop search-параметр `st`, например `https://brandshop.ru/search/?st={{ search_query }}`, а модель/цвет/размер проверяются как constraints.

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
- `payment_ready` требует `payload.order_id`, `payload.order_id_host` и `payload.message`, иначе возвращает `422` без изменения manifest;
- `payment_unverified` требует `payload.order_id`, `payload.order_id_host`, `payload.provider`, `payload.message` и `payload.reason`, переводит case в terminal `unverified`, очищает waiting context и сохраняет review-needed reason в `error`;
- `scenario_finished` требует `payload.status` `completed|failed|unverified` и `payload.message`, иначе возвращает `422` без изменения manifest;
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
- terminal `unverified` cases не отправляются в judge и не переводятся в `judged`: API сохраняет runtime status `unverified`, возвращает run judge status `unverified` и не считает это успешной evaluation;
- `write_judge_input()` записывает полный redacted `judge-input.json` без дополнительной фильтрации/обрезки evidence; дополнительно добавляет `evidence_files` с путями к `manifest.json`, самому judge input, будущему evaluation output, trace JSON, browser actions JSONL и screenshots;
- `JudgeRunner` вызывает `codex exec --output-schema ... -o <evaluation.json>` без передачи prompt в argv;
- judge prompt передается в `codex exec` через stdin, остается коротким и содержит инструкции, краткое описание структуры evidence-файлов, идентификаторы case и пути к ним; содержимое `judge-input.json`, callbacks и traces не инлайнится в prompt; trace/prompt/stdout/stderr трактуются как evidence, а не инструкции;
- `evaluation_schema.json` совместима со strict response_format: все object `properties` перечислены в `required`; логически optional поля `evidence_ref` передаются как nullable значения;
- после успешной schema/identity validation `JudgeRunner` перезаписывает `judge_metadata` серверными значениями `backend=codex_exec` и `model=EVAL_JUDGE_MODEL`, чтобы metadata не зависела от сгенерированного model output;
- при timeout, non-zero exit, невалидном JSON/schema mismatch или identity mismatch пишется fallback evaluation со skipped/failed checks.
- async judge-job пишет промежуточный `judge_input` в `artifact_paths`, после каждого judge-eligible case обновляет state на `judged`/`judge_failed`, пересчитывает `summary.json`, а повторный запуск пропускает уже `judged` cases с валидным evaluation artifact и `unverified` cases как review-needed terminal.

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
- список session summaries, включая `status='unverified'`, `order_id`, `order_id_host` и `payment_provider` для `payment_unverified`;
- SSE delivery в session/global subscribers.

Ошибки и ограничения:

- состояние не переживает рестарт `micro-ui`;
- queue ограничена `maxsize=200`; при переполнении самый старый элемент выкидывается;
- `ask_question` берется из `ask_user.payload.message`, а `question` поддерживается только как legacy fallback;
- waiting context очищается на `agent_step_started`, `handoff_resumed`, `payment_ready`, `payment_unverified`, `operator_reply` и `scenario_finished`.

### `micro-ui/app/models.py` и `settings.py`

Pydantic-модели callback, task proxy, reply proxy и session summary. `SessionSummary` хранит `payment_provider` для непроверенного платежного шага. `BUYER_BASE_URL` по умолчанию `http://buyer:8000`.

### Frontend assets

- `micro-ui/app/templates/index.html`: HTML shell.
- `micro-ui/app/static/app.js`: запуск задач, отправка replies, SSE stream, UI state; знает `payment_unverified`, показывает `unverified` как неуспешный/review-needed статус и выводит provider в summary.
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
| Agent memory | Postgres `buyer_agent_memory` и per-step `memory.json` | Последние сообщения для dynamic context files; auth-пакеты не должны попадать через пользовательские replies. | Нужно следить за утечками чувствительных данных при новых источниках. |
| Replies | Postgres `buyer_replies` | Pending/answered/consumed ответы пользователя; legacy auth-like текст дополнительно редактируется на persistence-границе. | Не предназначено для auth-payload reuse. |
| Trace artifacts | `BUYER_TRACE_DIR` | bootstrap prompts, dynamic context files, browser actions JSONL, step trace JSON, auth script traces, knowledge analysis. | Dynamic context writer и knowledge analysis не должны сохранять raw auth/payment/order secrets. |
| User profile | `BUYER_USER_INFO_PATH` | Долговременные пользовательские факты. | Auth, cookies, storageState, платежные данные, одноразовые детали заказа. |
| Evolve loop artifacts | `.tmp/evolve/latest.json`, `.tmp/evolve/cycles/<cycle_id>/**` | `cycle.json`, `summary.md`, `operator-action.json`, baseline/candidate eval request/result JSON, `patch-request.json`, `patch-manifest.json`, `patch-diffstat.json`, `candidate.diff`, `candidate-prepare.log`, `delta_report.json`, redacted logs. | Raw HTTP headers/bodies, tokens/cookies/authorization/password/secret/storageState, raw `orderId`, payment URLs. |

Candidate branches создаются по convention `refs/heads/evolve/cand-YYYYMMDDHHMMSS-NNN-<patch_slug>`; MVP-A оставляет branch/worktree для review, но не push-ит, не двигает `evolve/champion` и не выполняет promotion.

## Внешние зависимости

| Зависимость | Кто использует | Назначение | Типовые отказы |
| --- | --- | --- | --- |
| OpenAI/Codex auth | `AgentRunner`, `PostSessionKnowledgeAnalyzer` | Запуск `codex exec`. | Нет `OPENAI_API_KEY` и `/root/.codex/auth.json`; 401; 429. |
| Codex CLI | `AgentRunner`, analyzer | Structured agent step и post-session analysis. | `CODEX_BIN` не найден; timeout; non-zero return. |
| browser-sidecar CDP | `cdp_tool.py`, script runners | Управление Chromium. | CDP connect/command/transient errors. |
| External Sber cookies API | `ExternalSberCookiesClient`, `BuyerService._resolve_session_auth` | Машинная загрузка cookies через `GET` по полному URL из `SBER_COOKIES_API_URL` при `SBER_AUTH_SOURCE=external_cookies_api` и отсутствии inline auth. | Timeout, HTTP/network error, invalid JSON, invalid/empty cookies payload; все переходят в `auth_external_*` reason-code и guest-flow. |
| Postgres | `PostgresSessionRepository` | Persistent state. | Недоступная БД, pool/init/migration errors. |
| Callback receiver | `CallbackClient` | Доставка событий в `middle`. | timeout, non-2xx, network error -> `CallbackDeliveryError`. |
| Node.js + TSX | `SberIdScriptRunner` | Запуск TypeScript Playwright auth scripts. | Нет Node/TSX, timeout, process failed, invalid JSON. |
| noVNC | оператор через браузер | Handoff-наблюдение и ручные шаги. | Sidecar не поднялся, port недоступен. |
| `eval_service` HTTP API | `scripts/evolve_buyer_loop.py` | Baseline/candidate eval, judge запуск, polling и получение case fingerprints. | Endpoint unavailable, timeout, protocol mismatch, failed/canceled run, `judge_failed`, case mismatch. |
| Git CLI | `scripts/evolve_buyer_loop.py` | Проверка repo/base ref, создание candidate branch/worktree, diff validation, allowed-path add и candidate commit. | Git binary/repo/base ref недоступны, invalid branch name, forbidden touched path, `git diff --check` failure, commit failure. |
| External patch command | `scripts/evolve_buyer_loop.py` при `--patch-mode external-command` | Применяет code/prompt/playbook изменения в candidate worktree и пишет `patch-manifest.json`. | Non-zero exit, missing binary, timeout, missing/invalid manifest, empty diff, forbidden unstaged/staged path diff. |
| Candidate prepare command | `scripts/evolve_buyer_loop.py` при `--candidate-prepare-command` | Rebuild/restart/wait candidate runtime перед candidate eval. | Non-zero exit, missing binary, timeout, candidate healthcheck failure, endpoint указывает не на ожидаемый runtime. |

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

### Automatic purchase scripts

- Hidden automatic purchase scripts retired: `purchase_script_*` не является runtime failure-сигналом основного пути.
- Любой будущий custom-script contract должен быть явным и должен проходить verifier перед `payment_ready`.

### Knowledge analyzer

- `skipped/no_api_key_or_oauth`
- `failed/codex_binary_missing`
- `failed/timeout`
- `failed/codex_failed`
- `failed/parse_failed`
- path-safety `ValueError`

Ошибки analyzer не меняют итог сессии и не отправляют внешний callback.

### Evolve loop

- exit `0`: команда завершилась и записала ожидаемые artifacts, включая `inconclusive` или `needs_operator`.
- exit `1`: unexpected internal error.
- exit `2`: usage/config/precondition error.
- exit `3`: eval HTTP unavailable, timeout или protocol mismatch.
- exit `4`: git или patch command failure.
- exit `5`: unsafe artifact persistence/redaction failure.
- `verdict.status="inconclusive"`: отсутствует candidate eval, baseline unavailable, judge failure, case fingerprint mismatch или insufficient evidence.
- `verdict.status="needs_operator"`: baseline/candidate run остановлен на `waiting_user`/handoff и записан `operator-action.json`.
- `delta_status="not_comparable"`: candidate eval отсутствует или case fingerprints не совпали.

## Тестовая карта

| Путь | Что покрывает |
| --- | --- |
| `buyer/tests/test_persistent_state.py` | Store/repository lifecycle, restore, reply validation, redaction persistent state, stale runtime sessions. |
| `buyer/tests/test_url_policy.py` | URL policy для `start_url`/`callback_url`, включая запрет query/fragment в callback URL и trusted allowlist. |
| `buyer/tests/test_auth_reply_removal.py` | MON-29 regression: inline invalid auth уходит в guest без `ask_user`, auth-script refresh/failure уходит в heuristic без запроса auth-пакета, parser reply-auth удален. |
| `buyer/tests/test_external_auth.py` | MON-30 regression: external cookies payload validation, httpx `MockTransport`, timeout mapping, source priority inline over external и guest fallback с `auth_external_*` reason-code. |
| `buyer/tests/test_auth_secret_retention.py` | Runtime-only SberId auth payload, persistence redaction legacy auth-like replies, временный storageState-файл, auth runner stale output/unique output path/non-zero diagnostics behavior. |
| `buyer/tests/test_script_runtime.py` | Чтение script output с fallback на stdout и устойчивость к невалидному output-файлу. |
| `buyer/tests/test_prompt_externalization.py` | Review TODO hygiene, instruction manifest, dynamic context files, bootstrap prompt и отсутствие raw auth/profile/memory/CDP preflight blobs в prompt. |
| `buyer/tests/test_sberid_auth_idempotency.py` | Litres/Brandshop auth snapshot helpers и source-order regression для already-authenticated precheck до entry navigation/Sber ID clicks. |
| `buyer/tests/test_payment_verifier_and_ready.py` | Provider parsers PayEcom/YooMoney, Litres/Brandshop merchant policy, rejection matrix, `payment_ready.order_id_host` и unknown-merchant `unverified`. |
| `buyer/tests/test_brandshop_generic_instruction.py` | Brandshop generic prompt/instruction и snapshot hints для Jordan Air High 45 EU пути без automatic purchase script. |
| `buyer/tests/test_callback_trace_slimming.py` | Slim callback trace summary и OpenAPI requirement для `order_id_host`. |
| `buyer/tests/test_knowledge_analyzer.py` | Sanitization, safe paths, trace refs, analysis payload/output. |
| `buyer/tests/test_cdp_recovery.py` | CDP recovery markers, retries, transient behavior, Litres generic path и payment-boundary regression для supported/unsupported domains. |
| `buyer/tests/test_observability_and_cdp_tool.py` | Trace/browser action metrics, CDP tool output limits, observability, нейтральный выбор CDP-страницы и prompt guardrails для автономного поиска товара до запроса адреса доставки. |
| `eval_service/tests/test_case_registry.py` | Загрузка YAML eval templates, repository smoke cases и regression на Brandshop search template `?st={{ search_query }}`. |
| `eval_service/tests/test_callbacks.py` | Eval callback receiver, operator reply flow, `payment_unverified`, malformed terminal callbacks, manifest redaction на диске. |
| `eval_service/tests/test_run_store.py` | File manifest lifecycle, callback event redaction/deduplication, atomic summary writes. |
| `eval_service/tests/test_api.py` | Eval API, run detail/dashboard/judge payloads и outward sanitization. |
| `eval_service/tests/test_orchestrator.py` | Eval run orchestration, payment_ready grace, terminal `unverified`, waiting/reply resume progression. |
| `scripts/tests/test_evolve_buyer_loop.py` | MVP-A evolve-loop CLI: doctor preflight, baseline/candidate eval client, async judge polling, comparator/scoring, case fingerprints, placeholder/external patch, branch/worktree/commit flow, candidate prepare, handoff `operator-action.json`, `continue`, offline `compare`, artifact/redaction behavior. |
| `micro-ui/tests/test_store_stream.py` | CallbackStore, дедупликация, SSE queue behavior. |
| `micro-ui/tests/test_design_handoff.py` | Session summary для `ask_user`/waiting progression, `payment_ready.order_id_host` и `payment_unverified` в `CallbackStore`. |
| `micro-ui/tests/test_eval_shell_static.py` | Поведенчески значимый proxy timeout для долгого создания eval run и статический contract для отображения `payment_unverified`. |

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
