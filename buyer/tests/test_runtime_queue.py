from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from typing import Any

from buyer.app.auth_scripts import AUTH_OK, AuthScriptResult
from buyer.app.models import AgentOutput, EventEnvelope, SessionStatus, TaskAuthPayload
from buyer.app.runtime import BrowserSlot, BrowserSlotManager
from buyer.app.service import BuyerService
from buyer.app.state import InMemorySessionRepository, ReplyValidationError, SessionStore


class _RecordingCallbackClient:
    def __init__(self) -> None:
        self.delivered: list[EventEnvelope] = []

    def build_envelope(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_suffix: str | None = None,
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
        )

    async def deliver(self, callback_url: str, envelope: EventEnvelope) -> None:
        _ = callback_url
        self.delivered.append(envelope)


class _NoopAuthScriptRunner:
    def registry_snapshot(self) -> list[dict[str, str]]:
        return []

    async def run(self, **_: Any) -> Any:
        return None


class _RecordingAuthScriptRunner:
    def __init__(self) -> None:
        self.cdp_endpoints: list[str | None] = []

    def registry_snapshot(self) -> list[dict[str, str]]:
        return [{'domain': 'example.com', 'lifecycle': 'publish'}]

    async def run(self, **kwargs: Any) -> AuthScriptResult:
        self.cdp_endpoints.append(kwargs.get('cdp_endpoint'))
        return AuthScriptResult(
            status='completed',
            reason_code=AUTH_OK,
            message='auth ok',
            artifacts={'context_prepared_for_reuse': True},
        )


class _SequenceRunner:
    def __init__(self, outputs: list[AgentOutput]) -> None:
        self._outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def run_step(self, **kwargs: Any) -> AgentOutput:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._outputs) - 1)
        return self._outputs[index]


async def _wait_for_status(store: SessionStore, session_id: str, status: SessionStatus) -> None:
    for _ in range(100):
        state = await store.get(session_id)
        if state.status == status:
            return
        await asyncio.sleep(0.01)
    final = await store.get(session_id)
    raise AssertionError(f'Сессия {session_id} не перешла в {status}, текущий статус: {final.status}')


def _service(
    *,
    store: SessionStore,
    runner: Any,
    auth_script_runner: Any | None = None,
    waiting_user_timeout_sec: float = 1.0,
    max_active_jobs_per_worker: int = 1,
) -> BuyerService:
    return BuyerService(
        store=store,
        callback_client=_RecordingCallbackClient(),  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        novnc_url='http://legacy-novnc',
        default_callback_url='http://callback',
        cdp_recovery_window_sec=0.01,
        cdp_recovery_interval_ms=1,
        sberid_allowlist={'example.com'},
        sberid_auth_retry_budget=0,
        auth_script_runner=auth_script_runner or _NoopAuthScriptRunner(),  # type: ignore[arg-type]
        browser_slot_manager=BrowserSlotManager(
            slots=[
                BrowserSlot(
                    slot_id='slot-a',
                    cdp_endpoint='http://browser-a:9223',
                    novnc_url='http://localhost:6901/vnc.html',
                )
            ]
        ),
        max_active_jobs_per_worker=max_active_jobs_per_worker,
        waiting_user_timeout_sec=waiting_user_timeout_sec,
    )


class RuntimeQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_claims_queued_session_once(self) -> None:
        repository = InMemorySessionRepository()
        store = SessionStore(repository=repository, max_active_sessions=1)
        created = await store.create_session(
            task='Купить книгу',
            start_url='https://example.com',
            callback_url='http://callback',
            novnc_url='',
            metadata={},
            auth=None,
        )

        self.assertEqual(created.status, SessionStatus.QUEUED)

        claimed = await store.claim_next_queued_session(worker_id='worker-1', claim_token='claim-1')
        duplicate = await store.claim_next_queued_session(worker_id='worker-2', claim_token='claim-2')

        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.session_id, created.session_id)
        self.assertEqual(claimed.status, SessionStatus.RUNNING)
        self.assertEqual(claimed.runtime_worker_id, 'worker-1')
        self.assertEqual(claimed.runtime_claim_token, 'claim-1')
        self.assertIsNone(duplicate)

    async def test_slot_manager_holds_slot_and_enforces_domain_limit_until_release(self) -> None:
        manager = BrowserSlotManager(
            slots=[
                BrowserSlot(slot_id='slot-a', cdp_endpoint='http://browser-a:9223', novnc_url='http://novnc-a'),
                BrowserSlot(slot_id='slot-b', cdp_endpoint='http://browser-b:9223', novnc_url='http://novnc-b'),
            ],
            domain_limits={'example.com': 1},
        )

        first = await manager.acquire(session_id='session-1', domain='example.com')
        blocked_by_domain = await manager.acquire(session_id='session-2', domain='example.com')
        other_domain = await manager.acquire(session_id='session-3', domain='other.example')

        self.assertEqual(first.slot_id, 'slot-a')
        self.assertIsNone(blocked_by_domain)
        self.assertEqual(other_domain.slot_id, 'slot-b')

        await manager.release('session-1')
        after_release = await manager.acquire(session_id='session-2', domain='example.com')

        self.assertEqual(after_release.slot_id, 'slot-a')

    async def test_waiting_user_timeout_finishes_session_and_late_reply_is_rejected_machine_readably(self) -> None:
        runner = _SequenceRunner([
            AgentOutput(status='needs_user_input', message='Какой размер?', order_id=None, artifacts={}),
        ])
        store = SessionStore(max_active_sessions=1)
        service = _service(store=store, runner=runner, waiting_user_timeout_sec=0.02)
        await service.start_workers()
        try:
            created = await service.create_session(
                task='Купить футболку',
                start_url='https://example.com',
                callback_url='http://callback',
                metadata={},
                auth=None,
            )

            await _wait_for_status(store, created.session_id, SessionStatus.FAILED)
            final_state = await store.get(created.session_id)
            result = await service.submit_reply(final_state.session_id, 'late-reply', 'XL')

            self.assertFalse(result.accepted)
            self.assertEqual(result.reason_code, 'waiting_user_timeout')
            self.assertEqual(result.state.status, SessionStatus.FAILED)
            self.assertIn('waiting_user_timeout', final_state.last_error or '')
        finally:
            await service.shutdown_workers()

    async def test_worker_passes_slot_specific_endpoint_to_runner_and_auth_script(self) -> None:
        runner = _SequenceRunner([
            AgentOutput(status='completed', message='Готово', order_id='order-1', artifacts={}),
        ])
        auth_runner = _RecordingAuthScriptRunner()
        store = SessionStore(max_active_sessions=1)
        service = _service(store=store, runner=runner, auth_script_runner=auth_runner)
        await service.start_workers()
        try:
            created = await service.create_session(
                task='Купить товар',
                start_url='https://example.com',
                callback_url='http://callback',
                metadata={},
                auth=TaskAuthPayload(storageState={'cookies': [], 'origins': []}),
            )

            await _wait_for_status(store, created.session_id, SessionStatus.COMPLETED)
            final_state = await store.get(created.session_id)

            self.assertEqual(final_state.browser_slot_id, 'slot-a')
            self.assertEqual(final_state.browser_cdp_endpoint, 'http://browser-a:9223')
            self.assertEqual(auth_runner.cdp_endpoints, ['http://browser-a:9223'])
            self.assertEqual(runner.calls[0]['browser_cdp_endpoint'], 'http://browser-a:9223')
        finally:
            await service.shutdown_workers()


class RuntimeQueueMigrationTests(unittest.TestCase):
    def test_runtime_queue_migration_declares_required_columns(self) -> None:
        from buyer.app.persistence import SCHEMA_MIGRATIONS

        sql = '\n'.join(migration_sql for _, migration_sql in SCHEMA_MIGRATIONS)

        for column in (
            'queued_at',
            'started_at',
            'finished_at',
            'waiting_deadline_at',
            'runtime_worker_id',
            'runtime_claim_token',
            'runtime_heartbeat_at',
            'browser_slot_id',
            'browser_cdp_endpoint',
        ):
            self.assertIn(column, sql)

