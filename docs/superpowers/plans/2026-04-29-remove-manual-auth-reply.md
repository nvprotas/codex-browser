# Remove Manual Auth Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Удалить ручную передачу cookies/storageState через `ask_user` и `/v1/replies`.

**Architecture:** `BuyerService._run_sberid_auth_flow` перестает использовать пользовательский reply как auth-source. Невалидный inline `storageState` дает `auth_inline_invalid_payload` и guest-flow. Ошибки auth-скрипта больше не просят новый auth-пакет у пользователя, а ведут в существующий heuristic/handoff путь.

**Tech Stack:** Python 3, FastAPI, Pydantic v2, pytest/unittest.

**Linear:** [MON-29](https://linear.app/monaco-dev/issue/MON-29/buyer-phase-1-udalit-ruchnuyu-peredachu-auth-paketov-cherez).

---

### Task 1: Regression Tests For Forbidden Auth Reply UX

**Files:**
- Modify: `buyer/tests/test_observability_and_cdp_tool.py` or create `buyer/tests/test_auth_reply_removal.py`

- [ ] **Step 1: Add tests**

Create tests that assert:

```python
async def test_invalid_inline_auth_does_not_ask_user_for_storage_state():
    # создать SessionState с auth=TaskAuthPayload(storageState={'cookies': []})
    # заменить service._ask_user_for_reply на функцию, которая бросает AssertionError
    # вызвать service._run_sberid_auth_flow(state)
    # проверить summary['reason_code'] == 'auth_inline_invalid_payload'
    # проверить summary['mode'] == 'guest'


def test_service_no_longer_parses_auth_from_user_reply():
    assert not hasattr(BuyerService, '_parse_auth_from_user_reply')
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_auth_reply_removal.py -q
```

Expected: FAIL because current code still asks user for auth and still has `_parse_auth_from_user_reply`.

### Task 2: Remove Auth Refresh Through User Reply

**Files:**
- Modify: `buyer/app/service.py`

- [ ] **Step 1: Replace invalid storageState branch**

In `_run_sberid_auth_flow`, replace the branch that calls `_ask_user_for_reply` for invalid storage state with:

```python
            if not _is_valid_storage_state(storage_state):
                summary['mode'] = 'guest'
                summary['path'] = 'guest'
                summary['reason_code'] = 'auth_inline_invalid_payload'
                return summary
```

- [ ] **Step 2: Remove script-failure auth package retry**

Delete the block that asks:

```text
SberId-авторизация не подтвердилась. Отправьте новый auth-пакет...
```

For `AUTH_REFRESH_REQUESTED`, `AUTH_FAILED_REDIRECT_LOOP`, and `AUTH_FAILED_INVALID_SESSION`, keep fallback to the existing heuristic path.

- [ ] **Step 3: Delete parser**

Delete `BuyerService._parse_auth_from_user_reply`.

### Task 3: Update Contracts And Docs

**Files:**
- Modify: `docs/openapi.yaml`
- Modify: `docs/callbacks.openapi.yaml`
- Modify: `docs/buyer.md`
- Modify: `docs/architecture-decisions.md`
- Modify: `docs/repository-map.md`

- [ ] **Step 1: Confirm docs state the new rule**

Docs must say that `ask_user` and `/v1/replies` cannot request or transfer cookies, `storageState`, localStorage, tokens, or JSON auth-packets.

- [ ] **Step 2: Search for legacy UX**

Run:

```bash
rg -n "Отправьте новый auth-пакет|JSON с storageState|_parse_auth_from_user_reply|storageState.*reply|reply.*storageState" buyer docs
```

Expected: no current-behavior matches. Historical specs may mention the removed behavior only as legacy context.

### Task 4: Verification

**Files:**
- No code changes unless verification finds failures.

- [ ] **Step 1: Run focused tests**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_auth_reply_removal.py buyer/tests/test_cdp_recovery.py buyer/tests/test_observability_and_cdp_tool.py -q
```

Expected: PASS.

- [ ] **Step 2: Compile changed modules**

Run:

```bash
uv run --with-requirements buyer/requirements.txt python -m py_compile buyer/app/service.py buyer/app/models.py
```

Expected: no output and exit code 0.
