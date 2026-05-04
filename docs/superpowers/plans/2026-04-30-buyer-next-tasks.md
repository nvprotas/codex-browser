# Buyer Next Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести Litres на generic-покупку, сделать SberId auth scripts идемпотентными, добавить Brandshop payment verifier и generic playbook, сократить callback trace payload и расширить `payment_ready` хостом источника `orderId`.

**Architecture:** `buyer` остается orchestration service: доменные auth scripts только готовят авторизованный browser context, generic Codex-agent выполняет покупку, а domain-specific verifier принимает решение о `payment_ready`. Callback payload-ы становятся меньше и безопаснее: полный trace остается в trace-файлах, callbacks передают только контрактные summary/ref-поля. `payment_ready` становится более проверяемым за счет обязательного `order_id_host`, полученного verifier-ом из evidence.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, pytest/unittest, TypeScript, Playwright, TSX, OpenAPI YAML, Mermaid-free Markdown docs.

**Linear:** Требуется создать или синхронизировать 6 issue:
- `Buyer: удалить Litres purchase script и перевести покупку на generic-agent`
- `Buyer: SberId auth scripts должны пропускать повторный login при активной авторизации`
- `Buyer: Brandshop domain-specific SberPay payment verifier`
- `Buyer: Brandshop generic-agent playbook для поиска Jordan Air High 45 EU`
- `Buyer: сократить trace payload в callbacks`
- `Buyer: добавить order_id_host в payment_ready`

---

## Контекст и ограничения

Текущий фактический runtime описан в `docs/litres-brandshop-agent-flow.md`.

Ключевые текущие свойства после выполнения плана:

- Litres имеет published auth script с idempotent precheck, не имеет purchase script и проходит покупку через generic-agent со строгим PayEcom verifier.
- Brandshop имеет published auth script с idempotent precheck, не имеет purchase script и проходит покупку через generic-agent с Brandshop playbook и строгим YooMoney verifier.
- Generic Codex-agent управляет браузером через `buyer/tools/cdp_tool.py`, не через прямой Playwright API.
- `payment_ready` содержит `order_id`, verifier-approved `order_id_host` и `message`.
- `agent_step_finished` callbacks несут slim trace summary; полный trace остается в локальных trace-файлах.
- `eval_service` считает `payment_ready` терминальным успешным состоянием case и валидирует callback payload, включая обязательный `order_id_host`.
- Litres и Brandshop eval-cases включены; Brandshop case `brandshop_purchase_smoke_001` доступен через registry.

Общие правила для всех задач:

- Не проводить реальный платеж.
- Не ослаблять SberPay-only policy: SBP/FPS/СБП не являются SberPay.
- Не сохранять cookies, localStorage, auth tokens или payment secrets в callbacks, docs, trace summaries или persistent artifacts.
- Любой `payment_ready` разрешен только после domain-specific verifier.
- Обновлять `docs/repository-map.md`, если меняется фактическая структура, публичный контракт, runtime-поведение, ошибки или тестовые границы.
- Обновлять `docs/litres-brandshop-agent-flow.md`, потому что все 6 задач меняют описанное там фактическое поведение.

## Рекомендуемый порядок выполнения

1. Добавить `order_id_host` в `payment_ready`, потому что это contract foundation для Brandshop verifier.
2. Сократить trace payload в callbacks, чтобы новые callback tests и eval tests не закрепляли старый payload.
3. Удалить Litres purchase script и закрепить generic-agent path.
4. Сделать SberId auth scripts идемпотентными.
5. Добавить Brandshop verifier и включить Brandshop eval-case.
6. Добавить Brandshop generic-agent playbook как prompt/knowledge/eval guidance без purchase script.

Такой порядок минимизирует пересечения: сначала меняются callback contracts, потом domain behavior.

## Task 1: Удалить purchase script для Litres, покупку выполнять generic-agent

### Требование

Litres не должен иметь специальный быстрый Playwright purchase script. После SberId auth `buyer` должен переходить к generic Codex-loop, и именно generic-agent должен выполнить поиск, выбор книги, корзину, checkout и достижение SberPay/PayEcom boundary.

Доменные Litres prompt-правила и Litres verifier остаются: generic-agent все равно обязан вернуть `payment_evidence.source=litres_payecom_iframe` и URL iframe `https://payecom.ru/pay_ru?orderId=...`, а `payment_verifier.py` должен строго проверить evidence.

### Files

