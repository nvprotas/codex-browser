from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from buyer.app.models import AgentOutput, EventEnvelope, SessionStatus
from buyer.app.runner import AgentRunner
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


class CDPRecoveryTests(unittest.IsolatedAsyncioTestCase):
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
