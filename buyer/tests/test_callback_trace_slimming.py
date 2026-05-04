from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from buyer.app.models import AgentOutput
from buyer.app.models import EventEnvelope
from buyer.app.payment_verifier import verify_completed_payment
from buyer.app.runner import AgentRunner
from buyer.app.service import BuyerService
from buyer.app.service import _build_agent_step_payload
from buyer.app.settings import Settings
from buyer.app.state import SessionStore


REMOVED_TRACE_FIELDS = {
    'prompt_path',
    'prompt_preview',
    'stdout_tail',
    'stderr_tail',
    'browser_actions_log_path',
    'browser_actions_tail',
    'command_duration_ms',
    'inter_command_idle_ms',
    'browser_busy_union_ms',
    'post_browser_idle_ms',
    'top_idle_gaps',
    'command_errors',
}


class _RecordingCallbackClient:
    def __init__(self) -> None:
        self.delivered: list[EventEnvelope] = []

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
        return EventEnvelope(
            event_id=f'event-{seq}',
            session_id=session_id,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc),
            idempotency_key=f'{session_id}:{event_type}:{idempotency_suffix or seq}',
            payload=payload,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
        )

    async def deliver(self, callback_url: str, envelope: EventEnvelope, *, headers: dict[str, str] | None = None) -> None:
        _ = callback_url
        _ = headers
        self.delivered.append(envelope)


class _NoopRunner:
    async def run_step(self, **_: Any) -> AgentOutput:
        raise AssertionError('run_step не должен вызываться в этом тесте')


class _NoopAuthScriptRunner:
    def registry_snapshot(self) -> list[dict[str, str]]:
        return []


def _service(callback_client: _RecordingCallbackClient, store: SessionStore) -> BuyerService:
    return BuyerService(
        store=store,
        callback_client=callback_client,  # type: ignore[arg-type]
        runner=_NoopRunner(),  # type: ignore[arg-type]
        novnc_url='http://novnc',
        default_callback_url='http://callback',
        cdp_recovery_window_sec=0,
        cdp_recovery_interval_ms=1,
        sberid_allowlist=set(),
        auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
    )


def _legacy_trace_artifacts() -> dict[str, Any]:
    return {
        'trace': {
            'step': 3,
            'trace_date': '2026-04-30',
            'trace_time': '10-00-00',
            'prompt_path': '/tmp/private-prompt.txt',
            'prompt_sha256': 'b' * 64,
            'prompt_preview': 'prompt preview with private context',
            'trace_file': '/tmp/step-003-trace.json',
            'browser_actions_log_path': '/tmp/step-003-browser-actions.jsonl',
            'browser_actions_total': 2,
            'browser_actions_tail': [
                {'event': 'browser_command_started', 'command': 'html'},
                {
                    'event': 'browser_command_finished',
                    'command': 'html',
                    'result': {'html': '<main>secret checkout excerpt</main>'},
                },
            ],
            'duration_ms': 456,
            'command_duration_ms': 123,
            'inter_command_idle_ms': 50,
            'browser_busy_union_ms': 140,
            'post_browser_idle_ms': 20,
            'top_idle_gaps': [{'duration_ms': 50, 'after_command': 'links'}],
            'command_errors': [{'command': 'click', 'error': 'selector failed'}],
            'codex_tokens_used': 789,
            'codex_model': 'gpt-test',
            'model_strategy': 'single',
            'model_fallback_reason': 'process_failed',
            'codex_returncode': 1,
            'codex_attempts': [
                {
                    'role': 'single',
                    'model': 'gpt-test',
                    'status': 'failed',
                    'failure_reason': 'process_failed',
                    'duration_ms': 456,
                    'stdout_tail': 'stdout diagnostic tail',
                    'stderr_tail': 'stderr diagnostic tail',
                    'output_path': '/tmp/codex-output.json',
                }
            ],
            'stdout_tail': 'stdout diagnostic tail',
            'stderr_tail': 'stderr diagnostic tail',
        },
        'receipt_ref': {'path': '/tmp/receipt.json', 'kind': 'diagnostic'},
    }


