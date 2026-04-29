# External Sber Cookies API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить внешний Sber cookies API как машинный источник SberId auth-пакета.

**Architecture:** Новый `buyer/app/external_auth.py` отвечает за HTTP GET `/api/v1/cookies`, validation и преобразование cookies в Playwright `storageState`. `BuyerService` получает optional client и вызывает его только если inline `auth.storageState` отсутствует. Полученный пакет сохраняется через `SessionStore.set_auth()` только в runtime auth, а в `auth_summary` попадает безопасная metadata.

**Tech Stack:** Python 3, httpx, Pydantic Settings, FastAPI dependency assembly, pytest/unittest.

**Linear:** [MON-30](https://linear.app/monaco-dev/issue/MON-30/buyer-phase-1-external-sber-cookies-api-kak-auth-source).

---

### Task 1: External Cookies Client

**Files:**
- Create: `buyer/app/external_auth.py`
- Test: `buyer/tests/test_external_auth.py`

- [ ] **Step 1: Add tests**

Cover:

```python
def test_cookies_payload_to_storage_state_accepts_valid_payload():
    # expects reason_code auth_external_loaded
    # expects {'cookies': [...], 'origins': []}
    # expects metadata cookie_count/domains/updated_at without cookie values


def test_cookies_payload_to_storage_state_rejects_empty_payload():
    # expects auth_external_empty_payload


def test_cookies_payload_to_storage_state_rejects_invalid_cookie_shape():
    # expects auth_external_invalid_payload


async def test_client_returns_loaded_result_with_mock_transport():
    # GET https://auth.example/api/v1/cookies
    # response JSON -> storage_state


async def test_client_maps_timeout():
    # httpx.TimeoutException -> auth_external_timeout
```

- [ ] **Step 2: Implement client**

Create:

- `ExternalSberCookiesResult(reason_code, storage_state=None, metadata={}, message=None)`;
- `ExternalSberCookiesClient(base_url, timeout_sec, retries, http_client=None)`;
- `fetch_storage_state()`;
- `cookies_payload_to_storage_state(payload)`.

Client must not log cookie values.

### Task 2: Settings And App Wiring

**Files:**
- Modify: `buyer/app/settings.py`
- Modify: `buyer/app/main.py`
- Modify: `.env.example`
- Modify: `.env` only if it exists

- [ ] **Step 1: Add settings**

```python
    sber_auth_source: Literal['inline_only', 'external_cookies_api'] = 'inline_only'
    sber_cookies_api_url: str = ''
    sber_cookies_api_timeout_sec: float = Field(default=5.0, ge=0.1)
    sber_cookies_api_retries: int = Field(default=1, ge=0)
```

- [ ] **Step 2: Wire in `main.py`**

When `settings.sber_auth_source == 'external_cookies_api'`, create `ExternalSberCookiesClient` and pass it into `BuyerService`. Close it on shutdown after `service.shutdown_post_session_analysis()`.

- [ ] **Step 3: Sync env example**

Add:

```dotenv
SBER_AUTH_SOURCE=inline_only
SBER_COOKIES_API_URL=
SBER_COOKIES_API_TIMEOUT_SEC=5
SBER_COOKIES_API_RETRIES=1
```

If `.env` exists, add the same keys there according to project rule.

### Task 3: Service Integration

**Files:**
- Modify: `buyer/app/service.py`
- Test: `buyer/tests/test_external_auth.py`

- [ ] **Step 1: Add service tests**

Cover:

```python
async def test_external_auth_used_when_inline_missing():
    # fake external client returns auth_external_loaded + storage_state
    # _run_sberid_auth_flow stores auth in SessionStore runtime state
    # auth_summary['source'] == 'external_cookies_api'


async def test_inline_auth_skips_external_client():
    # state.auth is present
    # fake external client call count remains 0


async def test_external_failure_continues_guest():
    # fake external client returns auth_external_timeout
    # summary mode/path guest and reason_code auth_external_timeout
```

- [ ] **Step 2: Add dependency to `BuyerService`**

Constructor gets:

```python
        external_auth_client: ExternalSberCookiesClient | None = None,
```

- [ ] **Step 3: Resolve auth before provider checks**

Add helper:

```python
    async def _resolve_session_auth(self, state: SessionState, summary: dict[str, Any]) -> TaskAuthPayload | None:
        if state.auth is not None:
            summary['source'] = 'inline'
            return state.auth
        if self._external_auth_client is None:
            summary['source'] = 'none'
            summary['reason_code'] = 'auth_not_provided'
            return None
        result = await self._external_auth_client.fetch_storage_state()
        summary['source'] = 'external_cookies_api'
        summary['external_auth'] = result.metadata
        if result.storage_state is None:
            summary['reason_code'] = result.reason_code
            if result.message:
                summary['external_auth_message'] = _tail_text(result.message, limit=500)
            return None
        auth = TaskAuthPayload(provider='sberid', storageState=result.storage_state)
        await self._store.set_auth(state.session_id, auth)
        state.auth = auth
        summary['reason_code'] = result.reason_code
        return auth
```

Then use it at the start of `_run_sberid_auth_flow`.

### Task 4: Docs And Verification

**Files:**
- Modify: `docs/openapi.yaml`
- Modify: `docs/callbacks.openapi.yaml`
- Modify: `docs/buyer.md`
- Modify: `docs/architecture-decisions.md`
- Modify: `docs/repository-map.md`

- [ ] **Step 1: Sync docs**

Docs must mention source priority, env settings, reason-codes, and session-bound storage.

- [ ] **Step 2: Run tests**

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_external_auth.py buyer/tests/test_auth_reply_removal.py buyer/tests/test_cdp_recovery.py buyer/tests/test_observability_and_cdp_tool.py -q
```

- [ ] **Step 3: Compile changed modules**

```bash
uv run --with-requirements buyer/requirements.txt python -m py_compile buyer/app/external_auth.py buyer/app/settings.py buyer/app/main.py buyer/app/service.py buyer/app/models.py
```
