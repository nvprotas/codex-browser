# Тотальное ревью кодовой базы от 2026-04-28

## Объем и методика

Ревью выполнено как оркестрация 13 readonly-субагентов на `gpt-5.5` с `xhigh` effort: первичная волна по подсистемам, затем перекрестное ревью по безопасности, контрактам, payment boundary, persistence, eval, тестам и финальная triage-дедупликация.

Проверялись:

- `buyer`: orchestration, persistence, callback delivery, prompt/runner, CDP tool, SberId/purchase scripts.
- `browser`: Chromium/CDP/noVNC runtime.
- `eval_service`: callbacks, orchestrator, judge pipeline, trace collector, run store, dashboard.
- `micro-ui`: callbacks, SSE, buyer/eval UI, proxy.
- `docs`, OpenAPI/JSON Schema, Docker/env и тесты.

## Краткий итог

Текущая ветка не готова к эксплуатации за пределами локальной доверенной машины. Главные блокеры: открытые control-plane порты без auth, SSRF/эксфильтрация через `callback_url` и `start_url`, утечки `storageState`/payment/callback secrets в trace/Postgres/eval artifacts, недостаточная проверка SberPay evidence, а также сломанный eval/micro-ui reply flow.

Приоритет исправлений:

1. Убрать durable-хранение секретов: raw `storageState`, auth replies, callback tokens, payment URLs/order IDs, prompt/trace/eval manifest.
2. Закрыть сетевой периметр: auth для `/v1/*`, micro-ui callbacks, noVNC/CDP; bind localhost/internal-only; запретить произвольные callback/start URLs.
3. Ввести единый payment verifier: `payment_ready` только после доменно-валидированного SberPay evidence.
4. Синхронизировать callback/API contracts между buyer, eval_service, micro-ui и docs.
5. Ужесточить runner/script/test contracts: JSON Schema = Pydantic, non-zero script exit = failure, реальные behavioral tests.

## Release Blockers

### RB-01. Auth/storageState сохраняется в долговременные артефакты и prompt

**Severity:** critical
**Файлы:** `buyer/app/service.py:463`, `buyer/app/service.py:668`, `buyer/app/persistence.py:383`, `buyer/app/auth_scripts.py:148`, `buyer/app/runner.py:131`

**Проблема:** auth-refresh reply с JSON `storageState` сохраняется как обычный пользовательский ответ: в `buyer_replies.message`, затем в `buyer_agent_memory.text`, затем попадает в prompt и prompt trace. Отдельно auth script пишет raw Playwright `storageState` в `BUYER_TRACE_DIR/<session_id>/auth-storage-attempt-XX.json` и не удаляет файл.

**Доказательство:** `_ask_user_for_reply()` безусловно добавляет `reply_text` в agent memory; persistence пишет reply/message и memory raw; `build_agent_prompt()` включает последние memory items; `AgentRunner` пишет prompt file; `SberIdScriptRunner` пишет raw storage state в trace dir под `/workspace`.

**План исправления:**

- Разделить типы reply: обычный user reply и auth payload reply.
- Для auth reply парсить payload до persistence; в БД хранить только статус и sanitized summary.
- В agent memory добавлять `[SBERID_AUTH_RECEIVED]` без cookies/localStorage.
- Писать storageState во временный файл вне `/workspace` с `0600`, удалять в `finally`, лучше передавать через stdin/fd/tmpfs.
- Добавить regressions: auth reply не попадает в `buyer_replies`, `buyer_agent_memory`, prompt preview, trace files.

### RB-02. Control-plane и browser-sidecar открыты без защиты

**Severity:** critical
**Файлы:** `docker-compose.yml:28`, `docker-compose.yml:92`, `docker-compose.yml:110`, `browser/entrypoint.sh:43`, `browser/entrypoint.sh:55`, `browser/entrypoint.sh:69`, `browser/entrypoint.sh:79`, `buyer/app/main.py:101`

**Проблема:** `buyer` (`8000`), `micro-ui` (`8080`), noVNC (`6901`) и CDP (`9223`) публикуются на все интерфейсы. API buyer не требует auth; noVNC/CDP доступны без auth; Chromium remote debugging слушает `0.0.0.0`.