def test_runner_returns_slim_trace_artifact_and_keeps_full_trace_file(tmp_path: Path) -> None:
    runner = AgentRunner(
        Settings(
            _env_file=None,
            buyer_trace_dir=str(tmp_path),
            buyer_browser_actions_tail=20,
        )
    )
    trace_context = runner._prepare_trace_context(session_id='session-trace-slim', step_index=2)
    trace_context['browser_actions_log_path'].write_text(
        '\n'.join(
            [
                json.dumps(
                    {
                        'event': 'browser_command_started',
                        'command': 'html',
                        'ts': '2026-04-30T10:00:00+00:00',
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        'event': 'browser_command_finished',
                        'command': 'html',
                        'ok': True,
                        'duration_ms': 123,
                        'ts': '2026-04-30T10:00:00.123000+00:00',
                        'result': {'html': '<main>secret checkout excerpt</main>', 'html_size': 2048},
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding='utf-8',
    )

    artifacts = runner._build_trace_artifacts(
        trace=trace_context,
        preflight_summary='OK',
        prompt_hash='a' * 64,
        prompt_preview='prompt preview with private context',
        command_for_log=['codex', 'exec'],
        output_path='/tmp/codex-output.json',
        stdout_text='stdout diagnostic tail',
        stderr_text='stderr diagnostic tail',
        codex_returncode=0,
        duration_ms=456,
        codex_model='gpt-test',
        codex_attempts=[
            {
                'role': 'single',
                'model': 'gpt-test',
                'status': 'completed',
                'duration_ms': 456,
                'output_path': '/tmp/codex-output.json',
            }
        ],
        model_strategy='single',
        fallback_reason=None,
    )

    callback_trace = artifacts['trace']
    assert callback_trace['step'] == 2
    assert callback_trace['prompt_sha256'] == 'a' * 64
    assert callback_trace['duration_ms'] == 456
    assert callback_trace['codex_model'] == 'gpt-test'
    assert callback_trace['browser_actions_total'] == 2
    assert callback_trace['trace_file'].endswith('step-002-trace.json')
    assert callback_trace['codex_attempts'] == [{'role': 'single', 'model': 'gpt-test', 'status': 'completed'}]
    assert REMOVED_TRACE_FIELDS.isdisjoint(callback_trace)

    full_trace = json.loads(Path(callback_trace['trace_file']).read_text(encoding='utf-8'))
    assert full_trace['prompt_preview'] == 'prompt preview with private context'
    assert full_trace['stdout_tail'] == 'stdout diagnostic tail'
    assert full_trace['stderr_tail'] == 'stderr diagnostic tail'
    assert full_trace['browser_actions_tail'][1]['result']['html'] == '<main>secret checkout excerpt</main>'
    assert full_trace['command_duration_ms'] == 123


def test_service_event_payload_slims_legacy_full_trace_artifact() -> None:
    result = AgentOutput(
        status='failed',
        message='step failed',
        artifacts={
            'trace': {
                'step': 3,
                'trace_date': '2026-04-30',
                'trace_time': '10-00-00',
                'prompt_path': '/tmp/private-prompt.txt',
                'prompt_sha256': 'b' * 64,
                'prompt_preview': 'prompt preview with private context',
                'trace_file': '/tmp/step-003-trace.json',
                'browser_actions_log_path': '/tmp/step-003-browser-actions.jsonl',
                'browser_actions_total': 2,
                'browser_actions_tail': [
                    {'event': 'browser_command_started', 'command': 'html'},
                    {
                        'event': 'browser_command_finished',
                        'command': 'html',
                        'result': {'html': '<main>secret checkout excerpt</main>'},
                    },
                ],
                'duration_ms': 456,
                'command_duration_ms': 123,
                'inter_command_idle_ms': 50,
                'browser_busy_union_ms': 140,
                'post_browser_idle_ms': 20,
                'top_idle_gaps': [{'duration_ms': 50, 'after_command': 'links'}],
                'command_errors': [{'command': 'click', 'error': 'selector failed'}],
                'codex_tokens_used': 789,
                'codex_model': 'gpt-test',
                'model_strategy': 'single',
                'model_fallback_reason': 'process_failed',
                'codex_returncode': 1,
                'codex_attempts': [
                    {
                        'role': 'single',
                        'model': 'gpt-test',
                        'status': 'failed',
                        'failure_reason': 'process_failed',
                        'duration_ms': 456,
                        'stdout_tail': 'stdout diagnostic tail',
                        'stderr_tail': 'stderr diagnostic tail',
                        'output_path': '/tmp/codex-output.json',
                    }
                ],
                'stdout_tail': 'stdout diagnostic tail',
                'stderr_tail': 'stderr diagnostic tail',
            }
        },
    )

    payload = _build_agent_step_payload(step_index=3, result=result)

    trace = payload['trace']
    assert trace == {
        'step': 3,
        'trace_date': '2026-04-30',
        'trace_time': '10-00-00',
        'prompt_sha256': 'b' * 64,
        'trace_file': '/tmp/step-003-trace.json',
        'browser_actions_total': 2,
        'duration_ms': 456,
        'codex_tokens_used': 789,
        'codex_model': 'gpt-test',
        'model_strategy': 'single',
        'model_fallback_reason': 'process_failed',
        'codex_returncode': 1,
        'codex_attempts': [
            {
                'role': 'single',
                'model': 'gpt-test',
                'status': 'failed',
                'failure_reason': 'process_failed',
            }
        ],
    }
    assert REMOVED_TRACE_FIELDS.isdisjoint(trace)


def test_failed_scenario_finished_artifacts_slim_legacy_full_trace() -> None:
    async def run() -> None:
        callback_client = _RecordingCallbackClient()
        store = SessionStore(max_active_sessions=1)
        service = _service(callback_client, store)
        state = await store.create_session(
            task='test',
            start_url='https://brandshop.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )

        await service._handle_failed(
            state,
            'failed with trace',
            _legacy_trace_artifacts(),
            auth_summary=None,
        )

        scenario_finished = callback_client.delivered[-1]
        assert scenario_finished.event_type == 'scenario_finished'
        callback_artifacts = scenario_finished.payload['artifacts']
        assert callback_artifacts['receipt_ref'] == {'path': '/tmp/receipt.json', 'kind': 'diagnostic'}
        assert callback_artifacts['trace'] == {
            'step': 3,
            'trace_date': '2026-04-30',
            'trace_time': '10-00-00',
            'prompt_sha256': 'b' * 64,
            'trace_file': '/tmp/step-003-trace.json',
            'browser_actions_total': 2,
            'duration_ms': 456,
            'codex_tokens_used': 789,
            'codex_model': 'gpt-test',
            'model_strategy': 'single',
            'model_fallback_reason': 'process_failed',
            'codex_returncode': 1,
            'codex_attempts': [
                {
                    'role': 'single',
                    'model': 'gpt-test',
                    'status': 'failed',
                    'failure_reason': 'process_failed',
                }
            ],
        }
        assert REMOVED_TRACE_FIELDS.isdisjoint(callback_artifacts['trace'])

    asyncio.run(run())


def test_completed_scenario_finished_artifacts_slim_legacy_full_trace() -> None:
    async def run() -> None:
        callback_client = _RecordingCallbackClient()
        store = SessionStore(max_active_sessions=1)
        service = _service(callback_client, store)
        state = await store.create_session(
            task='test',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )

        result = AgentOutput(
            status='completed',
            message='payment boundary reached',
            order_id='order-123',
            payment_evidence={
                'source': 'litres_payecom_iframe',
                'url': 'https://payecom.ru/pay_ru?orderId=order-123',
            },
            artifacts=_legacy_trace_artifacts(),
        )
        await service._handle_completed(
            state,
            result,
            auth_summary=None,
            payment_verification=verify_completed_payment(state.start_url, result),
        )

        scenario_finished = callback_client.delivered[-1]
        assert scenario_finished.event_type == 'scenario_finished'
        callback_artifacts = scenario_finished.payload['artifacts']
        assert callback_artifacts['receipt_ref'] == {'path': '/tmp/receipt.json', 'kind': 'diagnostic'}
        assert callback_artifacts['payment_evidence'] == {
            'source': 'litres_payecom_iframe',
            'url': 'https://payecom.ru/pay_ru?orderId=order-123',
        }
        assert callback_artifacts['trace']['trace_file'] == '/tmp/step-003-trace.json'
        assert callback_artifacts['trace']['prompt_sha256'] == 'b' * 64
        assert REMOVED_TRACE_FIELDS.isdisjoint(callback_artifacts['trace'])

    asyncio.run(run())

def test_callback_openapi_schema_excludes_removed_trace_fields_and_requires_order_id_host() -> None:
    schema_text = (Path(__file__).parents[2] / 'docs' / 'callbacks.openapi.yaml').read_text(encoding='utf-8')
    trace_summary = _schema_block(schema_text, 'TraceSummary')
    payment_ready = _schema_block(schema_text, 'PaymentReadyPayload')

    for field in REMOVED_TRACE_FIELDS:
        assert re.search(rf'^\s+{re.escape(field)}:', trace_summary, flags=re.MULTILINE) is None

    assert 'trace_file:' in trace_summary
    assert 'browser_actions_total:' in trace_summary
    assert re.search(r'^\s+- order_id_host$', payment_ready, flags=re.MULTILINE) is not None
    assert re.search(r'^\s+order_id_host:', payment_ready, flags=re.MULTILINE) is not None


def _schema_block(schema_text: str, schema_name: str) -> str:
    match = re.search(
        rf'^\s{{4}}{re.escape(schema_name)}:\n(?P<body>.*?)(?=^\s{{4}}[A-Za-z0-9_]+:\n|\Z)',
        schema_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group('body')