- Delete: `buyer/scripts/purchase/litres.ts`
- Modify: `buyer/app/purchase_scripts.py`
- Modify: `buyer/app/settings.py`
- Modify: `docker-compose.yml`
- Modify: `docker-compose.openclaw.yml`
- Modify: `buyer/app/service.py`
- Modify: `buyer/tests/test_cdp_recovery.py`
- Modify: `buyer/tests/test_observability_and_cdp_tool.py`
- Modify: `buyer/tests/test_script_runtime.py`
- Modify: `docs/litres-brandshop-agent-flow.md`
- Modify: `docs/repository-map.md`

### Acceptance criteria

- `rg -n "purchase/litres|buyer/scripts/purchase/litres.ts|Litres purchase-скрипт|Litres purchase script" buyer docs eval` не находит current-behavior references.
- `PurchaseScriptRunner` infrastructure может остаться для будущих доменов, но registry не содержит `litres.ru`.
- Default `PURCHASE_SCRIPT_ALLOWLIST` не содержит `litres.ru`.
- `BuyerService._run_purchase_script_flow()` для `litres.ru` возвращает `None` и не блокирует generic runner.
- Litres eval-case остается включенным.
- Litres success через generic runner по-прежнему требует строгий Litres verifier.
- Tests больше не ожидают, что Litres purchase script пропускает generic runner.

### Implementation steps

- [ ] **Step 1: Write a failing runtime test for Litres generic path**

Add a test in `buyer/tests/test_cdp_recovery.py` asserting that a Litres session reaches generic runner when the purchase registry has no Litres script. Use a fake generic runner that returns a valid Litres `AgentOutput` with:

- `status='completed'`
- `order_id='order-789'`
- `payment_evidence.source='litres_payecom_iframe'`
- `payment_evidence.url='https://payecom.ru/pay_ru?orderId=order-789'`

Expected result:

- generic runner was called once;
- `payment_ready` was emitted once;
- `scenario_finished.status == 'completed'`.

- [ ] **Step 2: Run the focused failing test**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_cdp_recovery.py::CDPRecoveryTests::test_litres_uses_generic_runner_with_default_purchase_settings -q
```

Expected during red phase before implementation: FAIL because the old Litres purchase script path could short-circuit generic runner.

- [ ] **Step 3: Remove Litres registry entry and default allowlist**

Change `buyer/app/purchase_scripts.py` so `_registry` no longer maps `litres.ru`.

Change `buyer/app/settings.py` default `purchase_script_allowlist` from `litres.ru` to empty string or empty set semantics. The desired behavior is: no domain has scripts-first purchase behavior by default.

Change both compose files so `PURCHASE_SCRIPT_ALLOWLIST` is absent or empty. If env remains, set it to an empty value and document that empty means no scripts-first purchase domains.

- [ ] **Step 4: Delete the Litres purchase script**

Delete `buyer/scripts/purchase/litres.ts`.

Remove TS helper smoke tests that import helpers from this file. Keep tests for generic prompt Litres rules and payment verifier.

- [ ] **Step 5: Rework script runner tests**

Keep `buyer/tests/test_script_runtime.py` for generic `PurchaseScriptRunner` behavior if the runner remains useful for future scripts. Remove assumptions that Litres is registered.

Tests should still cover:

- nonzero script process result is not success;
- stale output is rejected;
- missing/unpublished script returns failed diagnostic result when explicitly requested for a registered fixture domain.

- [ ] **Step 6: Update docs**

Update `docs/litres-brandshop-agent-flow.md`:

- Litres table: purchase script `Нет`;
- Litres step-by-step: after SberId auth, generic loop starts;
- remove script-first sequence;
- keep Litres verifier and PayEcom evidence contract;
- tests section should point to generic-runner and verifier coverage.

Update `docs/repository-map.md`:

- remove Litres purchase script from actual structure;
- clarify that `PurchaseScriptRunner` is infrastructure, not active for Litres by default.

- [ ] **Step 7: Verify**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_cdp_recovery.py buyer/tests/test_observability_and_cdp_tool.py buyer/tests/test_script_runtime.py -q
```

Expected: PASS.

Run:

```bash
rg -n "purchase/litres|Litres purchase-скрипт|Litres purchase script|PURCHASE_SCRIPT_ALLOWLIST=.*litres" buyer docs docker-compose.yml docker-compose.openclaw.yml
```

Expected: no current-behavior references.

## Task 2: SberId auth scripts сначала проверяют текущую авторизацию

### Требование