**Доказательство:** compose использует `"8000:8000"`, `"8080:8080"`, `"6901:6901"`, `"9223:9223"`; FastAPI endpoints `/v1/tasks`, `/v1/sessions`, `/v1/replies` не имеют auth dependency; `websockify` публикует noVNC на `0.0.0.0`; CDP опубликован через Chromium/socat.

**План исправления:**

- По умолчанию bind host-порты только на `127.0.0.1` или убрать публикацию CDP наружу.
- CDP оставить только во внутренней Docker-сети.
- Добавить API token/mTLS для `/v1/*` и micro-ui callback endpoint.
- noVNC вынести за authenticated reverse proxy или включить auth.
- Убрать `--remote-allow-origins=*`, если нет строгой необходимости.

### RB-03. SSRF и эксфильтрация через `callback_url` и `start_url`

**Severity:** critical
**Файлы:** `buyer/app/models.py:33`, `buyer/app/models.py:35`, `buyer/app/callback.py:53`, `buyer/app/runner.py:452`, `buyer/app/prompt_builder.py:50`

**Проблема:** `callback_url` и `start_url` принимаются как произвольные строки. Buyer POST-ит полный callback envelope на `callback_url`; browser/Codex flow открывает `start_url`. Это позволяет SSRF, обращение к internal/private hosts и эксфильтрацию trace/artifacts.

**Доказательство:** `TaskCreateRequest.callback_url` и `start_url` имеют только `min_length`; `CallbackClient.deliver()` делает `httpx.post(callback_url, json=...)`; runner/CDP сбрасывает браузер на `start_url`; prompt требует первым действием `goto start_url`. Ревьюеры подтвердили acceptance для loopback/link-local style URLs.

**План исправления:**

- Убрать per-task arbitrary `callback_url` или заменить registry ID на сервере.
- Если URL остается: валидировать scheme, host, DNS-resolved IP; блокировать loopback/private/link-local/metadata ranges, userinfo и unsafe redirects.
- Для `start_url` ввести public URL policy и domain allowlist/approval для non-public hosts.
- Добавить негативные тесты на `127.0.0.1`, `::1`, `169.254.169.254`, private ranges и redirect-to-private.

### RB-04. Callback/eval secrets и raw events сохраняются и отдаются наружу

**Severity:** critical
**Файлы:** `eval_service/app/callback_urls.py:19`, `buyer/app/persistence.py:277`, `buyer/app/main.py:149`, `eval_service/app/run_store.py:187`

**Проблема:** `EVAL_CALLBACK_SECRET` передается в query string (`?token=...`), затем buyer сохраняет raw `callback_url` в `buyer_sessions.callback_url` и возвращает его через API. Eval `manifest.json` сохраняет raw callback events/idempotency/artifacts, включая order IDs, payment URLs и tokens.

**Доказательство:** `build_buyer_callback_url()` добавляет token в URL; Postgres schema хранит `callback_url TEXT`; `_to_view()` возвращает `callback_url`; `RunStore._update_case()` кладет `callback_event` в manifest без redaction.

**План исправления:**

- Перенести callback auth в header/HMAC signature, не в query.
- Хранить delivery secret отдельно от display URL; API отдавать redacted URL.
- Redact на write в eval manifest или хранить raw events только в защищенном debug artifact с TTL.
- Добавить тесты, читающие `manifest.json` с диска и проверяющие отсутствие tokens/order/payment URLs/idempotency secrets.

### RB-05. Payment boundary не гарантирует SberPay evidence

**Severity:** critical/high
**Файлы:** `buyer/app/service.py:309`, `buyer/app/service.py:876`, `buyer/app/service.py:941`, `buyer/app/codex_output_schema.json:5`, `buyer/app/purchase_scripts.py:181`

**Проблема:** для non-Litres любой `completed + order_id` сразу эмитит `payment_ready` без SberPay evidence. Litres verifier принимает `http://payecom.ru`, subdomains вроде `evil.payecom.ru` и path prefix `/pay_ru_malicious`. JSON Schema/runner/script result contracts не обеспечивают надежный evidence boundary.

