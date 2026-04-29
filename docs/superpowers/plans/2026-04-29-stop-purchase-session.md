# Stop Purchase Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить остановку активной покупочной сессии через `buyer` API и `micro-ui`, включая принудительное завершение активного `codex exec`.

**Architecture:** `BuyerService.stop_session()` выполняет штатную failed-финализацию с reason-code `session_stopped_by_operator`, отменяет runtime task и освобождает active slot. `AgentRunner` становится cancellation-aware: при отмене во время `codex exec` он убивает subprocess и закрывает stream publisher. `micro-ui` получает proxy endpoint и кнопку для активных сессий.

**Tech Stack:** Python 3, FastAPI, Pydantic v2, asyncio subprocess, pytest/unittest, browser-side JavaScript.

---

### Task 1: Buyer Stop API And Store Semantics

**Files:**
- Modify: `buyer/app/models.py`
- Modify: `buyer/app/state.py`
- Modify: `buyer/app/service.py`
- Modify: `buyer/app/main.py`
- Test: `buyer/tests/test_session_stop.py`

- [ ] **Step 1: Write failing service and API tests**

Create `buyer/tests/test_session_stop.py` with tests equivalent to:

```python
class SessionStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_running_session_finalizes_failed_cancels_task_and_frees_slot(self) -> None:
        gate = asyncio.Event()
        runner = _BlockingRunner(gate)
        store = SessionStore(max_active_sessions=1)
        callback_client = _RecordingCallbackClient()
        service = _service(store=store, runner=runner, callback_client=callback_client)

        state = await service.create_session(
            task='Купить книгу',
            start_url='https://example.test',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await runner.started.wait()

        stopped = await service.stop_session(state.session_id, reason='Оператор остановил сценарий')
        await asyncio.wait_for(state.task_ref, timeout=1)

        self.assertTrue(stopped.accepted)
        self.assertEqual(stopped.status, SessionStatus.FAILED)
        self.assertTrue(runner.cancelled)
        final = await store.get(state.session_id)
        self.assertEqual(final.status, SessionStatus.FAILED)
        self.assertEqual(final.last_error, 'Оператор остановил сценарий')
        scenario_finished = [event for event in final.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished), 1)
        self.assertEqual(scenario_finished[0].payload['reason_code'], 'session_stopped_by_operator')

        next_state = await service.create_session(
            task='Новая покупка',
            start_url='https://example.test',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await service.stop_session(next_state.session_id, reason='cleanup')

    async def test_stop_waiting_user_wakes_runner_and_finishes_once(self) -> None:
        runner = _SequenceRunner([AgentOutput(status='needs_user_input', message='Выбрать цвет?', artifacts={})])
        store = SessionStore(max_active_sessions=1)
        service = _service(store=store, runner=runner, callback_client=_RecordingCallbackClient())

        state = await service.create_session(
            task='Купить товар',
            start_url='https://example.test',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await _wait_for_status(store, state.session_id, SessionStatus.WAITING_USER)

        stopped = await service.stop_session(state.session_id, reason=None)
        await asyncio.wait_for(state.task_ref, timeout=1)

        final = await store.get(state.session_id)
        self.assertTrue(stopped.accepted)
        self.assertEqual(final.status, SessionStatus.FAILED)
        self.assertIsNone(final.waiting_reply_id)
        self.assertEqual(
            [event.event_type for event in final.events].count('scenario_finished'),
            1,
        )

    async def test_stop_terminal_session_is_idempotent(self) -> None:
        store = SessionStore(max_active_sessions=1)
        state = await store.create_session(
            task='done',
            start_url='https://example.test',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )
        await store.set_status(state.session_id, SessionStatus.FAILED, error='already failed')
        service = _service(store=store, runner=_SequenceRunner([]), callback_client=_RecordingCallbackClient())

        result = await service.stop_session(state.session_id, reason='late')

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, SessionStatus.FAILED)


class StopEndpointTests(unittest.TestCase):
    def test_stop_endpoint_returns_404_for_unknown_session(self) -> None:
        original_service = buyer_main.service
        buyer_main.service = _StopServiceRaisesNotFound()
        try:
            response = TestClient(buyer_main.app).post('/v1/sessions/missing/stop', json={})
        finally:
            buyer_main.service = original_service

        self.assertEqual(response.status_code, 404)

    def test_stop_endpoint_returns_stop_response(self) -> None:
        original_service = buyer_main.service
        buyer_main.service = _StopServiceReturns()
        try:
            response = TestClient(buyer_main.app).post(
                '/v1/sessions/session-1/stop',
                json={'reason': 'Оператор остановил сценарий'},
            )
        finally:
            buyer_main.service = original_service

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'session_id': 'session-1', 'accepted': True, 'status': 'failed'})
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_session_stop.py -q
```

