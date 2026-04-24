from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from buyer.app.models import AgentOutput, EventEnvelope, SessionStatus
from buyer.app.purchase_scripts import PurchaseScriptResult
from buyer.app.runner import AgentRunner, _trace_date_dir_name
from buyer.app.service import BuyerService
from buyer.app.settings import Settings
from buyer.app.state import SessionStore


class _FakeProcess:
    def __init__(self, *, returncode: int, stdout_text: str = '', stderr_text: str = '') -> None:
        self.returncode = returncode
        self._stdout = stdout_text.encode('utf-8')
        self._stderr = stderr_text.encode('utf-8')

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        return


class _SequenceRunner:
    def __init__(self, outputs: list[AgentOutput]) -> None:
        self._outputs = outputs
        self.calls = 0

    async def run_step(self, **_: Any) -> AgentOutput:
        index = min(self.calls, len(self._outputs) - 1)
        self.calls += 1
        return self._outputs[index]


class _RecordingCallbackClient:
    def __init__(self) -> None:
        self.delivered: list[EventEnvelope] = []

    def build_envelope(self, session_id: str, event_type: str, payload: dict, idempotency_suffix: str | None = None) -> EventEnvelope:
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


class _CompletedPurchaseScriptRunner:
    async def run(self, **_: Any) -> PurchaseScriptResult:
        return PurchaseScriptResult(
            status='completed',
            reason_code='purchase_ready',
            message='Скрипт дошел до оплаты',
            order_id='order-456',
            artifacts={'source': 'purchase-script-test'},
        )


class _ThrowingPurchaseScriptRunner:
    async def run(self, **_: Any) -> PurchaseScriptResult:
        raise OSError('purchase runtime unavailable')


class CDPRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_trace_context_uses_date_and_time_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            runner = AgentRunner(Settings(buyer_trace_dir=tmpdir))

            trace = runner._prepare_trace_context(session_id='session-123', step_index=7)

            expected_session_dir = Path(tmpdir) / trace['trace_date'] / trace['trace_time'] / 'session-123'
            self.assertEqual(trace['session_dir'], expected_session_dir)
            self.assertEqual(trace['trace_date'], _trace_date_dir_name())
            self.assertRegex(trace['trace_time'], r'^\d{2}-\d{2}-\d{2}$')
            self.assertTrue(expected_session_dir.is_dir())
            self.assertEqual(trace['prompt_path'], expected_session_dir / 'step-007-prompt.txt')
            self.assertEqual(trace['browser_actions_log_path'], expected_session_dir / 'step-007-browser-actions.jsonl')
            self.assertEqual(trace['step_trace_path'], expected_session_dir / 'step-007-trace.json')

    async def test_trace_context_reuses_existing_session_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            existing_session_dir = Path(tmpdir) / '2026-04-24' / '10-20-30' / 'session-123'
            existing_session_dir.mkdir(parents=True)
            runner = AgentRunner(Settings(buyer_trace_dir=tmpdir))

            trace = runner._prepare_trace_context(session_id='session-123', step_index=8)

            self.assertEqual(trace['session_dir'], existing_session_dir)
            self.assertEqual(trace['trace_date'], '2026-04-24')
            self.assertEqual(trace['trace_time'], '10-20-30')
            self.assertEqual(trace['prompt_path'], existing_session_dir / 'step-008-prompt.txt')

    async def test_preflight_recovers_after_two_failures(self) -> None:
        settings = Settings(
            cdp_recovery_window_sec=0.2,
            cdp_recovery_interval_ms=1,
            codex_workdir='/tmp',
        )
        runner = AgentRunner(settings)

        processes = [
            _FakeProcess(returncode=1, stdout_text='{"ok":false,"error":"CDP_TRANSIENT_ERROR: Execution context was destroyed"}'),
            _FakeProcess(returncode=1, stdout_text='{"ok":false,"error":"CDP_TRANSIENT_ERROR: Target page, context or browser has been closed"}'),
            _FakeProcess(returncode=0, stdout_text='{"ok":true,"url":"https://lamoda.ru"}'),
        ]
        calls: list[tuple[Any, ...]] = []

        async def fake_create_subprocess_exec(*cmd: Any, **kwargs: Any) -> _FakeProcess:
            _ = kwargs
            calls.append(cmd)
            return processes.pop(0)

        with patch('buyer.app.runner.asyncio.create_subprocess_exec', new=fake_create_subprocess_exec):
            ok, summary = await runner._probe_browser_sidecar('http://browser:9223')

        self.assertTrue(ok)
        self.assertIn('recovered_after_retry=true', summary)
        self.assertIn('attempts=3', summary)
        self.assertIn('last_error_tail=CDP_TRANSIENT_ERROR', summary)
        self.assertTrue(all(cmd[-1] == 'url' for cmd in calls))

    async def test_transient_failure_does_not_finish_session_immediately(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='failed',
                message='CDP_TRANSIENT_ERROR: Execution context was destroyed',
                order_id=None,
                artifacts={},
            ),
            AgentOutput(
                status='completed',
                message='Шаг оплаты найден',
                order_id='order-123',
                artifacts={'source': 'test'},
            ),
        ])
        store = SessionStore(max_active_sessions=1)
        service = BuyerService(
            store=store,
            callback_client=callback_client,  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            novnc_url='http://novnc',
            default_callback_url='http://callback',
            cdp_recovery_window_sec=0.2,
            cdp_recovery_interval_ms=1,
            sberid_allowlist=set(),
            sberid_auth_retry_budget=1,
            auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
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
        self.assertEqual(final_state.status, SessionStatus.COMPLETED)
        self.assertEqual(runner.calls, 2)

        memory = await store.get_agent_memory(state.session_id)
        self.assertTrue(any('[CDP_RECOVERY_RESTART_FROM_START_URL]' in item['text'] for item in memory))

        scenario_finished_events = [event for event in final_state.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished_events), 1)
        self.assertEqual(scenario_finished_events[0].payload.get('status'), 'completed')

    async def test_purchase_script_completion_skips_generic_runner(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='failed',
                message='generic runner should not be called',
                order_id=None,
                artifacts={},
            ),
        ])
        store = SessionStore(max_active_sessions=1)
        service = BuyerService(
            store=store,
            callback_client=callback_client,  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            novnc_url='http://novnc',
            default_callback_url='http://callback',
            cdp_recovery_window_sec=0.2,
            cdp_recovery_interval_ms=1,
            sberid_allowlist=set(),
            sberid_auth_retry_budget=1,
            auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
            purchase_script_allowlist={'litres.ru'},
            purchase_script_runner=_CompletedPurchaseScriptRunner(),  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='Открой litres. Ищи книгу одиссея гомера',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref

        final_state = await store.get(state.session_id)
        self.assertEqual(final_state.status, SessionStatus.COMPLETED)
        self.assertEqual(runner.calls, 0)

        payment_ready_events = [event for event in final_state.events if event.event_type == 'payment_ready']
        self.assertEqual(len(payment_ready_events), 1)
        self.assertEqual(payment_ready_events[0].payload.get('order_id'), 'order-456')

    async def test_purchase_script_exception_falls_back_to_generic_runner(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Generic runner completed',
                order_id='order-789',
                artifacts={'source': 'generic'},
            ),
        ])
        store = SessionStore(max_active_sessions=1)
        service = BuyerService(
            store=store,
            callback_client=callback_client,  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            novnc_url='http://novnc',
            default_callback_url='http://callback',
            cdp_recovery_window_sec=0.2,
            cdp_recovery_interval_ms=1,
            sberid_allowlist=set(),
            sberid_auth_retry_budget=1,
            auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
            purchase_script_allowlist={'litres.ru'},
            purchase_script_runner=_ThrowingPurchaseScriptRunner(),  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='Открой litres. Ищи книгу одиссея гомера',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref

        final_state = await store.get(state.session_id)
        self.assertEqual(final_state.status, SessionStatus.COMPLETED)
        self.assertEqual(runner.calls, 1)

        memory = await store.get_agent_memory(state.session_id)
        self.assertTrue(any('[PURCHASE_SCRIPT_FALLBACK]' in item['text'] for item in memory))

    async def test_transient_failure_after_window_finishes_as_failed(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='failed',
                message='CDP_TRANSIENT_ERROR: Target page, context or browser has been closed',
                order_id=None,
                artifacts={},
            ),
            AgentOutput(
                status='failed',
                message='CDP_TRANSIENT_ERROR: Execution context was destroyed',
                order_id=None,
                artifacts={},
            ),
        ])
        store = SessionStore(max_active_sessions=1)
        service = BuyerService(
            store=store,
            callback_client=callback_client,  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            novnc_url='http://novnc',
            default_callback_url='http://callback',
            cdp_recovery_window_sec=0.001,
            cdp_recovery_interval_ms=2,
            sberid_allowlist=set(),
            sberid_auth_retry_budget=1,
            auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
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
        self.assertGreaterEqual(runner.calls, 2)

        scenario_finished_events = [event for event in final_state.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished_events), 1)
        payload = scenario_finished_events[0].payload
        self.assertEqual(payload.get('status'), 'failed')
        self.assertIn('Transient CDP-сбой не восстановился', payload.get('message', ''))
        self.assertIn('recovery', payload.get('artifacts', {}))