**Доказательство:** `_completed_result_contract_failure()` возвращает `None` для всех не-Litres; `_handle_completed()` шлет `payment_ready` для любого truthy `order_id`; `_payecom_order_id_from_url()` использует `host.endswith('.payecom.ru')` и `path.startswith('/pay_ru')`.

**План исправления:**

- `payment_ready` разрешать только после domain-specific verifier.
- Для unsupported domains возвращать `failed`/`needs_user_input`/handoff, но не success.
- Для Litres требовать `scheme == "https"`, `hostname == "payecom.ru"`, точный `path == "/pay_ru"` и exact `orderId`.
- Расширить `PaymentEvidence` на доменные источники и покрыть negative tests.
- Синхронизировать `AgentOutput`, JSON Schema и fake outputs.

### RB-06. Eval/micro-ui reply и ask_user contracts сломаны

**Severity:** high
**Файлы:** `micro-ui/app/static/eval.js:681`, `eval_service/app/callbacks.py:35`, `buyer/app/service.py:650`, `micro-ui/app/store.py:84`, `docs/callbacks.openapi.yaml:275`

**Проблема:** eval UI отправляет `session_id` в strict `OperatorReplyRequest`, который принимает только `message`/`reply_id`, поэтому операторский reply получает `422`. Обычный micro-ui ищет `ask_user.payload.question`, тогда как buyer и docs используют `payload.message`, поэтому UI знает `reply_id`, но не показывает вопрос.

**Доказательство:** `eval.js` формирует body `{session_id, reply_id, message}`; `OperatorReplyRequest` наследует `StrictBaseModel(extra='forbid')`; `BuyerService._ask_user_for_reply()` отправляет `message`; `CallbackStore` читает `question`.

**План исправления:**

- Убрать `session_id` из eval UI body или добавить optional field с проверкой совпадения.
- В `micro-ui CallbackStore` читать `message` как primary и `question` как legacy fallback.
- Обновить fixtures/tests на canonical `message + reply_id`.
- Добавить integration test: UI/proxy -> eval reply endpoint.

## High Severity Findings

### H-01. Внешние callbacks отдают raw trace/stream telemetry до redaction

**Файлы:** `buyer/app/service.py:768`, `buyer/app/service.py:784`, `buyer/app/service.py:1020`, `buyer/tools/cdp_tool.py:150`, `buyer/tools/cdp_tool.py:514`

**Проблема:** callback payload может включать prompt preview, stdout/stderr tails, browser action tail, URLs, hrefs, attrs и текст страницы. Это доставляется внешнему receiver до persistence redaction.

**План:** разделить internal raw trace и external callback payload; redact перед `append_event`/`deliver`; для non-local callbacks отключить или минимизировать `agent_stream_event`.

### H-02. Persistent redaction не чистит payment/order/token values

**Файлы:** `buyer/app/persistence.py:273`, `buyer/app/persistence.py:611`, `buyer/app/persistence.py:692`, `buyer/tests/test_persistent_state.py:195`

**Проблема:** sanitizer чистит в основном sensitive keys, но не строковые значения: `order_id`, `payment_frame_src`, `_url`, query token. `buyer_sessions.metadata` пишется raw.

**План:** внедрить единый URL/text sanitizer; применять к session metadata, event payload, artifacts metadata/uri, `last_error`, `delivery_error`; хранить order/payment identifiers как presence/hash.

### H-03. LLM `profile_updates` пишутся в user profile без sanitizer

**Файлы:** `buyer/app/service.py:709`, `buyer/app/user_profile.py:67`

**Проблема:** model output может добавить token/order/payment/cookie-like строки в долговременный `user-buyer-info.md`; enforcement отсутствует.

**План:** allowlist долговременных preference facts, denylist auth/payment/order/url/token markers, длина и дедупликация; negative tests.

### H-04. Script runners принимают stale/non-zero payload

**Файлы:** `buyer/app/auth_scripts.py:240`, `buyer/app/purchase_scripts.py:96`, `buyer/app/purchase_scripts.py:181`, `buyer/app/script_runtime.py:29`