Каждый SberId auth script должен быть идемпотентным. После применения cookies/storageState и открытия домена script обязан сначала проверить, не находится ли пользователь уже в авторизованном состоянии. Если авторизация уже подтверждена, script возвращает success без клика по login/Sber ID и без повторного захода на `id.sber.ru`.

### Files

- Modify: `buyer/scripts/sberid/litres.ts`
- Modify: `buyer/scripts/sberid/brandshop.ts`
- Modify: `buyer/app/auth_scripts.py` only if result metadata contract changes
- Modify: `buyer/tests/test_observability_and_cdp_tool.py`
- Modify: `docs/litres-brandshop-agent-flow.md`
- Modify: `docs/repository-map.md`

### Acceptance criteria

- Litres auth script checks current auth state before searching login button.
- Brandshop auth script checks current auth state before searching profile/login/Sber ID entrypoint.
- If already authenticated, result contains a clear sanitized diagnostic marker, for example `already_authenticated: true` in script metadata/artifacts or message.
- Already-authenticated path does not navigate to `id.sber.ru`.
- Already-authenticated path does not click Sber ID.
- If not authenticated, existing SberId flow still runs.
- Brandshop auth check is stronger than current host-return heuristic and must use account/profile/logout/user markers.

### Domain-specific auth signals

Litres accepted signals:

- `/me/profile/` or current page contains authenticated profile markers;
- text markers include stable account/profile/book ownership signals such as “Мои книги” or “Профиль”;
- login form is absent.

Brandshop accepted signals:

- account/profile page or account flyout indicates an authenticated user;
- logout/profile/account markers are visible;
- login/Sber ID form is absent.

Brandshop must not accept “returned to brandshop.ru after Sber loop” as the only success evidence.

### Implementation steps

- [ ] **Step 1: Add Litres helper tests**

In `buyer/tests/test_observability_and_cdp_tool.py`, extend the Litres auth helper smoke test to cover:

- authenticated page text returns `auth_ok`;
- login form text returns not authenticated;
- callback page without profile markers returns not authenticated.

- [ ] **Step 2: Add Brandshop helper tests**

Add Brandshop helper smoke coverage for:

- account/logout/profile marker returns authenticated;
- Sber ID/login form returns not authenticated;
- plain home page without profile marker returns not authenticated.

- [ ] **Step 3: Implement Litres precheck**

In `buyer/scripts/sberid/litres.ts`, after storageState/cookies are applied and before login-button discovery:

- open a safe Litres page;
- call existing verification helper against current page, profile page, and return URL;
- if verification passes, return success with already-authenticated diagnostic;
- skip login/Sber ID clicks.

- [ ] **Step 4: Implement Brandshop precheck**

In `buyer/scripts/sberid/brandshop.ts`, after cookies are applied and before Sber ID entrypoint discovery:

- open Brandshop home or account page;
- close overlays;
- check profile/account/logout markers;
- if confirmed, return success with already-authenticated diagnostic;
- skip Sber ID clicks.

- [ ] **Step 5: Preserve non-authenticated path**

Run auth helper tests that prove a non-authenticated page still enters the existing login flow. Add a source-order regression test that verifies the `already_authenticated` branch is checked before `page.goto(entryUrl)` and before any `sberIdTargets()` click attempt.