Expected: FAIL because `SessionStopRequest`, `SessionStopResponse`, `BuyerService.stop_session()`, and endpoint `/v1/sessions/{session_id}/stop` do not exist.

- [ ] **Step 3: Implement minimal buyer stop contract**

Add to `buyer/app/models.py`:

```python
class SessionStopRequest(BaseModel):
    reason: str | None = Field(default=None, min_length=1)


class SessionStopResponse(BaseModel):
    session_id: str
    accepted: bool
    status: SessionStatus
```

Add to `buyer/app/state.py`:

```python
    async def stop_waiting_or_running(self, session_id: str, *, error: str) -> tuple[SessionState, bool]:
        async with self._lock:
            state = await self._get_locked(session_id)
            if state.status in self._TERMINAL_STATUSES:
                return self._attach_runtime(state), False
            state.status = SessionStatus.FAILED
            state.last_error = error
            state.waiting_reply_id = None
            state.waiting_question = None
            state.pending_reply_text = None
            self._runtime_sessions.discard(session_id)
            self._wake_for(session_id).set()
            self._touch_locked(state)
            await self._repository.update_session(state)
            return self._attach_runtime(state), True
```

Add `BuyerService.stop_session()` that:

- reads current state;
- returns `accepted=False` for terminal state;
- emits `scenario_finished` with `status='failed'`, `reason_code='session_stopped_by_operator'`;
- calls `SessionStore.stop_waiting_or_running()`;
- cancels `task_ref` if it exists and is not done;
- returns `SessionStopResponse`.

Add endpoint to `buyer/app/main.py`:

```python
@app.post('/v1/sessions/{session_id}/stop', response_model=SessionStopResponse)
async def stop_session(session_id: str, request: SessionStopRequest | None = None) -> SessionStopResponse:
    try:
        return await service.stop_session(session_id, reason=request.reason if request else None)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
```

- [ ] **Step 4: Run buyer stop tests to verify GREEN**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_session_stop.py -q
```

Expected: PASS.

### Task 2: Kill Active Codex Subprocess On Cancellation

**Files:**
- Modify: `buyer/app/runner.py`
- Test: `buyer/tests/test_cdp_recovery.py`

- [ ] **Step 1: Write failing runner cancellation test**

Add a test that monkeypatches `asyncio.create_subprocess_exec` in `buyer.app.runner` to return a fake process whose `wait()` blocks until canceled. The test starts `AgentRunner.run_step()`, waits until the process is created, cancels the task, and asserts:

```python
self.assertTrue(fake_process.killed)
self.assertTrue(task.cancelled())
```

- [ ] **Step 2: Run test to verify RED**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_cdp_recovery.py::AgentRunnerTests::test_run_step_cancellation_kills_codex_process -q
```

Expected: FAIL because cancellation currently propagates without killing the subprocess.

- [ ] **Step 3: Implement cancellation cleanup**

In `AgentRunner._execute_attempt()`, wrap the stream collection with:

```python
            try:
                attempt.stdout_text, attempt.stderr_text = await asyncio.wait_for(...)
            except asyncio.CancelledError:
                process.kill()
                await _communicate_quietly(process)
                await stream_publisher.aclose()
                logger.info('codex_step_cancelled step=%s role=%s model=%s', ...)
                raise
```

Keep existing `TimeoutError` behavior unchanged.

- [ ] **Step 4: Run focused runner tests**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_cdp_recovery.py::AgentRunnerTests -q
```

Expected: PASS.

### Task 3: Micro-UI Proxy And Button

**Files:**
- Modify: `micro-ui/app/models.py`
- Modify: `micro-ui/app/main.py`
- Modify: `micro-ui/app/static/app.js`
- Modify: `micro-ui/app/static/app.css`
- Test: `micro-ui/tests/test_store_stream.py`

- [ ] **Step 1: Write failing proxy test**

Add a test using `respx` or monkeypatched `httpx.AsyncClient` equivalent to:

```python
async def test_api_stop_session_forwards_to_buyer():
    request = StopSessionRequest(reason='Оператор остановил сценарий')
    # call POST /api/sessions/session-1/stop
    # assert buyer target is /v1/sessions/session-1/stop
    # assert JSON body includes reason
    # assert response.forwarded is True and buyer_response.status == 'failed'