**Проблема:** JSON payload читается даже при non-zero exit code; purchase output path фиксированный `purchase-script-result.json` на сессию и может вернуть stale completed payload.

**План:** удалять output перед запуском; использовать уникальный output path per attempt; non-zero exit всегда failure; проверять mtime/current attempt; payload при failure использовать только как diagnostics.

### H-05. CDP tool выбирает старую Litres-вкладку вместо текущего магазина

**Файлы:** `buyer/tools/cdp_tool.py:182`

**Проблема:** `_page_priority()` hardcoded поднимает любой URL, содержащий `litres.ru`, выше других HTTP pages. Для Brandshop/других магазинов старая вкладка Litres может стать рабочей.

**План:** убрать hardcoded Litres priority; выбирать page по session context/start_url origin; парсить hostname, а не substring.

### H-06. SberId scripts восстанавливают только cookies и слабее проверяют Brandshop auth

**Файлы:** `buyer/scripts/sberid/litres.ts:487`, `buyer/scripts/sberid/brandshop.ts:391`, `buyer/scripts/sberid/brandshop.ts:588`

**Проблема:** `storageState.origins/localStorage` игнорируются; Brandshop `auth_ok` подтверждается возвратом на host без profile/auth marker.

**План:** создавать fresh Playwright context с полным `storageState` или восстанавливать origins/localStorage; для Brandshop проверять устойчивые profile/account/logout markers.

### H-07. Eval callback receiver принимает malformed terminal payloads

**Файлы:** `eval_service/app/callbacks.py:233`, `eval_service/app/callbacks.py:238`, `docs/callbacks.openapi.yaml:328`

**Проблема:** `payment_ready` без `order_id` переводит case в `PAYMENT_READY`; `scenario_finished` без `status` считается success.

**План:** добавить discriminated payload validation по `event_type`; invalid terminal payload возвращать `422` и не менять state.

### H-08. Eval trace collector не symlink-safe

**Файлы:** `eval_service/app/trace_collector.py:43`

**Проблема:** collector следует symlink через `is_dir()`/`glob()` и может прочитать traces вне `trace_root`.

**План:** запрещать symlink на каждом уровне date/time/session/trace file; проверять `resolve(strict=True)` относительно исходного root; добавить symlink regression.

### H-09. `payment_ready` может зависнуть и блокировать judge

**Файлы:** `eval_service/app/callbacks.py:233`, `eval_service/app/api.py:30`

**Проблема:** callback переводит case в `payment_ready`; если активный orchestrator waiter отсутствует, `POST /runs/{id}/judge` считает case incomplete и возвращает `409`.

**План:** callback handler должен планировать grace-finalization, либо judge должен финализировать stale `payment_ready` после grace window.

### H-10. Eval auth profiles принимают malformed storageState

**Файлы:** `eval_service/app/auth_profiles.py:50`, `buyer/app/service.py:864`

**Проблема:** eval profile `{ "foo": "bar" }` считается valid, хотя buyer требует `cookies` и `origins` списками; run стартует, затем buyer запросит auth refresh.

**План:** валидировать Playwright storageState shape в `AuthProfileLoader`; malformed profile должен стать `skipped_auth_missing/auth_profile_invalid`.

### H-11. Eval UI/proxy и long-running judge несовместимы по timeout

**Файлы:** `micro-ui/app/main.py:34`, `eval_service/app/orchestrator.py:77`, `eval_service/app/judge_runner.py:18`

**Проблема:** `POST /runs/{id}/judge` через micro-ui proxy имеет 60s timeout, а judge timeout 600s на case и выполняется последовательно.

**План:** сделать async judge job с polling; минимум дать отдельный long timeout для judge route и тест на timeout contract.

## Medium Severity Findings

### M-01. Non-terminal sessions после рестарта остаются stale

**Файлы:** `buyer/app/main.py:89`, `buyer/app/state.py:189`, `buyer/app/state.py:266`, `buyer/app/state.py:344`

**Проблема:** startup только инициализирует store; `running/waiting_user` без runner остаются в БД, replies reject-ятся, новая сессия разрешается. Это не блокирует runtime slot, но оставляет неконсистентный статус и возможное смешение browser state.

