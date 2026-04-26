# Исправление Замечаний Ревью Persistent State

> **Для агентных исполнителей:** при выполнении задач использовать TDD: сначала failing test, затем минимальная реализация, затем verification. Шаги используют checkbox (`- [x]`) для отслеживания.

**Цель:** закрыть замечания ревью по безопасному сохранению persistent state и поведению активных сессий после рестарта.

**Архитектура:** `BuyerService` продолжает отправлять внешний callback с исходным payload, но repository сохраняет в Postgres только redacted-представление событий и артефактов. `SessionStore` различает активные сессии текущего процесса и stale-сессии, восстановленные из persistent backend без `task_ref`, чтобы restart не блокировал новые задачи до появления Redis locks/resume.

**Технологии:** Python 3.12, unittest/pytest, asyncpg repository, FastAPI state layer.

---

### Task 1: Redaction Persisted Event Payload

**Files:**
- Modify: `buyer/tests/test_persistent_state.py`
- Modify: `buyer/app/persistence.py`

- [x] **Step 1: Write failing test**

Добавить тест, который строит `EventEnvelope` с `storageState`, `storage_state_path`, `accessToken`, `refresh_token`, `authorization` и проверяет, что `_serialize_event_payload_for_storage(event.payload)` не содержит секреты и forbidden keys, но сохраняет безопасные поля.

- [x] **Step 2: Verify red**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py::PersistentStateStoreTests::test_event_payload_for_storage_redacts_sensitive_auth_data -q`

Expected: FAIL из-за отсутствия `_serialize_event_payload_for_storage` или из-за сохранения token-like ключей.

- [x] **Step 3: Implement minimal redaction**

В `buyer/app/persistence.py` добавить serializer для event payload и использовать его в `_sync_session_related` перед записью `buyer_events.payload`.

- [x] **Step 4: Verify green**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py::PersistentStateStoreTests::test_event_payload_for_storage_redacts_sensitive_auth_data -q`

Expected: PASS.

### Task 2: Token-Like Key Matching

**Files:**
- Modify: `buyer/tests/test_persistent_state.py`
- Modify: `buyer/app/persistence.py`

- [x] **Step 1: Write failing test**

Расширить redaction-тест проверкой ключей `accessToken`, `refresh_token`, `idToken`, `session-token`, `api_key`, `secret`.

- [x] **Step 2: Verify red**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py::PersistentStateStoreTests::test_persistent_metadata_redacts_token_like_key_variants -q`

Expected: FAIL на текущем sanitizer.

- [x] **Step 3: Implement pattern matching**

Вынести нормализацию ключей и блокировать ключи, содержащие token/secret/password/api key/session key/access key/refresh key/id key/auth key.

- [x] **Step 4: Verify green**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py::PersistentStateStoreTests::test_persistent_metadata_redacts_token_like_key_variants -q`

Expected: PASS.

### Task 3: Stale Active Sessions After Restart

**Files:**
- Modify: `buyer/tests/test_persistent_state.py`
- Modify: `buyer/app/state.py`

- [x] **Step 1: Write failing test**

Добавить тест с общим persistent repository: первый `SessionStore` создает `RUNNING` сессию, второй `SessionStore` на том же repository имитирует рестарт и должен позволить создать новую задачу при `max_active_sessions=1`.

- [x] **Step 2: Verify red**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py::PersistentStateStoreTests::test_restarted_store_ignores_stale_active_sessions_without_runtime_task -q`

Expected: FAIL с `SessionConflictError`.

- [x] **Step 3: Implement runtime-aware active counting**

В `SessionStore.create_session` считать активными только non-terminal сессии с runtime `task_ref`, а stale persistent сессии без runtime handle не блокируют новый запуск до реализации Redis locks/resume.

- [x] **Step 4: Verify green**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py::PersistentStateStoreTests::test_restarted_store_ignores_stale_active_sessions_without_runtime_task -q`

Expected: PASS.

### Task 4: Full Verification

**Files:**
- All touched files

- [x] **Step 1: Run targeted tests**

Run: `uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_persistent_state.py buyer/tests/test_cdp_recovery.py buyer/tests/test_observability_and_cdp_tool.py -q`

- [x] **Step 2: Compile changed Python files**

Run: `uv run --with-requirements buyer/requirements.txt python -m py_compile buyer/app/state.py buyer/app/persistence.py buyer/tests/test_persistent_state.py`

- [x] **Step 3: Review diff**

Run: `git diff --check`

Expected: no whitespace errors.