```

- [ ] **Step 2: Run micro-ui test to verify RED**

Run:

```bash
uv run --with-requirements micro-ui/requirements.txt --with pytest pytest micro-ui/tests/test_store_stream.py -q
```

Expected: FAIL because the proxy endpoint and models do not exist.

- [ ] **Step 3: Implement proxy contract**

Add to `micro-ui/app/models.py`:

```python
class StopSessionRequest(BaseModel):
    reason: str | None = Field(default=None, min_length=1)


class StopSessionResponse(BaseModel):
    forwarded: bool
    buyer_response: dict[str, Any]
```

Add to `micro-ui/app/main.py`:

```python
@app.post('/api/sessions/{session_id}/stop', response_model=StopSessionResponse)
async def api_stop_session(session_id: str, request: StopSessionRequest | None = None) -> StopSessionResponse:
    target = f"{settings.buyer_base_url}/v1/sessions/{session_id}/stop"
    payload = request.model_dump() if request else {}
    ...
```

Follow the existing `/api/reply` error handling style.

- [ ] **Step 4: Add UI button**

In `micro-ui/app/static/app.js`, add `isActiveSessionStatus(status)` and render an `Остановить` button for selected/active sessions. The click handler calls:

```javascript
await fetchJson(`/api/sessions/${encodeURIComponent(session.session_id)}/stop`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ reason: 'Оператор остановил сценарий из micro-ui' }),
});
```

Then refresh sessions/events and update `global-status`.

- [ ] **Step 5: Run micro-ui tests**

Run:

```bash
uv run --with-requirements micro-ui/requirements.txt --with pytest pytest micro-ui/tests/test_store_stream.py micro-ui/tests/test_design_handoff.py -q
```

Expected: PASS.

### Task 4: API Contracts And Repository Map

**Files:**
- Modify: `docs/openapi.yaml`
- Modify: `docs/callbacks.openapi.yaml`
- Modify: `docs/buyer.md`
- Modify: `docs/repository-map.md`
- Modify: `README.md`

- [ ] **Step 1: Update HTTP OpenAPI**

Document `POST /v1/sessions/{session_id}/stop`, `SessionStopRequest`, and `SessionStopResponse`.

- [ ] **Step 2: Update callback docs**

Add `reason_code: session_stopped_by_operator` to `ScenarioFinishedPayload` documentation without adding a new event type.

- [ ] **Step 3: Update human docs**

Document stop behavior in `docs/buyer.md`, `docs/repository-map.md`, and README "Что уже реализовано".

- [ ] **Step 4: Search for contract drift**

Run:

```bash
rg -n "stop|session_stopped_by_operator|SessionStop|scenario_finished" buyer micro-ui docs README.md
```

Expected: stop contract appears in code and docs; no contradictory statement says active sessions cannot be stopped.

### Task 5: Final Verification

**Files:**
- No code changes unless verification finds failures.

- [ ] **Step 1: Run focused buyer tests**

Run:

```bash
uv run --with-requirements buyer/requirements.txt --with pytest pytest buyer/tests/test_session_stop.py buyer/tests/test_cdp_recovery.py buyer/tests/test_url_policy.py -q
```

Expected: PASS.

- [ ] **Step 2: Run focused micro-ui tests**

Run:

```bash
uv run --with-requirements micro-ui/requirements.txt --with pytest pytest micro-ui/tests/test_store_stream.py micro-ui/tests/test_design_handoff.py -q
```

Expected: PASS.

- [ ] **Step 3: Compile changed Python modules**

Run:

```bash
uv run --with-requirements buyer/requirements.txt python -m py_compile buyer/app/models.py buyer/app/state.py buyer/app/service.py buyer/app/main.py buyer/app/runner.py
uv run --with-requirements micro-ui/requirements.txt python -m py_compile micro-ui/app/models.py micro-ui/app/main.py
```

Expected: both commands exit 0.

- [ ] **Step 4: Commit implementation**

Run:

```bash
git add buyer micro-ui docs README.md
git commit -m "Add purchase session stop control"
```