**План:** startup reconciliation: перевод non-terminal без runner в `failed/interrupted` или durable resume queue; сброс browser context перед новой сессией.

### M-02. Callback at-least-once задокументирован, но outbox replay нет

**Файлы:** `docs/callbacks.openapi.yaml:11`, `buyer/app/service.py:759`, `buyer/app/main.py:89`, `buyer/app/persistence.py:230`

**Проблема:** событие пишется перед доставкой и `delivery_status` хранится, но startup/background worker не переотправляет pending/failed. Crash между append и deliver теряет доставку.

**План:** реализовать persisted outbox worker/replay на startup с тем же `idempotency_key`; покрыть crash/pending/failed scenarios.

### M-03. Eval ids есть runtime, но отсутствуют в callback schema/persistence/micro-ui

**Файлы:** `buyer/app/models.py:66`, `docs/callbacks.openapi.yaml:137`, `buyer/app/persistence.py:323`, `micro-ui/app/models.py:9`

**Проблема:** `eval_run_id/eval_case_id` отправляются в envelope, но OpenAPI запрещает extra fields, Postgres load/save и micro-ui их теряют.

**План:** выбрать канон: либо добавить optional eval ids в callback OpenAPI, persistence и micro-ui model, либо убрать top-level eval ids и коррелировать только server-side.

### M-04. OpenAPI buyer строже/иначе runtime-моделей

**Файлы:** `docs/openapi.yaml:269`, `buyer/app/models.py:22`, `buyer/app/main.py:101`

**Проблема:** docs используют `additionalProperties: false`, runtime Pydantic в ряде моделей extra игнорирует; `storage_state` alias принимается кодом, но не документирован; generated FastAPI OpenAPI не содержит явные `404/409`.

**План:** либо `extra='forbid'` в Pydantic, либо смягчить docs; документировать `storage_state`; добавить `responses={...}` или schema-diff test.

### M-05. Eval receiver response shape расходится с callback docs

**Файлы:** `eval_service/app/callbacks.py:50`, `docs/callbacks.openapi.yaml:41`

**Проблема:** документированный receiver возвращает `CallbackAck {accepted, duplicate}`, eval receiver возвращает `{eval_run_id, eval_case_id, state}` и не сообщает duplicate.

**План:** сделать eval receiver ack-compatible или вынести отдельный eval callback contract.

### M-06. Micro-ui не очищает waiting после обычного reply

**Файлы:** `micro-ui/app/store.py:89`, `buyer/app/state.py:259`

**Проблема:** после reply buyer переводит state в running, но не отправляет отдельный callback; micro-ui очищает waiting только на `handoff_resumed/scenario_finished`, поэтому следующий `agent_step_started` может оставить UI в `waiting_user`.

**План:** micro-ui должен сбрасывать waiting на `agent_step_started` после ask_user или buyer должен отправлять `reply_accepted`.

### M-07. Eval API отдает 500 на invalid `eval_run_id`

**Файлы:** `eval_service/app/api.py:185`

**Проблема:** error path ловит `ValueError`, но повторно вызывает `store.manifest_path(eval_run_id)`, снова бросая `ValueError`.

**План:** валидировать path segment до чтения и возвращать `422`; не вызывать path builder в error path.

### M-08. Upstream buyer reply errors превращаются в 500 на eval endpoint

**Файлы:** `eval_service/app/callbacks.py:131`, `eval_service/app/buyer_client.py:124`

**Проблема:** `httpx.HTTPStatusError` от buyer `/v1/replies` пробрасывается как internal server error.

**План:** ловить upstream HTTP errors, восстанавливать waiting-state, возвращать тот же status/detail или нормализованный 409/502.

### M-09. Payment URL redaction в judge input слишком сильный

**Файлы:** `eval_service/app/redaction.py:211`

**Проблема:** payment URL целиком заменяется на `[redacted-payment-url]`, judge теряет host/path evidence (`payecom.ru/pay_ru` vs SBP/иной checkout).

**План:** сохранять `scheme://host/path`, редактировать только query/fragment/path IDs; добавить tests, что evidence остается достаточным.

