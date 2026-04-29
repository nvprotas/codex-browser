from __future__ import annotations

import asyncio
import contextlib
import unittest
from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

from buyer.app import main as buyer_main
from buyer.app.models import AgentOutput, EventEnvelope, SessionStatus
from buyer.app.service import BuyerService
from buyer.app.state import SessionNotFoundError, SessionStore


class _RecordingCallbackClient:
    def __init__(self) -> None:
        self.delivered: list[EventEnvelope] = []
        self.headers: list[dict[str, str] | None] = []

    def build_envelope(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_suffix: str | None = None,
        *,
        eval_run_id: str | None = None,
        eval_case_id: str | None = None,
    ) -> EventEnvelope:
        seq = len(self.delivered) + 1
        suffix = idempotency_suffix or str(seq)
        return EventEnvelope(
            event_id=f'event-{seq}',
            session_id=session_id,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc),
            idempotency_key=f'{session_id}:{event_type}:{suffix}',
            payload=payload,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
        )

    async def deliver(self, callback_url: str, envelope: EventEnvelope, *, headers: dict[str, str] | None = None) -> None:
        _ = callback_url
        self.delivered.append(envelope)
        self.headers.append(headers)


class _NoopAuthScriptRunner:
    def registry_snapshot(self) -> list[dict[str, str]]:
        return []

    async def run(self, **_: Any) -> None:
        return None


class _SequenceRunner:
    def __init__(self, outputs: list[AgentOutput]) -> None:
        self._outputs = outputs
        self.calls = 0

    async def run_step(self, **_: Any) -> AgentOutput:
        index = min(self.calls, len(self._outputs) - 1)
        self.calls += 1
        return self._outputs[index]


class _BlockingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run_step(self, **_: Any) -> AgentOutput:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError('blocking runner должен завершаться только отменой')


class _CancelSwallowingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run_step(self, **_: Any) -> AgentOutput:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            return AgentOutput(status='failed', message='late failed result', artifacts={})
        raise AssertionError('runner должен быть отменен')


def _service(*, store: SessionStore, runner: Any, callback_client: _RecordingCallbackClient) -> BuyerService:
    return BuyerService(
        store=store,
        callback_client=callback_client,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        novnc_url='http://novnc',
        default_callback_url='http://callback',
        cdp_recovery_window_sec=0,
        cdp_recovery_interval_ms=1,
        sberid_allowlist=set(),
        sberid_auth_retry_budget=0,
        auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
    )


async def _wait_for_status(store: SessionStore, session_id: str, status: SessionStatus) -> None:
    for _ in range(50):
        state = await store.get(session_id)
        if state.status == status:
            return
        await asyncio.sleep(0.01)
    final = await store.get(session_id)
    raise AssertionError(f'Ожидался статус {status}, получен {final.status}')


async def _drain_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)


class SessionStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_running_session_finalizes_failed_cancels_task_and_frees_slot(self) -> None:
        runner = _BlockingRunner()
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
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        stopped = await service.stop_session(state.session_id, reason='Оператор остановил сценарий')
        await _drain_task(state.task_ref)

        self.assertTrue(stopped.accepted)
        self.assertEqual(stopped.status, SessionStatus.FAILED)
        self.assertTrue(runner.cancelled)
        final = await store.get(state.session_id)
        self.assertEqual(final.status, SessionStatus.FAILED)
        self.assertEqual(final.last_error, 'Оператор остановил сценарий')
        scenario_finished = [event for event in final.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished), 1)
        self.assertEqual(scenario_finished[0].payload['reason_code'], 'session_stopped_by_operator')
        self.assertEqual(scenario_finished[0].payload['artifacts']['stop_reason'], 'Оператор остановил сценарий')

        next_state = await service.create_session(
            task='Новая покупка',
            start_url='https://example.test',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await service.stop_session(next_state.session_id, reason='cleanup')
        await _drain_task(next_state.task_ref)

    async def test_stop_waiting_user_wakes_runner_and_finishes_once(self) -> None:
        runner = _SequenceRunner([AgentOutput(status='needs_user_input', message='Выбрать цвет?', artifacts={})])
        store = SessionStore(max_active_sessions=1)
        callback_client = _RecordingCallbackClient()
        service = _service(store=store, runner=runner, callback_client=callback_client)

        state = await service.create_session(
            task='Купить товар',
            start_url='https://example.test',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await _wait_for_status(store, state.session_id, SessionStatus.WAITING_USER)

        stopped = await service.stop_session(state.session_id, reason=None)
        await _drain_task(state.task_ref)

        final = await store.get(state.session_id)
        self.assertTrue(stopped.accepted)
        self.assertEqual(final.status, SessionStatus.FAILED)
        self.assertEqual(final.last_error, 'Сессия остановлена оператором.')
        self.assertIsNone(final.waiting_reply_id)
        self.assertIsNone(final.waiting_question)
        scenario_finished = [event for event in final.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished), 1)
        self.assertEqual(scenario_finished[0].payload['reason_code'], 'session_stopped_by_operator')

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
        service = _service(
            store=store,
            runner=_SequenceRunner([]),
            callback_client=_RecordingCallbackClient(),
        )

        result = await service.stop_session(state.session_id, reason='late')

        self.assertFalse(result.accepted)
        self.assertEqual(result.status, SessionStatus.FAILED)
        final = await store.get(state.session_id)
        self.assertEqual(final.last_error, 'already failed')

    async def test_late_runner_result_after_stop_is_ignored(self) -> None:
        runner = _CancelSwallowingRunner()
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
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        stopped = await service.stop_session(state.session_id, reason='Оператор остановил сценарий')
        await _drain_task(state.task_ref)

        final = await store.get(state.session_id)
        scenario_finished = [event for event in final.events if event.event_type == 'scenario_finished']
        self.assertTrue(stopped.accepted)
        self.assertTrue(runner.cancelled)
        self.assertEqual(final.status, SessionStatus.FAILED)
        self.assertEqual(len(scenario_finished), 1)
        self.assertEqual(scenario_finished[0].payload['reason_code'], 'session_stopped_by_operator')
        self.assertNotIn('late failed result', str([event.payload for event in final.events]))

    async def test_stop_callback_keeps_callback_token_header_after_cancelling_task(self) -> None:
        runner = _BlockingRunner()
        store = SessionStore(max_active_sessions=1)
        callback_client = _RecordingCallbackClient()
        service = _service(store=store, runner=runner, callback_client=callback_client)

        state = await service.create_session(
            task='Купить книгу',
            start_url='https://example.test',
            callback_url='http://callback',
            metadata={},
            auth=None,
            callback_token='callback-secret',
        )
        await asyncio.wait_for(runner.started.wait(), timeout=1)

        await service.stop_session(state.session_id, reason='Оператор остановил сценарий')
        await _drain_task(state.task_ref)

        stop_event_index = next(
            index for index, event in enumerate(callback_client.delivered) if event.idempotency_key.endswith(':scenario-stopped')
        )
        self.assertEqual(callback_client.headers[stop_event_index], {'X-Eval-Callback-Token': 'callback-secret'})


class _StopServiceRaisesNotFound:
    async def stop_session(self, session_id: str, *, reason: str | None = None) -> Any:
        _ = reason
        raise SessionNotFoundError(f'Сессия {session_id} не найдена.')


class _StopServiceReturns:
    async def stop_session(self, session_id: str, *, reason: str | None = None) -> Any:
        _ = reason
        from buyer.app.models import SessionStopResponse

        return SessionStopResponse(session_id=session_id, accepted=True, status=SessionStatus.FAILED)


class StopEndpointTests(unittest.TestCase):
    def test_stop_endpoint_returns_404_for_unknown_session(self) -> None:
        original_service = buyer_main.service
        buyer_main.service = _StopServiceRaisesNotFound()  # type: ignore[assignment]
        try:
            response = TestClient(buyer_main.app).post('/v1/sessions/missing/stop', json={})
        finally:
            buyer_main.service = original_service

        self.assertEqual(response.status_code, 404)

    def test_stop_endpoint_returns_stop_response(self) -> None:
        original_service = buyer_main.service
        buyer_main.service = _StopServiceReturns()  # type: ignore[assignment]
        try:
            response = TestClient(buyer_main.app).post(
                '/v1/sessions/session-1/stop',
                json={'reason': 'Оператор остановил сценарий'},
            )
        finally:
            buyer_main.service = original_service

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'session_id': 'session-1', 'accepted': True, 'status': 'failed'})