- [ ] **Step 6: Verify**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_observability_and_cdp_tool.py buyer/tests/test_sberid_auth_idempotency.py -q
```

Expected: PASS.

Run:

```bash
rg -n "already_authenticated|context_prepared_for_reuse|id.sber.ru|Мои книги|Профиль|logout|account" buyer/scripts/sberid buyer/tests/test_observability_and_cdp_tool.py
```

Expected: matches show explicit already-authenticated checks before login-flow actions.

## Task 3: Brandshop Domain-specific payment verifier

### Требование

Brandshop должен получить собственный verifier, который позволяет `payment_ready` только при подтвержденном SberPay evidence для `brandshop.ru`. Generic-agent может выполнять покупку, но не может объявить успех без Brandshop verifier.

### Files

- Modify: `buyer/app/payment_verifier.py`
- Modify: `buyer/app/models.py`
- Modify: `buyer/app/codex_output_schema.json`
- Modify: `buyer/app/prompt_builder.py`
- Modify: `buyer/tests/test_cdp_recovery.py`
- Modify: `buyer/tests/test_observability_and_cdp_tool.py`
- Modify: `eval/cases/brandshop_purchase_smoke.yaml`
- Modify: `docs/callbacks.openapi.yaml`
- Modify: `docs/litres-brandshop-agent-flow.md`
- Modify: `docs/repository-map.md`

### Evidence contract

Brandshop verifier must define a separate `PaymentEvidence.source`.

Recommended source name:

- `brandshop_yoomoney_sberpay_redirect`

`PaymentEvidence` fields:

- `source='brandshop_yoomoney_sberpay_redirect'`;
- `url`: exact URL where `orderId` was found.

Correlated fields outside `PaymentEvidence`:

- top-level `AgentOutput.order_id`: matched against the evidence URL `orderId`;
- `PaymentVerificationResult.order_id_host`: host parsed from the accepted evidence URL and later emitted in `payment_ready.order_id_host`.

Accepted evidence URL must be explicit:

- scheme: `https`;
- host: exactly `yoomoney.ru`;
- path: exactly `/checkout/payments/v2/contract`;
- query: exactly one non-empty `orderId` value used as the returned `order_id`.

### Rejection rules

Verifier must reject:

- missing `order_id`;
- missing `payment_evidence`;
- evidence source not equal to `brandshop_yoomoney_sberpay_redirect`;
- non-HTTPS payment URL;
- host not exactly `yoomoney.ru`;
- path not exactly `/checkout/payments/v2/contract`;
- path-prefix attacks;
- duplicate `orderId`;
- empty `orderId`;
- mismatch between `AgentOutput.order_id` and evidence URL `orderId`;
- SBP/FPS/СБП-only evidence;
- any domain not explicitly supported.

### Eval requirement

`eval/cases/brandshop_purchase_smoke.yaml` в текущей реализации включен (`enabled: true`):

- keep `auth_profile: brandshop_sberid`;
- expected outcome remains `payment_ready`;
- forbidden actions still include final payment confirmation.

### Implementation steps

- [ ] **Step 1: Capture or add sanitized Brandshop payment evidence fixture**

Create `buyer/tests/fixtures/brandshop_payment_evidence.json` or an equivalent inline fixture with sanitized synthetic YooMoney redirect evidence:

- `payment_evidence.source`: `brandshop_yoomoney_sberpay_redirect`;
- `payment_evidence.url`: `https://yoomoney.ru/checkout/payments/v2/contract?orderId=brandshop-order-123`;
- top-level `order_id`: `brandshop-order-123`;
- expected verifier `order_id_host`: `yoomoney.ru`.

Do not commit cookies, names, phone numbers, addresses, cards or real order data.

- [ ] **Step 2: Add failing verifier tests**

In `buyer/tests/test_cdp_recovery.py`, add tests:

- Brandshop accepted evidence emits `payment_ready`;
- unsupported host rejected;
- HTTP URL rejected;
- duplicate `orderId` rejected;
- path-prefix attack rejected;
- source mismatch rejected;
- order id mismatch rejected.

- [ ] **Step 3: Extend models and schema**

Update `PaymentEvidence.source` in `buyer/app/models.py` and `buyer/app/codex_output_schema.json` to include `brandshop_yoomoney_sberpay_redirect`.

Do not make Brandshop use `litres_payecom_iframe`.

- [ ] **Step 4: Implement verifier**

Add Brandshop domain detection to `buyer/app/payment_verifier.py`.

Add a Brandshop parser equivalent in strictness to `payecom_order_id_from_url`, but with exact YooMoney redirect rules:

- `https`;
- `yoomoney.ru`;
- `/checkout/payments/v2/contract`;
- exactly one non-empty `orderId`.

The verifier result must expose `order_id_host` for Task 5. Do not add `order_id` or `order_id_host` to `PaymentEvidence`; the model/schema intentionally carry only `source` and `url`.

- [ ] **Step 5: Update prompt**

In `buyer/app/prompt_builder.py`, add Brandshop-specific completion instruction:

- return `payment_evidence.source='brandshop_yoomoney_sberpay_redirect'`;
- evidence URL must be the exact YooMoney SberPay redirect URL containing the same `orderId`;
- SBP/FPS/СБП remains failure or `needs_user_input`, not success.

- [ ] **Step 6: Enable eval case**

Set `eval/cases/brandshop_purchase_smoke.yaml` to `enabled: true`. Current implemented state keeps Brandshop eval enabled.