### M-10. Eval evidence refs могут быть пустыми

**Файлы:** `eval_service/app/evaluation_schema.json:85`, `eval_service/app/models.py:181`

**Проблема:** schema/model принимают `{}` как `EvidenceRef`, хотя prompt требует ссылку на event/trace/action/screenshot.

**План:** добавить `minProperties: 1`/`anyOf required` в JSON Schema и Pydantic validator.

### M-11. JudgeRunner direct contract нестабилен при missing codex

**Файлы:** `eval_service/app/judge_runner.py:91`

**Проблема:** `JudgeRunner.run()` не пишет fallback evaluation при `FileNotFoundError`/`OSError` запуска `codex`; API частично ловит, direct runner падает.

**План:** ловить `OSError` рядом с `TimeoutExpired`, возвращать `judge_failed` fallback.

### M-12. Dashboard baseline/success-rate не учитывают `evaluation.status`

**Файлы:** `eval_service/app/api.py:529`, `eval_service/app/aggregation.py:223`

**Проблема:** `judge_failed` с ok critical checks попадает в baseline и success-rate.

**План:** baseline/success-rate считать только для `status == judged`.

### M-13. Eval auth profiles volume невозможно удобно заполнить

**Файлы:** `docker-compose.yml:131`, `.env.example`

**Проблема:** auth profiles смонтированы как read-only named volume без host path переменной; `.env.example` не дает `EVAL_AUTH_PROFILES_HOST_DIR`.

**План:** заменить на bind mount `${EVAL_AUTH_PROFILES_HOST_DIR}:/run/eval/auth-profiles:ro` или документировать init/populate workflow.

### M-14. TS сценарии не имеют quality gate

**Файлы:** `buyer/scripts/package.json:6`, `buyer/tests/test_observability_and_cdp_tool.py:419`

**Проблема:** `scripts` пустой; smoke tests skip при отсутствии `node_modules`, поэтому TS scripts фактически optional.

**План:** добавить `typecheck`, `test`, `typescript`, `@types/node`; в CI отсутствие `node_modules`/typecheck должно быть failure.

### M-15. Allowlist принимает subdomain, registry требует exact domain

**Файлы:** `buyer/app/auth_scripts.py:41`, `buyer/app/purchase_scripts.py:57`

**Проблема:** `m.litres.ru` allowlisted, но registry lookup ищет exact key `m.litres.ru`, которого нет, поэтому script fallback не запустится.

**План:** резолвить canonical allowlist match и передавать registry key (`litres.ru`).

### M-16. Repository docs не описывают eval_service

**Файлы:** `README.md:3`, `docs/repository-map.md:13`

**Проблема:** README и repository map не отражают `eval_service`, eval API, judge flow, auth profiles, dashboard, eval artifacts. `AGENTS.md` требует поддерживать карту репозитория.

**План:** добавить разделы `eval_service`, `eval/cases`, eval artifacts/env/API/tests в `docs/repository-map.md`; обновить README ports/env/eval workflow.

## Проблемы тестов

### T-01. Codex fake outputs проверяют не реальный schema contract

**Файлы:** `buyer/tests/test_cdp_recovery.py:306`, `buyer/app/codex_output_schema.json:5`, `buyer/app/models.py:93`

**Проблема:** тестовые outputs содержат `artifacts` и часто пропускают `payment_evidence/profile_updates`, тогда как `codex_output_schema.json` запрещает `artifacts` и требует эти поля. Tests обходят реальный `codex --output-schema`.

**План:** синхронизировать schema/model; добавить schema consistency test; обновить fake outputs под реальный контракт.

### T-02. Micro-ui eval “reply flow” test не выполняет reply flow

**Файлы:** `micro-ui/tests/test_eval_shell_static.py:121`, `micro-ui/app/static/eval.js:681`

**Проблема:** тест ищет строки и `await loadRunDetail`, но не отправляет form submit; поэтому не ловит extra `session_id -> 422`.

**План:** заменить на jsdom/Playwright или ASGI integration с проверкой exact request body.

### T-03. Fixtures закрепляют старый `ask_user.question`

