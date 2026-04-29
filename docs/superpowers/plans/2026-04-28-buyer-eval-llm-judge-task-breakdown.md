# Buyer Eval LLM Judge Task Breakdown

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Разбить разработку eval-контура `buyer` на небольшие независимые work packages с явными зависимостями и параллельными волнами.

**Architecture:** MVP строится вокруг отдельного `eval_service` на Python + FastAPI. `eval_service` читает `eval/cases/*.yaml`, запускает batch через обычный API `buyer`, принимает callbacks, строит redacted judge input, запускает `codex exec` judge и отдает REST API для eval-таба `micro-ui`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest, YAML, filesystem artifacts, Codex CLI, vanilla JS/CSS в `micro-ui`.

---

## Scope

Этот документ является картой декомпозиции и параллельной разработки. Он фиксирует границы ownership, зависимости и порядок волн. Детальные task-by-task implementation plans можно писать отдельно для отдельных work packages перед непосредственной реализацией.

Источник дизайна: `docs/superpowers/specs/2026-04-28-buyer-eval-llm-judge-design.md`.

Linear: [MON-28](https://linear.app/monaco-dev/issue/MON-28/buyer-phase-2-llm-judge-eval-loop-i-dashboard).

## Dependency Tree

```text
Eval MVP
├─ 0. Базовый контракт и каркас [serial blocker]
│  ├─ 0.1 eval_service skeleton: FastAPI, settings, health, tests
│  ├─ 0.2 Pydantic-модели: cases, runs, callbacks, evaluations
│  ├─ 0.3 JSON schema для evaluation.json
│  └─ 0.4 docker-compose + env для eval_service
│
├─ 1. Case registry [parallel after 0.2]
│  ├─ YAML parser для eval/cases/*.yaml
│  ├─ template rendering + explicit variants
│  ├─ validation eval_case_id/case_version
│  └─ smoke YAML для litres.ru и brandshop.ru
│
├─ 2. Run storage и artifacts [parallel after 0.2]
│  ├─ filesystem layout eval/runs/<eval_run_id>/
│  ├─ manifest.json read/write
│  ├─ per-case state persistence
│  └─ summary.json writer
│
├─ 3. Buyer integration [parallel after 0.2]
│  ├─ buyer_client: create task, get session, send reply
│  ├─ auth_profile loader из /run/eval/auth-profiles
│  ├─ skipped_auth_missing handling
│  └─ fake buyer client tests
│
├─ 4. Callback receiver и ask_user flow [depends 2, 3]
│  ├─ POST /callbacks/buyer
│  ├─ event persistence в run manifest/state
│  ├─ payment_ready/scenario_finished detection
│  ├─ waiting_user state
│  └─ reply proxy endpoint для micro-ui
│
├─ 5. Sequential run orchestrator [depends 1, 2, 3, 4]
│  ├─ POST /runs
│  ├─ последовательный запуск selected cases
│  ├─ timeout 600s
│  ├─ payment_ready grace period 5s
│  └─ run/case lifecycle states
│
├─ 6. Judge input pipeline [parallel after 0.2]
│  ├─ trace-dir discovery по BUYER_TRACE_DIR/session_id
│  ├─ step trace summary collector
│  ├─ browser actions summary/tails
│  ├─ sanitizer/redaction
│  └─ judge-input.json writer
│
├─ 7. Judge runner [depends 6 + evaluation schema]
│  ├─ prompt builder
│  ├─ codex exec --output-schema command
│  ├─ judge_skipped/judge_failed handling
│  └─ evaluation.json validation
│
├─ 8. Aggregation и baseline [depends 2, 7]
│  ├─ checks ok/not_ok/skipped aggregation
│  ├─ duration/tokens medians
│  ├─ baseline median over last N successful evaluations
│  └─ dashboard data builders
│
├─ 9. eval_service REST API [can start after 0, finishes after 5/7/8]
│  ├─ GET /cases
│  ├─ POST /runs
│  ├─ GET /runs, GET /runs/{id}
│  ├─ POST /runs/{id}/judge
│  ├─ POST /runs/{id}/cases/{case_id}/reply
│  ├─ GET /dashboard/cases
│  └─ GET /dashboard/hosts
│
├─ 10. micro-ui Eval tab [parallel after API contract from 0/9 stub]
│  ├─ tab shell + routing
│  ├─ case checkbox list + Start run
│  ├─ run detail + ask_user reply form
│  ├─ Run judge button
│  ├─ evaluations table
│  └─ case/host charts for duration/tokens
│
└─ 11. Integration/docs [mostly final]
   ├─ fake-buyer integration test
   ├─ README/env docs
   ├─ docker smoke check
   └─ final fixture/unit test pass
```

## Parallel Waves

```text
Wave 0:
  0. Базовый контракт и каркас

Wave 1, параллельно:
  1. Case registry
  2. Run storage и artifacts
  3. Buyer integration
  6. Judge input pipeline
  10. micro-ui Eval tab shell по mock/stub API

Wave 2, параллельно:
  4. Callback receiver и ask_user flow
  7. Judge runner
  8. Aggregation и baseline
  10. micro-ui run detail/table/charts

Wave 3:
  5. Sequential run orchestrator
  9. eval_service REST API

Wave 4:
  10. Финальная связка micro-ui с реальным eval_service API
  11. Integration/docs
```

## Critical Path

```text
0 -> (1,2,3) -> 4 -> 5 -> 9 -> 10 -> 11
```

`6 -> 7 -> 8 -> 9` является вторым важным путем, но его можно вести параллельно с orchestration path.

## Work Packages

### 0. Базовый контракт и каркас

**Ownership:** `eval_service`, docker wiring.

**Primary files:**

- Create: `eval_service/app/__init__.py`
- Create: `eval_service/app/main.py`
- Create: `eval_service/app/settings.py`
- Create: `eval_service/app/models.py`
- Create: `eval_service/app/evaluation_schema.json`
- Create: `eval_service/requirements.txt`
- Create: `eval_service/Dockerfile`
- Modify: `docker-compose.yml`

**Output:**

- FastAPI app with health endpoint.
- Settings for `BUYER_API_BASE_URL`, `BUYER_TRACE_DIR`, `EVAL_RUNS_DIR`, `EVAL_CASES_DIR`, `EVAL_AUTH_PROFILES_DIR`, `EVAL_JUDGE_MODEL`, `EVAL_BASELINE_WINDOW`.
- Shared Pydantic contracts for cases, runs, callbacks, evaluations.
- JSON schema for strict `evaluation.json`.
- Docker service can start without external buyer dependency.

**Can run in parallel with:** none. This is the serial blocker.

### 1. Case registry

**Ownership:** case parsing and template expansion only.

**Primary files:**

- Create: `eval_service/app/case_registry.py`
- Create: `eval_service/tests/test_case_registry.py`
- Create: `eval/cases/litres_purchase_book.yaml`
- Create: `eval/cases/brandshop_purchase_smoke.yaml`

**Output:**

- Load all `eval/cases/*.yaml`.
- Validate one template per file.
- Render `task_template` and `start_url_template` from explicit variant variables.
- Reject missing or duplicate `eval_case_id`.
- Expose concrete cases with `eval_case_id`, `case_version`, `variant_id`, `host`, `task`, `start_url`, `metadata`, `auth_profile`, `expected_outcome`, `forbidden_actions`, `rubric`.

**Depends on:** 0.2 models.

**Can run in parallel with:** 2, 3, 6, initial 10.

### 2. Run storage и artifacts

**Ownership:** filesystem state, no HTTP orchestration.

**Primary files:**

- Create: `eval_service/app/run_store.py`
- Create: `eval_service/tests/test_run_store.py`

**Output:**

- Create `eval/runs/<eval_run_id>/`.
- Read/write `manifest.json`.
- Track per-case state, session id, callback events, errors, timings and artifact paths.
- Write `summary.json` from supplied aggregate data.
- Atomic-ish writes through temp file + replace to avoid corrupt partial JSON.

**Depends on:** 0.2 models.

**Can run in parallel with:** 1, 3, 6, initial 10.

### 3. Buyer integration

**Ownership:** communication with `buyer` and auth profile loading.

**Primary files:**

- Create: `eval_service/app/buyer_client.py`
- Create: `eval_service/app/auth_profiles.py`
- Create: `eval_service/tests/test_buyer_client.py`
- Create: `eval_service/tests/test_auth_profiles.py`

**Output:**

- `create_task` calls `POST /v1/tasks` with task, start_url, metadata, callback_url and inline `auth.storageState`.
- `get_session` calls `GET /v1/sessions/{session_id}`.
- `send_reply` calls `POST /v1/replies`.
- Auth loader reads `/run/eval/auth-profiles/<auth_profile>.json`.
- Missing/invalid auth profile returns a structured `skipped_auth_missing` reason for orchestrator.

**Depends on:** 0.2 models.

**Can run in parallel with:** 1, 2, 6, initial 10.

### 4. Callback receiver и ask_user flow

**Ownership:** callback handling and reply routing state.

**Primary files:**

- Create: `eval_service/app/callbacks.py`
- Create: `eval_service/tests/test_callbacks.py`
- Modify: `eval_service/app/main.py`

**Output:**

- `POST /callbacks/buyer` accepts buyer callback envelope.
- Callback events are stored in run manifest/state.
- `ask_user` changes case state to `waiting_user` and records `reply_id`.
- `payment_ready` changes case state to `payment_ready`.
- `scenario_finished` changes case state to `finished`.
- Reply endpoint receives operator answer and calls `buyer_client.send_reply`.

**Depends on:** 2, 3.

**Can run in parallel with:** 7, 8, micro-ui detail work.

### 5. Sequential run orchestrator

**Ownership:** case execution loop and lifecycle.

**Primary files:**

- Create: `eval_service/app/orchestrator.py`
- Create: `eval_service/tests/test_orchestrator.py`
- Modify: `eval_service/app/main.py`

**Output:**

- `POST /runs` creates `eval_run_id`.
- Selected cases execute sequentially.
- Each buyer task receives metadata: `eval_run_id`, `eval_case_id`, `case_version`, `host`, `case_title`, `variant_id`.
- Default timeout is 600 seconds.
- `payment_ready` waits 5 seconds grace period before closing case.
- Timeout case keeps partial state for judge.

**Depends on:** 1, 2, 3, 4.

**Can run in parallel with:** late 7/8 if interfaces are stable.

### 6. Judge input pipeline

**Ownership:** offline trace collection and safe redaction.

**Primary files:**

- Create: `eval_service/app/trace_collector.py`
- Create: `eval_service/app/redaction.py`
- Create: `eval_service/app/judge_input.py`
- Create: `eval_service/tests/test_trace_collector.py`
- Create: `eval_service/tests/test_redaction.py`
- Create: `eval_service/tests/fixtures/trace_session/`

**Output:**

- Find trace dir by `BUYER_TRACE_DIR/YYYY-MM-DD/HH-MM-SS/<session_id>`.
- Summarize `step-XXX-trace.json`.
- Summarize and tail `step-XXX-browser-actions.jsonl`.
- Include screenshot refs when present.
- Redact cookies, `storageState`, tokens, auth headers, API keys, `orderId`, payment/order ids and payment URLs.
- Write `<eval_case_id>.judge-input.json`.

**Depends on:** 0.2 models.

**Can run in parallel with:** 1, 2, 3, initial 10.

### 7. Judge runner

**Ownership:** LLM judge execution and validation.

**Primary files:**

- Create: `eval_service/app/judge_runner.py`
- Create: `eval_service/app/judge_prompt.py`
- Create: `eval_service/tests/test_judge_runner.py`
- Create: `eval_service/tests/test_judge_prompt.py`

**Output:**

- Build judge prompt from `judge-input.json`.
- Run `codex exec --output-schema eval_service/app/evaluation_schema.json`.
- Use `EVAL_JUDGE_MODEL`.
- Write `<eval_case_id>.evaluation.json`.
- Validate schema.
- Convert missing auth/timeout/no credentials/invalid JSON into `judge_skipped` or `judge_failed` without changing live outcome.

**Depends on:** 6 and evaluation schema from 0.3.

**Can run in parallel with:** 4, 8, micro-ui detail work.

### 8. Aggregation и baseline

**Ownership:** deterministic summaries and dashboard data, no LLM.

**Primary files:**

- Create: `eval_service/app/aggregation.py`
- Create: `eval_service/app/dashboard.py`
- Create: `eval_service/tests/test_aggregation.py`
- Create: `eval_service/tests/test_dashboard.py`

**Output:**

- Count `ok`, `not_ok`, `skipped` per check.
- Count skipped cases by reason.
- List not-ok cases.
- Count recommendations.
- Compute medians for `duration_ms` and `buyer_tokens_used`.
- Compute baseline as median of last N evaluations where `outcome_ok`, `safety_ok`, `payment_boundary_ok` are `ok`.
- Build dashboard payloads by `eval_case_id` and host.

**Depends on:** 2, 7 outputs.

**Can run in parallel with:** 4, late 7, micro-ui detail work.

### 9. eval_service REST API

**Ownership:** stable API surface for `micro-ui`.

**Primary files:**

- Create: `eval_service/app/api.py`
- Create: `eval_service/tests/test_api.py`
- Modify: `eval_service/app/main.py`

**Output:**

- `GET /cases`.
- `POST /runs`.
- `GET /runs`.
- `GET /runs/{eval_run_id}`.
- `POST /runs/{eval_run_id}/judge`.
- `POST /runs/{eval_run_id}/cases/{eval_case_id}/reply`.
- `GET /dashboard/cases`.
- `GET /dashboard/hosts`.
- `POST /callbacks/buyer`.

**Depends on:** 5, 7, 8 for full behavior. Route stubs can start after 0.

**Can run in parallel with:** micro-ui once response shapes are frozen.

### 10. micro-ui Eval tab

**Ownership:** UI only. Avoid changing buyer semantics.

**Primary files:**

- Modify: `micro-ui/app/templates/index.html`
- Modify: `micro-ui/app/static/app.js`
- Modify: `micro-ui/app/static/app.css`
- Optional create: `micro-ui/app/static/eval.js`
- Optional create: `micro-ui/app/static/eval.css`

**Output:**

- Eval tab in `micro-ui`.
- Case checkbox list.
- `Start run` action.
- Run detail with case statuses, session ids and callbacks.
- `ask_user` question display and reply form.
- `Run judge` action.
- Evaluations table.
- Dashboard by `eval_case_id`.
- Dashboard by host.
- Lightweight line charts for `duration_ms` and `buyer_tokens_used`.

**Depends on:** API contract from 9. Shell and mock UI can start after 0.

**Can run in parallel with:** 1, 2, 3, 6 initially; final integration waits for 9.

### 11. Integration/docs

**Ownership:** cross-service verification and docs.

**Primary files:**

- Modify: `README.md`
- Optional modify: `docs/buyer.md`
- Optional modify: `docs/openapi.yaml` only if public API changes are intentionally documented.
- Create: integration fixture tests under `eval_service/tests/`.

**Output:**

- Document env variables and mounted volumes.
- Document auth profile secret directory.
- Document manual eval run flow.
- Fake-buyer integration test covers run -> callback -> ask_user reply -> payment_ready -> judge.
- Docker compose smoke command documented.
- Final unit/fixture tests pass.

**Depends on:** 5, 7, 8, 9, 10.

## Conflict Map

These files are likely to create merge conflicts and should have one owner at a time:

- `docker-compose.yml`;
- `eval_service/app/main.py`;
- `micro-ui/app/static/app.js`;
- `micro-ui/app/static/app.css`;
- `micro-ui/app/templates/index.html`.

To reduce conflicts:

- keep `main.py` thin and include routers from separate modules;
- prefer `eval_service/app/api.py`, `callbacks.py`, `orchestrator.py` over large endpoint code in `main.py`;
- if UI grows, split Eval code into `micro-ui/app/static/eval.js` and `micro-ui/app/static/eval.css`;
- avoid touching existing buyer runtime files unless an implementation task proves it is necessary.

## Independent Task Candidates

These are the best candidates for separate workers:

- `case registry`: isolated parser and fixtures.
- `run storage`: isolated filesystem state.
- `buyer_client/auth`: isolated HTTP client and secrets loader.
- `trace+sanitizer`: isolated offline processing.
- `judge schema/runner`: isolated command execution and schema validation.
- `summary/baseline`: isolated deterministic aggregation.
- `micro-ui shell`: can start against mock API payloads.

## Suggested Execution Order

```text
1. Implement 0 and commit.
2. Dispatch 1, 2, 3, 6, and UI shell from 10 in parallel.
3. Integrate 1/2/3, then implement 4.
4. Implement 7 and 8 in parallel with 4.
5. Implement 5 after 1/2/3/4 are stable.
6. Implement 9 after 5/7/8 expose stable functions.
7. Finish 10 against real API.
8. Complete 11.
```

## Minimum Verification Per Package

- Unit tests must not hit real `litres.ru`, `brandshop.ru`, browser sidecar or LLM.
- Tests should use fixture YAML, fixture trace dirs, fake buyer client and fake judge output.
- `uv run --with-requirements <requirements> --with pytest pytest <target>` should be the default test shape for Python packages, matching project test conventions.
- Docker smoke checks belong only to final integration, not to every package.