- [ ] **Step 7: Verify**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_cdp_recovery.py buyer/tests/test_observability_and_cdp_tool.py -q
```

Expected: PASS.

Run:

```bash
rg -n "brandshop_yoomoney_sberpay_redirect|yoomoney.ru|brandshop_purchase_smoke|enabled: true" buyer eval docs
```

Expected: verifier, schema, prompt, eval and docs all reference the new Brandshop contract.

## Task 4: Убрать лишние trace из callbacks

### Требование

Callbacks не должны переносить большие или чувствительные trace-фрагменты. Полные prompt/stdout/stderr/browser action logs должны оставаться в trace-файлах на диске, а callback должен передавать только минимальный summary, достаточный для UI, eval orchestration и диагностики.

### Files

- Modify: `buyer/app/runner.py`
- Modify: `buyer/app/service.py`
- Modify: `buyer/app/models.py`
- Modify: `docs/callbacks.openapi.yaml`
- Modify: `eval_service/app/callbacks.py`
- Modify: `eval_service/app/trace_collector.py`
- Modify: `micro-ui/app/store.py`
- Modify: `micro-ui/app/static/app.js`
- Modify: `buyer/tests/test_observability_and_cdp_tool.py`
- Modify: `eval_service/tests/*`
- Modify: `docs/litres-brandshop-agent-flow.md`
- Modify: `docs/repository-map.md`

### Minimal callback trace contract

`agent_step_finished.payload.trace` should keep only:

- `trace_id` or deterministic step trace identifier;
- `step`;
- `status`;
- `model`;
- `duration_ms`;
- `attempts`;
- `prompt_sha256`;
- `browser_action_count`;
- `error_code` if present;
- `trace_artifact_ref` or path reference usable by local eval trace collector.

Remove from callback payload:

- prompt preview;
- stdout tail;
- stderr tail;
- browser actions tail;
- raw browser action items;
- HTML/text excerpts;
- screenshots as inline payload;
- command-level timing arrays;
- payment URL excerpts beyond verifier-approved fields.

Full trace files may still contain diagnostic detail if existing sanitation rules allow it.

### Acceptance criteria

- `agent_stream_event` remains best-effort.
- `agent_step_finished` callback payload is small and deterministic.
- Eval still collects full trace from trace files, not callback payload.
- Micro UI still shows status, step number, summary and a trace reference.
- Callback OpenAPI matches runtime payload.
- Tests assert that prompt/stdout/stderr/browser action tails are absent from callbacks.

### Implementation steps

- [ ] **Step 1: Add failing callback payload tests**

In `buyer/tests/test_observability_and_cdp_tool.py`, add tests asserting:

- `agent_step_finished.payload.trace.prompt_preview` absent;
- `stdout_tail` absent;
- `stderr_tail` absent;
- `browser_actions_tail` absent;
- `browser_action_metrics.command_durations` absent;
- `trace_artifact_ref` or equivalent retained.

- [ ] **Step 2: Slim trace summary creation**

In `buyer/app/runner.py`, split internal full trace from callback-safe trace summary:

- full trace continues to be written to `step-XXX-trace.json`;
- callback-safe summary is a separate method or model with only minimal fields.

- [ ] **Step 3: Update callback delivery**

In `buyer/app/service.py`, pass only callback-safe trace summary into `agent_step_finished`.

Keep full trace artifact paths in session artifacts when the existing artifact contract already stores local trace references.

- [ ] **Step 4: Update schemas and consumers**

Update `docs/callbacks.openapi.yaml` `TraceSummary`.

Update `eval_service` so judge input still gets full trace through `trace_collector.py`.

Update `micro-ui` to render missing old fields gracefully and display trace reference instead of raw trace tails.

- [ ] **Step 5: Verify**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with-requirements eval_service/requirements.txt --with pytest pytest buyer/tests/test_observability_and_cdp_tool.py eval_service/tests -q
```

Expected: PASS.

Run:

```bash
rg -n "prompt_preview|stdout_tail|stderr_tail|browser_actions_tail|command_durations" buyer/app docs/callbacks.openapi.yaml micro-ui eval_service
```

Expected: old fields do not appear in callback schema/runtime payload. They may appear only in full trace writer code or historical docs.

## Task 5: `payment_ready` передает хост, на котором найден `orderId`

### Требование

`payment_ready` payload должен дополнительно передавать host источника `orderId`. Это поле нужно middle/eval/UI для диагностики и для подтверждения, что `orderId` найден на verifier-approved платежном host, а не на случайной странице.

### Contract decision

Use field name:

- `order_id_host`

Payload:

```json
{
  "order_id": "order-123",
  "order_id_host": "payecom.ru",
  "message": "..."
}
```

For Litres expected value is `payecom.ru`.

For Brandshop expected value is the host accepted by Brandshop verifier.

If verifier cannot produce `order_id_host`, `buyer` must not emit `payment_ready`.

### Files

- Modify: `buyer/app/models.py`
- Modify: `buyer/app/payment_verifier.py`
- Modify: `buyer/app/service.py`
- Modify: `docs/callbacks.openapi.yaml`
- Modify: `docs/openapi.yaml` only if session detail models expose event payload examples
- Modify: `eval_service/app/callbacks.py`
- Modify: `eval_service/app/models.py`
- Modify: `micro-ui/app/store.py`
- Modify: `micro-ui/app/static/app.js`
- Modify: `buyer/tests/test_cdp_recovery.py`
- Modify: `eval_service/tests/*`
- Modify: `docs/litres-brandshop-agent-flow.md`
- Modify: `docs/repository-map.md`

### Acceptance criteria

- Every `payment_ready` emitted by `BuyerService` contains non-empty `order_id_host`.
- Litres success emits `order_id_host='payecom.ru'`.
- Eval callback receiver rejects `payment_ready` without `order_id_host`.
- Micro UI displays `order_id_host` near `order_id`.
- Micro UI tests assert that `order_id_host` is preserved in session summary.
- `scenario_finished.artifacts.payment_evidence` remains available for deeper debugging.
- OpenAPI callback schema marks `order_id_host` as required.

### Implementation steps

- [ ] **Step 1: Add failing tests for Litres payment_ready host**

In `buyer/tests/test_cdp_recovery.py`, extend Litres success tests:

- payment_ready payload has `order_id`;
- payment_ready payload has `order_id_host == 'payecom.ru'`;
- payment_ready payload has `message`.

Add a negative test where verifier accepts no host. Expected: no `payment_ready`, final status failed.

- [ ] **Step 2: Extend verifier result**

In `buyer/app/payment_verifier.py`, extend `PaymentVerificationResult` with:

- `order_id_host: str | None`

Litres verifier sets it from parsed evidence URL host.

Brandshop verifier from Task 3 sets it from Brandshop evidence URL host.

- [ ] **Step 3: Emit field in service**

In `BuyerService._handle_completed`, include `order_id_host` from accepted verification in `payment_ready` payload.

Do not parse host again in service. The verifier owns host extraction and acceptance.

- [ ] **Step 4: Update callback schema and receivers**

Update `docs/callbacks.openapi.yaml` `PaymentReadyPayload`:

- add required `order_id_host`;
- document that it is verifier-approved source host.

Update `eval_service/app/callbacks.py` validation to require non-empty `order_id_host`.

Update micro-ui event rendering.

- [ ] **Step 5: Verify**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with-requirements eval_service/requirements.txt --with pytest pytest buyer/tests/test_cdp_recovery.py eval_service/tests -q
```

Expected: PASS.

Run:

```bash
rg -n "order_id_host|PaymentReadyPayload|payment_ready" buyer docs eval_service micro-ui
```

Expected: runtime, schema, eval and UI all reference `order_id_host`.

## Task 6: Brandshop generic-agent playbook для ручного пути Jordan Air High 45 EU

### Требование

Описанный ручной путь Brandshop должен быть оформлен как параметризованный playbook/knowledge и prompt guidance для generic-agent, а не как TypeScript purchase script и не как hardcoded переход на конкретный SKU.

Целевой пример пользователя:

```text
купи светлые кроссовки Jordan Air High 45 EU
```

Observed successful manual path:

1. Auth script готовит авторизованную сессию и возвращает браузер на `https://brandshop.ru/`.
2. Generic-agent нажимает header search button с `aria-label="search"`.
3. Вводит в search input с placeholder `Искать в каталоге` запрос `Jordan Air High`.
4. Нажимает Enter.
5. Попадает на search page вида `https://brandshop.ru/search/?st=Jordan+Air+High`.
6. Выбирает фильтр размера `45 EU`; URL может стать похожим на `https://brandshop.ru/search/?st=Jordan+Air+High&mfp=13o-razmer[45+EU]`.
7. Сравнивает результаты и выбирает светлый или бежевый вариант, а не черный.
8. Открывает product page выбранного варианта.
9. Выбирает размер `45 EU`.
10. Нажимает `Добавить в корзину`.
11. Открывает cart и проверяет, что в корзине ровно один товар: Jordan, кроссовки Air Jordan 1 Retro High OG, размер `45 EU`, количество `1`.
12. Нажимает `Оформить заказ`.
13. На checkout проверяет адрес доставки.
14. Выбирает radio payment method `SberPay`.
15. Нажимает `Подтвердить заказ`.
16. После redirect на `https://yoomoney.ru/checkout/payments/v2/contract?orderId=...` останавливается, извлекает `orderId` и не нажимает ничего дальше.

### Non-goals

- Не создавать `buyer/scripts/purchase/brandshop.ts`.
- Не hardcode-ить `/goods/510194/ih4363-100/`.
- Не hardcode-ить query fragment `mfp=13o-razmer[45+EU]` как единственный способ фильтрации.
- Не hardcode-ить конкретный `orderId`.
- Не выбирать “Заказ в один клик”.
- Не выполнять действия на YooMoney после появления payment contract URL.

### Files

- Modify: `buyer/app/prompt_builder.py`
- Modify: `buyer/tools/cdp_tool.py` if snapshot hints need broader Brandshop coverage
- Modify: `buyer/tests/test_observability_and_cdp_tool.py`
- Modify: `eval/cases/brandshop_purchase_smoke.yaml`
- Modify: `docs/litres-brandshop-agent-flow.md`
- Modify: `docs/repository-map.md`
- Optional create: `docs/superpowers/specs/2026-04-30-brandshop-generic-playbook.md`

### Prompt requirements

Add a Brandshop-specific block to generic buyer prompt:

- Use UI search first: open search button, fill catalog search input, press Enter.
- Build search text from task product identity only: brand/model/category go into search; size and color are constraints for filtering/ranking.
- Treat `45 EU` as a required size constraint, not as free text that can be ignored.
- Use filters or confirmed page controls for size selection. A URL with `mfp=...45+EU...` may be used only after it was reached through UI or confirmed from page links/state.
- Interpret “светлые” as light/beige/white preference. If the page data cannot distinguish light/beige from black using text, links, image alt, snapshot or screenshot, return `needs_user_input`.
- Before `Добавить в корзину`, verify product brand/model/category, color preference and selected size.
- After adding to cart, open cart and verify exactly one cart item, matching product and size `45 EU`, quantity `1`.
- On checkout, verify an existing delivery address. If address is absent, ambiguous or requires selection/editing, return `needs_user_input`.
- Select only SberPay. SBP/FPS/СБП is not a substitute.
- On Brandshop, pressing `Подтвердить заказ` is allowed only after SberPay is explicitly selected and only to create the external payment session. It is not allowed to continue payment on YooMoney.
- Stop immediately on `https://yoomoney.ru/checkout/payments/v2/contract?orderId=...`.
- Return `payment_evidence.source='brandshop_yoomoney_sberpay_redirect'`, evidence URL and matching `order_id`.

### CDP snapshot requirements

Current CDP commands are enough for a first implementation: `snapshot`, `links`, `text`, `attr`, `click`, `fill`, `press`, `url`.

If generic-agent misses Brandshop controls, extend snapshot classification so it surfaces:

- header search button;
- search input;
- filter controls;
- size filter values;
- product cards and product links;
- product size plates;
- add-to-cart button;
- cart item title/subtitle/size/quantity;
- checkout address;
- SberPay radio;
- confirm order button.

### Tests

- [ ] **Step 1: Add prompt coverage test**

In `buyer/tests/test_observability_and_cdp_tool.py`, assert the built prompt contains Brandshop playbook requirements:

- search button/search input/Enter;
- size `45 EU` as filter/constraint;
- light/beige choice;
- one cart item verification;
- checkout address verification;
- SberPay radio selection;
- `Подтвердить заказ` allowed only to create external payment session;
- YooMoney evidence source `brandshop_yoomoney_sberpay_redirect`;
- stop after YooMoney contract URL.

- [ ] **Step 2: Add snapshot coverage if needed**

If `buyer/tools/cdp_tool.py` changes, add tests proving snapshot exposes Brandshop controls without requiring full HTML fallback.

- [ ] **Step 3: Update eval case**

Update `eval/cases/brandshop_purchase_smoke.yaml` variant to match the manual path:

- product category: `кроссовки`;
- brand: `Jordan`;
- model/search: `Air High`;
- size: `45 EU`;
- color preference: `светлые`.

Keep `enabled: true`; Brandshop verifier and `order_id_host` are part of the implemented contract.

- [ ] **Step 4: Verify**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_observability_and_cdp_tool.py -q
```

Expected: PASS.

Run:

```bash
rg -n "Jordan Air High|45 EU|brandshop_yoomoney_sberpay_redirect|yoomoney.ru|Подтвердить заказ|Искать в каталоге" buyer docs eval
```

Expected: prompt/tests/docs/eval contain the Brandshop generic playbook; no `buyer/scripts/purchase/brandshop.ts` exists.

## Cross-task verification

After all tasks are implemented, run:

```bash
uv run --with-requirements buyer/requirements.txt --with-requirements eval_service/requirements.txt --with pytest pytest buyer/tests/test_cdp_recovery.py buyer/tests/test_observability_and_cdp_tool.py buyer/tests/test_external_auth.py buyer/tests/test_script_runtime.py eval_service/tests -q
```

Expected: PASS.

Include the new focused regression tests in final verification:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_payment_verifier_and_ready.py buyer/tests/test_purchase_script_registry.py buyer/tests/test_sberid_auth_idempotency.py buyer/tests/test_brandshop_generic_playbook.py buyer/tests/test_callback_trace_slimming.py -q
```

Expected: PASS.

Run:

```bash
uv run --with-requirements micro-ui/requirements.txt --with pytest pytest micro-ui/tests -q
```

Expected: PASS.

Run a callback-contract scoped search:

```bash
rg -n "payment_method=SberPay|payment_url|prompt_preview|stdout_tail|stderr_tail|browser_actions_tail|command_durations" docs/callbacks.openapi.yaml eval_service/app/callbacks.py micro-ui/app buyer/app/service.py
```

Expected:

- no old `payment_ready` assumptions about `payment_method` or `payment_url`;
- no removed trace fields in callback schema or consumers;
- matches in `buyer/app/service.py` are allowed only where the service explicitly removes/sanitizes full trace fields before callbacks.

Run a script-registry scoped search:

```bash
rg -n "purchase/litres|BUYER_PURCHASE_SCRIPT_ALLOWLIST|PURCHASE_SCRIPT_ALLOWLIST=.*litres" buyer docs eval_service micro-ui docker-compose.yml docker-compose.openclaw.yml
```

Expected:

- no old env name `BUYER_PURCHASE_SCRIPT_ALLOWLIST`;
- no active Litres purchase script registry/config references;
- docs may mention `buyer/scripts/purchase/litres.ts` only as deleted historical behavior.

Run:

```bash
rg -n "order_id_host|brandshop_yoomoney_sberpay_redirect|already_authenticated|PURCHASE_SCRIPT_ALLOWLIST|litres_payecom_iframe|Jordan Air High|45 EU" buyer docs eval micro-ui
```

Expected:

- `order_id_host` present in callback contract and consumers;
- Brandshop verifier contract present;
- Brandshop generic playbook present without hardcoded SKU;
- SberId auth idempotency markers present;
- Litres verifier evidence still present;
- `PURCHASE_SCRIPT_ALLOWLIST` no longer enables Litres by default.

## Documentation requirements

Update these docs in the same implementation branch:

- `docs/litres-brandshop-agent-flow.md`
- `docs/repository-map.md`
- `docs/callbacks.openapi.yaml`
- `docs/openapi.yaml` if examples include callback event payloads
- `docs/buyer-roadmap.md` if this changes roadmap priority/status
- `docs/architecture-decisions.md` if the team decides that generic-first purchase, slim callbacks, or `order_id_host` is an architectural decision rather than only task scope

## Completion checklist

- [ ] Linear issues created or updated with links to implementation PR. Not completed in this workspace: no Linear CLI/MCP is configured.
- [x] Litres purchase script deleted and no active docs claim it exists.
- [x] Litres purchase works only through generic Codex-agent.
- [x] Litres verifier still enforces strict PayEcom evidence.
- [x] SberId auth scripts skip login when already authenticated.
- [x] Brandshop verifier accepts only Brandshop-approved SberPay evidence.
- [x] Brandshop generic-agent playbook covers the Jordan Air High 45 EU manual path without hardcoded SKU or purchase script.
- [x] Brandshop eval case enabled (`enabled: true`).
- [x] `payment_ready` includes `order_id_host`.
- [x] Callback trace payload is slim; full trace remains available through trace files.
- [x] Focused buyer/eval tests pass.
- [x] Docs and repository map reflect final behavior.