**Файлы:** `micro-ui/tests/test_design_handoff.py:35`, `eval_service/tests/test_callbacks.py:101`, `eval_service/tests/test_api.py:226`, `eval_service/tests/test_orchestrator.py:819`

**Проблема:** тесты используют `payload.question/options`, хотя canonical buyer/docs contract `message/reply_id`.

**План:** обновить canonical fixtures на `message + reply_id`; legacy `question` оставить в отдельном backward-compat тесте.

### T-04. Нет negative/security tests для event-specific callback payload

**Файлы:** `eval_service/tests/test_callbacks.py:245`, `eval_service/app/callbacks.py:225`

**Проблема:** payload тип `dict[str, Any]`, состояние меняется по `event_type`; тесты не проверяют invalid terminal payload.

**План:** добавить negative tests: `ask_user` без `message/reply_id`, `payment_ready` без `order_id/message`, `scenario_finished` без valid status.

### T-05. Redaction tests проверяют API response, но не storage artifacts

**Файлы:** `eval_service/tests/test_api.py:635`, `eval_service/app/run_store.py:63`, `eval_service/app/run_store.py:186`

**Проблема:** tests проходят, хотя raw `manifest.json` содержит tokens/order/payment URLs.

**План:** читать on-disk manifest/evaluation artifacts/evidence refs в тестах и проверять redaction at write/read.

### T-06. Postgres integration skipped by default

**Файлы:** `buyer/tests/test_persistent_state.py:265`

**Проблема:** ключевой Postgres restore/integration test пропускается без `BUYER_TEST_DATABASE_URL`.

**План:** поднять Postgres через compose/testcontainers в CI или hard-fail в CI при отсутствии env; добавить реальные migration/idempotency/delivery-state tests.

### T-07. Redaction-тест закрепляет retention `order_id`

**Файлы:** `buyer/tests/test_persistent_state.py:195`

**Проблема:** тест с названием про sensitive redaction явно assert-ит наличие `order-123`.

**План:** разделить safe metadata и payment/order secrets; добавить negative на `order_id`, payment URL, tokenized trace/error payloads.

## Проверки, выполненные ревьюерами

Выборочно запускались:

- `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_cdp_recovery.py buyer/tests/test_observability_and_cdp_tool.py buyer/tests/test_script_runtime.py buyer/tests/test_persistent_state.py`
- `uv run --with-requirements eval_service/requirements.txt --with pytest pytest eval_service/tests`
- `uv run --with-requirements micro-ui/requirements.txt --with pytest pytest micro-ui/tests/test_store_stream.py micro-ui/tests/test_eval_shell_static.py micro-ui/tests/test_design_handoff.py`
- `uv run --with pytest --with-requirements buyer/requirements.txt --with-requirements eval_service/requirements.txt --with-requirements micro-ui/requirements.txt pytest --collect-only -q buyer/tests eval_service/tests micro-ui/tests` -> `217 tests collected`
- `npm ci --dry-run` в `buyer/scripts` -> lockfile согласован.
- `npm test` и `npm run typecheck` в `buyer/scripts` -> отсутствуют scripts.

Текущие тесты в основном проходят, но это не является сигналом готовности: несколько тестов закрепляют неверные контракты или не проверяют фактический user flow.

## Рекомендуемый порядок работ

1. **Security patch:** закрыть порты, добавить auth, убрать arbitrary callback/start URL, убрать query token.
2. **Secret redaction patch:** ephemeral auth storage, redacted auth replies, redacted Postgres/eval manifest/prompt/trace/profile updates.
3. **Payment boundary patch:** strict evidence verifier, schema/model sync, non-Litres no-success-without-verifier.
4. **Contract patch:** callback schema, eval ids, `ask_user.message`, eval reply body, malformed callback validation.
5. **Runtime reliability patch:** script runner exit/stale output, CDP page selection, restart reconciliation, outbox replay.
6. **Eval hardening patch:** trace symlink guard, stale `payment_ready`, long-running judge design, storageState profile validation.
7. **Test rebuild:** replace static/string tests with behavioral tests, make TS/Postgres gates real, add negative security tests.
