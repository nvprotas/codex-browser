from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

import httpx

from buyer.app.callback import CallbackClient
from buyer.app.models import EventEnvelope, SessionStatus
from buyer.app.runtime import InMemoryRuntimeCoordinator, RuntimeLockConflictError, parse_domain_limits
from buyer.app.service import BuyerService
from buyer.app.settings import Settings
from buyer.app.state import SessionConflictError, SessionStore


class _FailingCallbackRuntime:
    async def record_callback_attempt(self, **_: Any) -> None:
        raise RuntimeError('runtime marker unavailable')

    async def clear_callback_attempt(self, _: str) -> None:
        raise RuntimeError('runtime marker unavailable')


class _RejectingRuntimeCoordinator(InMemoryRuntimeCoordinator):
    async def acquire_session_runner(self, *, session_id: str, start_url: str):
        _ = session_id, start_url
        raise RuntimeLockConflictError('worker_active_limit', 'Достигнут лимит активных задач текущего worker.')


class _RecordingCallbackClient:
    def __init__(self) -> None:
        self.delivered: list[EventEnvelope] = []

    def build_envelope(self, session_id: str, event_type: str, payload: dict, idempotency_suffix: str | None = None) -> EventEnvelope:
        suffix = idempotency_suffix or str(len(self.delivered) + 1)
        return EventEnvelope(
            event_id=f'event-{len(self.delivered) + 1}',
            session_id=session_id,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc),
            idempotency_key=f'{session_id}:{event_type}:{suffix}',
            payload=payload,
        )

    async def deliver(self, callback_url: str, envelope: EventEnvelope) -> None:
        _ = callback_url
        self.delivered.append(envelope)


class _UnusedRunner:
    async def run_step(self, **_: Any):
        raise AssertionError('runner should not start after runtime lock rejection')


class _NoopAuthScriptRunner:
    def registry_snapshot(self) -> list[dict[str, str]]:
        return []

    async def run(self, **_: Any) -> None:
        return None


class RuntimeCoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_lock_blocks_second_start_for_same_session(self) -> None:
        coordinator = InMemoryRuntimeCoordinator(
            worker_id='worker-a',
            max_active_jobs_per_worker=4,
            max_handoff_sessions=1,
            domain_active_limit_default=None,
            lock_ttl_sec=60,
            marker_ttl_sec=60,
        )

        lease = await coordinator.acquire_session_runner(
            session_id='session-1',
            start_url='https://www.litres.ru/cart',
        )
        with self.assertRaises(RuntimeLockConflictError) as caught:
            await coordinator.acquire_session_runner(
                session_id='session-1',
                start_url='https://www.litres.ru/cart',
            )

        self.assertEqual(caught.exception.reason_code, 'session_runner_locked')

        await coordinator.release_session_runner(lease)
        next_lease = await coordinator.acquire_session_runner(
            session_id='session-1',
            start_url='https://www.litres.ru/cart',
        )
        await coordinator.release_session_runner(next_lease)

    async def test_runtime_limits_active_jobs_per_worker_and_domain(self) -> None:
        coordinator = InMemoryRuntimeCoordinator(
            worker_id='worker-a',
            max_active_jobs_per_worker=2,
            max_handoff_sessions=1,
            domain_active_limit_default=1,
            lock_ttl_sec=60,
            marker_ttl_sec=60,
        )

        first = await coordinator.acquire_session_runner(
            session_id='session-1',
            start_url='https://www.litres.ru/books',
        )
        with self.assertRaises(RuntimeLockConflictError) as same_domain:
            await coordinator.acquire_session_runner(
                session_id='session-2',
                start_url='https://litres.ru/cart',
            )
        self.assertEqual(same_domain.exception.reason_code, 'domain_active_limit')

        second = await coordinator.acquire_session_runner(
            session_id='session-3',
            start_url='https://brandshop.ru/',
        )
        with self.assertRaises(RuntimeLockConflictError) as worker_limit:
            await coordinator.acquire_session_runner(
                session_id='session-4',
                start_url='https://samokat.ru/',
            )
        self.assertEqual(worker_limit.exception.reason_code, 'worker_active_limit')

        await coordinator.release_session_runner(first)
        await coordinator.release_session_runner(second)

    async def test_runtime_markers_track_browser_handoff_and_callback_attempts(self) -> None:
        coordinator = InMemoryRuntimeCoordinator(
            worker_id='worker-a',
            max_active_jobs_per_worker=4,
            max_handoff_sessions=1,
            domain_active_limit_default=None,
            lock_ttl_sec=60,
            marker_ttl_sec=60,
        )
        lease = await coordinator.acquire_session_runner(
            session_id='session-1',
            start_url='https://example.com/',
        )

        await coordinator.mark_browser_context_active('session-1', lease=lease)
        browser_marker = await coordinator.get_marker('browser_context', 'session-1')
        self.assertEqual(browser_marker['session_id'], 'session-1')
        self.assertEqual(browser_marker['worker_id'], 'worker-a')

        handoff = await coordinator.acquire_handoff('session-1', reason_code='captcha')
        with self.assertRaises(RuntimeLockConflictError) as caught:
            await coordinator.acquire_handoff('session-2', reason_code='captcha')
        self.assertEqual(caught.exception.reason_code, 'handoff_active_limit')
        handoff_marker = await coordinator.get_marker('handoff', 'session-1')
        self.assertEqual(handoff_marker['reason_code'], 'captcha')

        await coordinator.record_callback_attempt(
            session_id='session-1',
            event_id='event-1',
            event_type='scenario_finished',
            attempt=2,
            attempts_total=3,
        )
        callback_marker = await coordinator.get_marker('callback_attempt', 'event-1')
        self.assertEqual(callback_marker['attempt'], 2)
        self.assertEqual(callback_marker['attempts_total'], 3)

        await coordinator.clear_callback_attempt('event-1')
        self.assertIsNone(await coordinator.get_marker('callback_attempt', 'event-1'))
        await coordinator.release_handoff(handoff)
        await coordinator.clear_browser_context_active('session-1')
        await coordinator.release_session_runner(lease)

    async def test_session_store_uses_worker_and_domain_limits(self) -> None:
        store = SessionStore(
            max_active_jobs_per_worker=2,
            domain_active_limit_default=1,
            status_ttl_sec=None,
        )

        first = await store.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )
        await store.set_status(first.session_id, SessionStatus.RUNNING)

        with self.assertRaises(SessionConflictError) as same_domain:
            await store.create_session(
                task='Купить вторую книгу',
                start_url='https://litres.ru/another',
                callback_url='http://callback',
                novnc_url='http://novnc',
                metadata={},
                auth=None,
            )
        self.assertIn('домена litres.ru', str(same_domain.exception))

        second = await store.create_session(
            task='Купить одежду',
            start_url='https://brandshop.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )
        await store.set_status(second.session_id, SessionStatus.RUNNING)

        with self.assertRaises(SessionConflictError) as worker_limit:
            await store.create_session(
                task='Купить продукты',
                start_url='https://samokat.ru/',
                callback_url='http://callback',
                novnc_url='http://novnc',
                metadata={},
                auth=None,
            )
        self.assertIn('активных сценариев', str(worker_limit.exception))

    def test_parse_domain_limits(self) -> None:
        self.assertEqual(parse_domain_limits('litres.ru=1, brandshop.ru=2'), {'litres.ru': 1, 'brandshop.ru': 2})
        self.assertEqual(parse_domain_limits(''), {})
        with self.assertRaises(ValueError):
            parse_domain_limits('broken')

    async def test_callback_delivery_does_not_fail_when_runtime_marker_is_unavailable(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200)

        client = CallbackClient(
            Settings(callback_retries=1),
            runtime_coordinator=_FailingCallbackRuntime(),  # type: ignore[arg-type]
        )
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
        envelope = EventEnvelope(
            event_id='event-1',
            session_id='session-1',
            event_type='scenario_finished',
            occurred_at=datetime.now(timezone.utc),
            idempotency_key='session-1:scenario_finished:test',
            payload={'status': 'completed'},
        )
        try:
            await client.deliver('http://callback.test/events', envelope)
        finally:
            await client.aclose()

        self.assertEqual(len(requests), 1)

    async def test_runtime_lock_rejection_finishes_session_with_failed_callback(self) -> None:
        callback_client = _RecordingCallbackClient()
        store = SessionStore(max_active_jobs_per_worker=4)
        service = BuyerService(
            store=store,
            callback_client=callback_client,  # type: ignore[arg-type]
            runner=_UnusedRunner(),  # type: ignore[arg-type]
            novnc_url='http://novnc',
            default_callback_url='http://callback',
            cdp_recovery_window_sec=0.2,
            cdp_recovery_interval_ms=1,
            sberid_allowlist=set(),
            sberid_auth_retry_budget=1,
            auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
            runtime_coordinator=_RejectingRuntimeCoordinator(),
        )

        state = await service.create_session(
            task='test-task',
            start_url='https://example.com',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref

        final_state = await store.get(state.session_id)
        self.assertEqual(final_state.status, SessionStatus.FAILED)
        scenario_finished = [event for event in final_state.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished), 1)
        self.assertEqual(scenario_finished[0].payload['status'], 'failed')
        self.assertIn('worker_active_limit', scenario_finished[0].payload['artifacts']['runtime']['reason_code'])
