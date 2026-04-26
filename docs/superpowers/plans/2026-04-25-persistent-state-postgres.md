# Persistent State Postgres Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Перевести долговременное состояние `buyer` на Postgres без изменения внешнего API.

**Architecture:** `SessionStore` остается фасадом для `BuyerService`, но получает backend через repository. Postgres repository хранит сессии, события, replies, agent memory, auth metadata и artifact refs; in-memory backend остается для быстрых тестов.

**Tech Stack:** Python 3.12, FastAPI, Pydantic Settings, asyncpg, pytest/unittest, Docker Compose Postgres.

---

### Task 1: Документация Границ

**Files:**
- Modify: `docs/buyer-roadmap.md`
- Modify: `docs/architecture-decisions.md`
- Modify: `README.md`

- [ ] **Step 1: Add future browser-state task**

Добавить отдельный roadmap-пункт про исследование сохранения browser context/storage между рестартами. Зафиксировать, что текущая Postgres-задача не сохраняет cookies/tokens/storageState.

- [ ] **Step 2: Update README limitations**

Обновить раздел текущей реализации: persistent session/event state есть, автоматическое продолжение active runner после restart пока не входит.

### Task 2: Failing Store Tests

**Files:**
- Create: `buyer/tests/test_persistent_state.py`

- [ ] **Step 1: Write failing tests**

Покрыть восстановление статуса, событий, replies, agent memory и отсутствие восстановления `storageState` из persistent backend.

- [ ] **Step 2: Run tests and verify red**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py -q`

Expected: import/name failures for repository classes.

### Task 3: Repository Layer

**Files:**
- Modify: `buyer/app/state.py`

- [ ] **Step 1: Implement repository protocol and in-memory repository**

Добавить `SessionRepository` protocol, record dataclasses, `InMemorySessionRepository`. `SessionStore` должен принимать `repository` и работать через него.

- [ ] **Step 2: Preserve runtime fields**

`SessionStore` должен держать локальный словарь runtime handles `{session_id: task_ref, wake_event, auth}` и приклеивать эти поля к `SessionState` при чтении.

- [ ] **Step 3: Run store tests**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py buyer/tests/test_cdp_recovery.py -q`

Expected: PASS.

### Task 4: Postgres Repository And Migrations

**Files:**
- Create: `buyer/app/persistence.py`
- Modify: `buyer/requirements.txt`

- [ ] **Step 1: Add asyncpg dependency**

Добавить `asyncpg==0.30.0`.

- [ ] **Step 2: Implement migrations**

Добавить SQL migrations для `buyer_schema_migrations`, `buyer_sessions`, `buyer_events`, `buyer_replies`, `buyer_artifacts`, `buyer_auth_context`, `buyer_agent_memory`.

- [ ] **Step 3: Implement PostgresSessionRepository**

Методы repository должны читать/писать JSONB, timestamptz и сортировать events/memory стабильно.

- [ ] **Step 4: Add Postgres tests guarded by env**

Тесты используют `BUYER_TEST_DATABASE_URL`; если переменная не задана, они проверяют только SQL/mapping без сетевого подключения.

### Task 5: App Wiring

**Files:**
- Modify: `buyer/app/settings.py`
- Modify: `buyer/app/main.py`
- Modify: `docker-compose.yml`
- Modify: `.env.example`

- [ ] **Step 1: Add settings**

Добавить `state_backend`, `database_url`, pool sizes.

- [ ] **Step 2: Build store from settings**

`main.py` должен выбирать in-memory или Postgres repository. Startup вызывает `store.initialize()`, shutdown вызывает `store.aclose()`.

- [ ] **Step 3: Add compose postgres**

Добавить service `postgres`, volume `buyer-postgres-data`, env `POSTGRES_DB/USER/PASSWORD`, healthcheck и `DATABASE_URL` для buyer.

### Task 6: Verification

**Files:**
- All touched files

- [ ] **Step 1: Run targeted tests**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py buyer/tests/test_cdp_recovery.py buyer/tests/test_observability_and_cdp_tool.py -q`

- [ ] **Step 2: Compile changed Python files**

Run: `uv run --with-requirements buyer/requirements.txt python -m py_compile buyer/app/state.py buyer/app/persistence.py buyer/app/main.py buyer/app/settings.py buyer/tests/test_persistent_state.py`

- [ ] **Step 3: Review docs**

Проверить, что README, roadmap и architecture decisions не противоречат правилу `storageState` session-bound.
