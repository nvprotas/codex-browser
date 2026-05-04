from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from buyer.app.models import AgentOutput, EventEnvelope, SessionStatus
from buyer.app.runner import (
    AgentRunner,
    _AgentStreamPublisher,
    _collect_process_streams,
    _read_process_stream,
    _trace_date_dir_name,
)
from buyer.app.service import BuyerService, _looks_like_transient_cdp_failure
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


class _FakeLineReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b''
        await asyncio.sleep(0)
        return self._lines.pop(0)


class _StreamingFakeProcess:
    def __init__(self, *, returncode: int, stdout_lines: list[bytes], stderr_lines: list[bytes]) -> None:
        self.returncode = returncode
        self.stdout = _FakeLineReader(stdout_lines)
        self.stderr = _FakeLineReader(stderr_lines)

    async def wait(self) -> int:
        await asyncio.sleep(0)
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        stdout = b''.join(self.stdout._lines)
        stderr = b''.join(self.stderr._lines)
        self.stdout._lines.clear()
        self.stderr._lines.clear()
        return stdout, stderr

    def kill(self) -> None:
        self.returncode = -9


class _BlockingLineReader:
    def __init__(self) -> None:
        self.cancelled = False

    async def readline(self) -> bytes:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return b''


class _BlockingStreamingProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.stdout = _BlockingLineReader()
        self.stderr = _BlockingLineReader()

    async def wait(self) -> int:
        await asyncio.sleep(3600)
        return 0


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
        self.headers: list[dict[str, str] | None] = []

    def build_envelope(
        self,
        session_id: str,
        event_type: str,
        payload: dict,
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


class _FailingStreamCallbackClient(_RecordingCallbackClient):
    async def deliver(self, callback_url: str, envelope: EventEnvelope, *, headers: dict[str, str] | None = None) -> None:
        if envelope.event_type == 'agent_stream_event':
            from buyer.app.callback import CallbackDeliveryError

            raise CallbackDeliveryError('stream receiver unavailable')
        await super().deliver(callback_url, envelope, headers=headers)


class _NoopAuthScriptRunner:
    def registry_snapshot(self) -> list[dict[str, str]]:
        return []

    async def run(self, **_: Any) -> Any:
        return None


class _RecordingKnowledgeAnalyzer:
    def __init__(self, *, callback_client: _RecordingCallbackClient | None = None, fail: bool = False) -> None:
        self.callback_client = callback_client
        self.fail = fail
        self.snapshots: list[Any] = []
        self.delivered_event_types_at_start: list[str] = []

    async def analyze(self, snapshot: Any) -> dict[str, Any]:
        self.snapshots.append(snapshot)
        if self.callback_client is not None:
            self.delivered_event_types_at_start = [event.event_type for event in self.callback_client.delivered]
        if self.fail:
            raise RuntimeError('analysis failed')
        return {'status': 'completed'}


class CDPRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_store_prunes_terminal_sessions_by_ttl(self) -> None:
        now = datetime.now(timezone.utc)

        def clock() -> datetime:
            return now

        store = SessionStore(max_active_sessions=1, status_ttl_sec=1, clock=clock)
        state = await store.create_session(
            task='test',
            start_url='https://example.com',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )
        await store.set_status(state.session_id, SessionStatus.COMPLETED)

        now += timedelta(seconds=2)

        self.assertEqual((await store.get(state.session_id)).status, SessionStatus.COMPLETED)

        self.assertEqual(await store.list_sessions(), [])

    async def test_transient_cdp_detection_scans_bounded_nested_artifacts(self) -> None:
        artifacts = {
            'large_html': '<html>' + ('x' * 20_000) + '</html>',
            'nested': [{'error': 'CDP_TRANSIENT_ERROR: Target page, context or browser has been closed'}],
        }

        self.assertTrue(_looks_like_transient_cdp_failure('', artifacts))

    async def test_transient_cdp_detection_keeps_prefix_of_long_nested_artifacts(self) -> None:
        artifacts = {
            'nested': {
                'error': 'CDP_TRANSIENT_ERROR: Target page, context or browser has been closed ' + ('x' * 20_000),
            },
        }

        self.assertTrue(_looks_like_transient_cdp_failure('', artifacts))

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

    async def test_run_step_uses_single_model_and_records_trace_metrics(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = Settings(
                buyer_trace_dir=tmpdir,
                codex_workdir=tmpdir,
                codex_model='gpt-test',
            )
            runner = AgentRunner(settings)
            calls: list[tuple[Any, ...]] = []
            attempt_env_ids: list[str] = []

            async def fake_probe(*_: Any, **__: Any) -> tuple[bool, str]:
                return True, 'OK'

            async def fake_create_subprocess_exec(*cmd: Any, **kwargs: Any) -> _FakeProcess:
                calls.append(cmd)
                attempt_env_ids.append(kwargs['env']['BUYER_CODEX_ATTEMPT_ID'])
                output_path = Path(cmd[cmd.index('-o') + 1])
                output_path.write_text(
                    json.dumps({'status': 'completed', 'message': 'ok', 'order_id': None, 'artifacts': {}}),
                    encoding='utf-8',
                )
                return _FakeProcess(returncode=0, stderr_text='tokens used 123')

            with (
                patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}),
                patch.object(runner, '_probe_browser_sidecar', new=fake_probe),
                patch('buyer.app.runner.asyncio.create_subprocess_exec', new=fake_create_subprocess_exec),
            ):
                result = await runner.run_step(
                    session_id='session-123',
                    step_index=1,
                    task='test-task',
                    start_url='https://example.com',
                    metadata={},
                    auth=None,
                    auth_context=None,
                    memory=[],
                    latest_user_reply=None,
                )
                full_trace = json.loads(Path(result.artifacts['trace']['trace_file']).read_text(encoding='utf-8'))
                output_path = Path(full_trace['codex_output_path'])
                output_path_exists = output_path.is_file()
                output_status = json.loads(output_path.read_text(encoding='utf-8'))['status']

        self.assertEqual(result.status, 'completed')
        self.assertEqual(len(calls), 1)
        self.assertIn('gpt-test', calls[0])
        trace = result.artifacts['trace']
        self.assertEqual(trace['model_strategy'], 'single')
        self.assertEqual(trace['codex_model'], 'gpt-test')
        self.assertEqual(trace['codex_tokens_used'], 123)
        self.assertRegex(attempt_env_ids[0], r'^step-001-single-[0-9a-f]{8}$')
        self.assertEqual(trace['codex_attempts'][0]['attempt_id'], attempt_env_ids[0])
        self.assertEqual(trace['codex_attempts'][0]['role'], 'single')
        self.assertTrue(output_path_exists)
        self.assertEqual(output_status, 'completed')

    async def test_run_step_streams_codex_stdout_json_and_browser_actions(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = Settings(
                buyer_trace_dir=tmpdir,
                codex_workdir=tmpdir,
                codex_model='gpt-test',
            )
            runner = AgentRunner(settings)
            stream_events: list[dict[str, Any]] = []

            async def fake_probe(*_: Any, **__: Any) -> tuple[bool, str]:
                return True, 'OK'

            async def fake_create_subprocess_exec(*cmd: Any, **kwargs: Any) -> _StreamingFakeProcess:
                output_path = Path(cmd[cmd.index('-o') + 1])
                output_path.write_text(
                    json.dumps({'status': 'completed', 'message': 'ok', 'order_id': None, 'artifacts': {}}),
                    encoding='utf-8',
                )
                actions_path = Path(kwargs['env']['BUYER_CDP_ACTIONS_LOG_PATH'])
                actions_path.write_text(
                    json.dumps(
                        {
                            'event': 'browser_command_finished',
                            'command': 'goto',
                            'ok': True,
                            'duration_ms': 10,
                        }
                    )
                    + '\n',
                    encoding='utf-8',
                )
                return _StreamingFakeProcess(
                    returncode=0,
                    stdout_lines=[json.dumps({'type': 'agent_message', 'message': 'Ищу товар'}).encode() + b'\n'],
                    stderr_lines=[b'progress line\n'],
                )

            async def stream_callback(payload: dict[str, Any]) -> None:
                stream_events.append(payload)

            with (
                patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}),
                patch.object(runner, '_probe_browser_sidecar', new=fake_probe),
                patch('buyer.app.runner.asyncio.create_subprocess_exec', new=fake_create_subprocess_exec),
            ):
                result = await runner.run_step(
                    session_id='session-stream',
                    step_index=1,
                    task='test-task',
                    start_url='https://example.com',
                    metadata={},
                    auth=None,
                    auth_context=None,
                    memory=[],
                    latest_user_reply=None,
                    stream_callback=stream_callback,
                )

        self.assertEqual(result.status, 'completed')
        self.assertTrue(any(item['source'] == 'codex' and item['stream'] == 'codex_json' for item in stream_events))
        self.assertTrue(any(item['source'] == 'codex' and item['stream'] == 'stderr' for item in stream_events))
        self.assertTrue(any(item['source'] == 'browser' and item['stream'] == 'browser_actions' for item in stream_events))
        self.assertTrue(all(isinstance(item.get('items'), list) and item['items'] for item in stream_events))

    async def test_stream_reader_accepts_single_codex_json_line_longer_than_asyncio_limit(self) -> None:
        oversized_line = json.dumps(
            {
                'type': 'item.completed',
                'item': {
                    'type': 'command_execution',
                    'aggregated_output': 'x' * 120_000,
                },
            }
        )
        reader = asyncio.StreamReader(limit=64)
        reader.feed_data(oversized_line.encode('utf-8') + b'\n')
        reader.feed_eof()
        chunks: list[str] = []
        stream_events: list[dict[str, Any]] = []

        async def stream_callback(payload: dict[str, Any]) -> None:
            stream_events.append(payload)

        publisher = _AgentStreamPublisher(
            session_id='session-long-line',
            step_index=1,
            callback=stream_callback,
        )

        await _read_process_stream(
            reader,
            source='codex',
            stream='stdout',
            chunks=chunks,
            publisher=publisher,
        )
        await publisher.aclose()

        self.assertEqual(''.join(chunks), oversized_line + '\n')
        self.assertTrue(any(event['stream'] == 'codex_json' for event in stream_events))
        completed_event = next(event for event in stream_events if event['stream'] == 'codex_json')
        output = completed_event['items'][0]['item']['aggregated_output']
        self.assertLess(len(output), len(oversized_line))
        self.assertIn('[truncated stream text:', output)

    async def test_collect_process_streams_cleans_reader_tasks_on_cancellation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            process = _BlockingStreamingProcess()
            publisher = _AgentStreamPublisher(
                session_id='session-cancel',
                step_index=1,
                callback=None,
            )
            task = asyncio.create_task(
                _collect_process_streams(
                    process,
                    publisher=publisher,
                    browser_actions_log_path=Path(tmpdir) / 'actions.jsonl',
                    browser_actions_offset=0,
                )
            )
            await asyncio.sleep(0)
            task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.stdout.cancelled)
        self.assertTrue(process.stderr.cancelled)

    async def test_run_step_reports_agent_failure_without_fallback_attempt(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = Settings(
                buyer_trace_dir=tmpdir,
                codex_workdir=tmpdir,
                codex_model='gpt-strong',
            )
            runner = AgentRunner(settings)
            calls: list[tuple[Any, ...]] = []

            async def fake_probe(*_: Any, **__: Any) -> tuple[bool, str]:
                return True, 'OK'

            async def fake_create_subprocess_exec(*cmd: Any, **kwargs: Any) -> _FakeProcess:
                _ = kwargs
                calls.append(cmd)
                output_path = Path(cmd[cmd.index('-o') + 1])
                output_path.write_text(
                    json.dumps({'status': 'failed', 'message': 'agent stopped on checkout', 'order_id': None, 'artifacts': {}}),
                    encoding='utf-8',
                )
                return _FakeProcess(returncode=0, stderr_text=f'tokens used {len(calls) * 10}')

            with (
                patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}),
                patch.object(runner, '_probe_browser_sidecar', new=fake_probe),
                patch('buyer.app.runner.asyncio.create_subprocess_exec', new=fake_create_subprocess_exec),
            ):
                result = await runner.run_step(
                    session_id='session-456',
                    step_index=1,
                    task='test-task',
                    start_url='https://example.com',
                    metadata={},
                    auth=None,
                    auth_context=None,
                    memory=[],
                    latest_user_reply=None,
                )
                full_trace = json.loads(Path(result.artifacts['trace']['trace_file']).read_text(encoding='utf-8'))
                full_attempt = full_trace['codex_attempts'][0]
                trace_output_path_exists = Path(full_trace['codex_output_path']).is_file()
                attempt_output_path_exists = Path(full_attempt['output_path']).is_file()

        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.message, 'agent stopped on checkout')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('gpt-mini', calls[0])
        self.assertIn('gpt-strong', calls[0])
        trace = result.artifacts['trace']
        self.assertEqual(trace['model_strategy'], 'single')
        self.assertEqual(trace['model_fallback_reason'], 'agent_reported_failed')
        self.assertEqual(len(trace['codex_attempts']), 1)
        self.assertRegex(trace['codex_attempts'][0]['attempt_id'], r'^step-001-single-[0-9a-f]{8}$')
        self.assertEqual(trace['codex_attempts'][0]['role'], 'single')
        self.assertEqual(trace['codex_attempts'][0]['model'], 'gpt-strong')
        self.assertEqual(trace['codex_attempts'][0]['status'], 'failed')
        self.assertEqual(trace['codex_attempts'][0]['failure_reason'], 'agent_reported_failed')
        self.assertEqual(trace['codex_attempts'][0]['failure_message'], 'agent stopped on checkout')
        self.assertEqual(full_attempt['failure_reason'], 'agent_reported_failed')
        self.assertEqual(full_attempt['failure_message'], 'agent stopped on checkout')
        self.assertTrue(trace_output_path_exists)
        self.assertTrue(attempt_output_path_exists)
        self.assertEqual(trace['codex_tokens_used'], 10)

    async def test_run_step_does_not_retry_same_model_when_failed_without_browser_mutation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = Settings(
                buyer_trace_dir=tmpdir,
                codex_workdir=tmpdir,
                codex_model='gpt-same',
            )
            runner = AgentRunner(settings)
            calls: list[tuple[Any, ...]] = []

            async def fake_probe(*_: Any, **__: Any) -> tuple[bool, str]:
                return True, 'OK'

            async def fake_create_subprocess_exec(*cmd: Any, **kwargs: Any) -> _FakeProcess:
                calls.append(cmd)
                actions_path = Path(kwargs['env']['BUYER_CDP_ACTIONS_LOG_PATH'])
                if len(calls) == 1:
                    actions_path.write_text(
                        json.dumps({'event': 'browser_command_finished', 'command': 'url', 'ok': True})
                        + '\n'
                        + json.dumps({'event': 'browser_command_finished', 'command': 'snapshot', 'ok': True})
                        + '\n',
                        encoding='utf-8',
                    )
                output_path = Path(cmd[cmd.index('-o') + 1])
                status = 'failed' if len(calls) == 1 else 'completed'
                output_path.write_text(
                    json.dumps({'status': status, 'message': status, 'order_id': None, 'artifacts': {}}),
                    encoding='utf-8',
                )
                return _FakeProcess(returncode=0, stderr_text=f'tokens used {len(calls) * 10}')

            with (
                patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}),
                patch.object(runner, '_probe_browser_sidecar', new=fake_probe),
                patch('buyer.app.runner.asyncio.create_subprocess_exec', new=fake_create_subprocess_exec),
            ):
                result = await runner.run_step(
                    session_id='session-same-clean',
                    step_index=1,
                    task='test-task',
                    start_url='https://example.com',
                    metadata={},
                    auth=None,
                    auth_context=None,
                    memory=[],
                    latest_user_reply=None,
                )

        self.assertEqual(result.status, 'failed')
        self.assertEqual(len(calls), 1)
        self.assertIn('gpt-same', calls[0])
        trace = result.artifacts['trace']
        self.assertEqual([item['role'] for item in trace['codex_attempts'] if 'role' in item], ['single'])
        self.assertEqual(trace['model_fallback_reason'], 'agent_reported_failed')

    async def test_run_step_does_not_retry_same_model_when_failed_after_browser_mutation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = Settings(
                buyer_trace_dir=tmpdir,
                codex_workdir=tmpdir,
                codex_model='gpt-same',
            )
            runner = AgentRunner(settings)
            calls: list[tuple[Any, ...]] = []

            async def fake_probe(*_: Any, **__: Any) -> tuple[bool, str]:
                return True, 'OK'

            async def fake_create_subprocess_exec(*cmd: Any, **kwargs: Any) -> _FakeProcess:
                calls.append(cmd)
                Path(kwargs['env']['BUYER_CDP_ACTIONS_LOG_PATH']).write_text(
                    json.dumps({'event': 'browser_command_finished', 'command': 'click', 'ok': True}) + '\n',
                    encoding='utf-8',
                )
                output_path = Path(cmd[cmd.index('-o') + 1])
                output_path.write_text(
                    json.dumps({'status': 'failed', 'message': 'stop', 'order_id': None, 'artifacts': {}}),
                    encoding='utf-8',
                )
                return _FakeProcess(returncode=0, stderr_text='tokens used 10')

            with (
                patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}),
                patch.object(runner, '_probe_browser_sidecar', new=fake_probe),
                patch('buyer.app.runner.asyncio.create_subprocess_exec', new=fake_create_subprocess_exec),
            ):
                result = await runner.run_step(
                    session_id='session-same-dirty',
                    step_index=1,
                    task='test-task',
                    start_url='https://example.com',
                    metadata={},
                    auth=None,
                    auth_context=None,
                    memory=[],
                    latest_user_reply=None,
                )

        self.assertEqual(result.status, 'failed')
        self.assertEqual(len(calls), 1)

    async def test_stream_event_delivery_failure_is_best_effort_and_saved(self) -> None:
        callback_client = _FailingStreamCallbackClient()
        store = SessionStore(max_active_sessions=1)
        service = BuyerService(
            store=store,
            callback_client=callback_client,
            runner=_SequenceRunner([]),
            novnc_url='http://novnc',
            default_callback_url='http://callback',
            cdp_recovery_window_sec=0,
            cdp_recovery_interval_ms=1,
            sberid_allowlist=set(),
            sberid_auth_retry_budget=0,
            auth_script_runner=_NoopAuthScriptRunner(),
        )
        state = await store.create_session(
            task='test',
            start_url='https://example.com',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )

        await service._emit_stream_event_best_effort(
            state,
            {
                'step': 1,
                'source': 'codex',
                'stream': 'codex_json',
                'sequence': 1,
                'items': [{'type': 'agent_message'}],
                'message': 'agent_message',
            },
        )

        refreshed = await store.get(state.session_id)
        self.assertEqual([event.event_type for event in refreshed.events], ['agent_stream_event'])
        self.assertEqual(refreshed.events[0].payload['source'], 'codex')

    async def test_callbacks_include_eval_ids_from_task_metadata(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Шаг оплаты найден',
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
            cdp_recovery_window_sec=0,
            cdp_recovery_interval_ms=1,
            sberid_allowlist=set(),
            sberid_auth_retry_budget=0,
            auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='Купить книгу',
            start_url='https://example.test',
            callback_url='http://callback',
            metadata={'eval_run_id': 'eval-run-001', 'eval_case_id': 'case-a'},
            auth=None,
        )
        await state.task_ref

        event = callback_client.delivered[0]
        self.assertEqual(event.event_type, 'session_started')
        self.assertEqual(event.eval_run_id, 'eval-run-001')
        self.assertEqual(event.eval_case_id, 'case-a')
        self.assertEqual(event.payload['message'], 'Сессия buyer запущена. Задача: Купить книгу')

    async def test_callback_token_is_sent_as_header_and_not_kept_after_session(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='failed',
                message='stop',
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
            cdp_recovery_window_sec=0,
            cdp_recovery_interval_ms=1,
            sberid_allowlist=set(),
            sberid_auth_retry_budget=0,
            auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='test',
            start_url='https://example.test',
            callback_url='http://callback',
            metadata={},
            auth=None,
            callback_token='callback-secret',
        )
        await state.task_ref

        self.assertTrue(callback_client.headers)
        self.assertTrue(
            all(headers == {'X-Eval-Callback-Token': 'callback-secret'} for headers in callback_client.headers)
        )
        self.assertEqual(service._callback_tokens, {})

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
                payment_evidence={
                    'source': 'litres_payecom_iframe',
                    'url': 'https://payecom.ru/pay_ru?orderId=order-123',
                },
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
            start_url='https://www.litres.ru/',
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

    async def test_completed_session_schedules_knowledge_analysis_after_scenario_finished(self) -> None:
        callback_client = _RecordingCallbackClient()
        analyzer = _RecordingKnowledgeAnalyzer(callback_client=callback_client)
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Шаг оплаты найден',
                order_id='order-123',
                payment_evidence={
                    'source': 'litres_payecom_iframe',
                    'url': 'https://payecom.ru/pay_ru?orderId=order-123',
                },
                artifacts={'trace': {'trace_file': '/tmp/trace.json'}},
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
            knowledge_analyzer=analyzer,  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='test-task',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref
        await service.wait_for_post_session_analysis()

        self.assertEqual(len(analyzer.snapshots), 1)
        self.assertEqual(analyzer.snapshots[0].outcome, 'completed')
        self.assertIn('scenario_finished', analyzer.delivered_event_types_at_start)
        self.assertEqual(callback_client.delivered[-1].event_type, 'scenario_finished')
        self.assertNotIn('knowledge_analysis_finished', [event.event_type for event in callback_client.delivered])

    async def test_knowledge_analysis_failure_does_not_change_completed_status(self) -> None:
        callback_client = _RecordingCallbackClient()
        analyzer = _RecordingKnowledgeAnalyzer(fail=True)
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Шаг оплаты найден',
                order_id='order-123',
                payment_evidence={
                    'source': 'litres_payecom_iframe',
                    'url': 'https://payecom.ru/pay_ru?orderId=order-123',
                },
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
            knowledge_analyzer=analyzer,  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='test-task',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref
        await service.wait_for_post_session_analysis()

        final_state = await store.get(state.session_id)
        self.assertEqual(final_state.status, SessionStatus.COMPLETED)
        scenario_finished_events = [event for event in final_state.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished_events), 1)

    async def test_knowledge_analysis_failure_does_not_change_failed_status(self) -> None:
        callback_client = _RecordingCallbackClient()
        analyzer = _RecordingKnowledgeAnalyzer(fail=True)
        runner = _SequenceRunner([
            AgentOutput(
                status='failed',
                message='Не удалось найти товар',
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
            knowledge_analyzer=analyzer,  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='test-task',
            start_url='https://brandshop.ru/',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref
        await service.wait_for_post_session_analysis()

        final_state = await store.get(state.session_id)
        self.assertEqual(final_state.status, SessionStatus.FAILED)
        scenario_finished_events = [event for event in final_state.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished_events), 1)
        self.assertEqual(scenario_finished_events[0].payload.get('status'), 'failed')

    async def test_unverified_payment_boundary_finishes_without_completed_analysis(self) -> None:
        callback_client = _RecordingCallbackClient()
        analyzer = _RecordingKnowledgeAnalyzer(callback_client=callback_client)
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Found YooMoney contract URL',
                order_id='unknown-order-123',
                payment_evidence={
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=unknown-order-123',
                },
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
            knowledge_analyzer=analyzer,  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='test-task',
            start_url='https://example-shop.test/',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref
        await service.wait_for_post_session_analysis()

        final_state = await store.get(state.session_id)
        self.assertEqual(final_state.status, SessionStatus.UNVERIFIED)
        self.assertEqual(analyzer.snapshots, [])
        self.assertEqual([event.event_type for event in final_state.events if event.event_type == 'payment_ready'], [])
        scenario_finished_events = [event for event in final_state.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished_events), 1)
        self.assertEqual(scenario_finished_events[0].payload.get('status'), 'unverified')

    async def test_litres_uses_generic_runner_with_default_purchase_settings(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Generic runner дошел до Litres SberPay iframe',
                order_id='order-789',
                payment_evidence={
                    'source': 'litres_payecom_iframe',
                    'url': 'https://payecom.ru/pay_ru?orderId=order-789',
                },
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

        payment_ready_events = [event for event in final_state.events if event.event_type == 'payment_ready']
        self.assertEqual(len(payment_ready_events), 1)
        self.assertEqual(payment_ready_events[0].payload.get('order_id'), 'order-789')

    async def test_litres_generic_runner_rejects_non_exact_payecom_payment_evidence(self) -> None:
        cases = [
            'http://payecom.ru/pay_ru?orderId=order-456',
            'https://evil.payecom.ru/pay_ru?orderId=order-456',
            'https://payecom.ru/pay_ru_malicious?orderId=order-456',
            'https://payecom.ru/pay_ru?orderId=other-order',
        ]

        for frame_src in cases:
            with self.subTest(frame_src=frame_src):
                callback_client = _RecordingCallbackClient()
                runner = _SequenceRunner([
                    AgentOutput(
                        status='completed',
                        message='Generic runner дошел до Litres payment boundary',
                        order_id='order-456',
                        payment_evidence={
                            'source': 'litres_payecom_iframe',
                            'url': frame_src,
                        },
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
                self.assertEqual(final_state.status, SessionStatus.FAILED)
                self.assertEqual(runner.calls, 1)
                self.assertEqual([event.event_type for event in final_state.events if event.event_type == 'payment_ready'], [])
                scenario_finished_events = [event for event in final_state.events if event.event_type == 'scenario_finished']
                self.assertEqual(len(scenario_finished_events), 1)
                self.assertEqual(scenario_finished_events[0].payload.get('status'), 'failed')

    async def test_litres_completed_without_order_id_finishes_as_failed(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Дошел только до checkout без SberPay orderId',
                order_id=None,
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
        self.assertEqual(final_state.status, SessionStatus.FAILED)
        scenario_finished_events = [event for event in final_state.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished_events), 1)
        self.assertEqual(scenario_finished_events[0].payload.get('status'), 'failed')
        self.assertIn('order_id', scenario_finished_events[0].payload.get('message', ''))

    async def test_brandshop_completed_with_order_id_without_valid_evidence_is_not_success(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Дошел до неизвестного платежного шага',
                order_id='order-unsupported',
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
        )

        state = await service.create_session(
            task='Купить товар и дойти до SberPay',
            start_url='https://brandshop.ru/',
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref

        final_state = await store.get(state.session_id)
        self.assertEqual(final_state.status, SessionStatus.FAILED)
        self.assertEqual([event.event_type for event in final_state.events if event.event_type == 'payment_ready'], [])
        scenario_finished_events = [event for event in final_state.events if event.event_type == 'scenario_finished']
        self.assertEqual(len(scenario_finished_events), 1)
        self.assertEqual(scenario_finished_events[0].payload.get('status'), 'failed')
        self.assertIn('Brandshop completed result rejected', scenario_finished_events[0].payload.get('message', ''))

    async def test_profile_updates_are_appended_to_user_info_file(self) -> None:
        callback_client = _RecordingCallbackClient()
        runner = _SequenceRunner([
            AgentOutput(
                status='completed',
                message='Дошел до шага оплаты',
                order_id='order-900',
                profile_updates=[
                    'Предпочитает электронные книги',
                    'Бюджет на книги до 1500 рублей',
                ],
                artifacts={},
            ),
        ])

        with TemporaryDirectory() as tmpdir:
            user_info_path = Path(tmpdir) / 'user-buyer-info.md'
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
                buyer_user_info_path=str(user_info_path),
            )

            state = await service.create_session(
                task='Открой litres. Ищи книгу одиссея гомера',
                start_url='https://www.litres.ru/',
                callback_url='http://callback',
                metadata={},
                auth=None,
            )
            await state.task_ref

            self.assertEqual(
                user_info_path.read_text(encoding='utf-8'),
                '- Предпочитает электронные книги\n- Бюджет на книги до 1500 рублей\n',
            )

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
