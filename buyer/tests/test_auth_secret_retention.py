from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from buyer.app.auth_scripts import (
    AUTH_FAILED_INVALID_SESSION,
    AUTH_OK,
    AUTH_REFRESH_REQUESTED,
    AuthScriptResult,
    SberIdScriptRunner,
)
from buyer.app.models import EventEnvelope, SessionStatus, TaskAuthPayload
from buyer.app.service import BuyerService
from buyer.app.state import SessionState, SessionStore


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
        _ = headers
        self.delivered.append(envelope)


class _UnusedRunner:
    async def run_step(self, **_: Any) -> None:
        raise AssertionError('generic runner не должен запускаться в auth unit test')


class _SuccessfulAuthScriptRunner:
    def __init__(self) -> None:
        self.seen_storage_state: dict[str, Any] | None = None

    def registry_snapshot(self) -> list[dict[str, str]]:
        return [{'domain': 'litres.ru', 'lifecycle': 'publish', 'script': 'sberid/litres.ts'}]

    async def run(self, **kwargs: Any) -> AuthScriptResult:
        self.seen_storage_state = kwargs['storage_state']
        return AuthScriptResult(
            status='completed',
            reason_code=AUTH_OK,
            message='ok',
            artifacts={'context_prepared_for_reuse': True},
        )


class _FakeConn:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.executemany_calls: list[tuple[str, list[tuple[Any, ...]]]] = []

    async def fetch(self, *_: Any) -> list[Any]:
        return []

    async def execute(self, sql: str, *args: Any) -> None:
        self.execute_calls.append((sql, args))

    async def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.executemany_calls.append((sql, rows))


def _raw_storage_state_reply() -> str:
    return json.dumps(
        {
            'storageState': {
                'cookies': [{'name': 'sid', 'value': 'cookie-secret-value'}],
                'origins': [
                    {
                        'origin': 'https://www.litres.ru',
                        'localStorage': [{'name': 'token', 'value': 'local-storage-token-secret'}],
                    }
                ],
            }
        },
        ensure_ascii=False,
    )


def _assert_no_raw_auth_payload(test: unittest.TestCase, value: str) -> None:
    test.assertIn('[SBERID_AUTH_RECEIVED]', value)
    for forbidden in (
        'cookie-secret-value',
        'local-storage-token-secret',
        'storageState',
        'cookies',
        'localStorage',
        '"token"',
    ):
        test.assertNotIn(forbidden, value)


class AuthSecretRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_sberid_auth_refresh_reply_keeps_raw_only_for_runtime_and_stores_marker(self) -> None:
        store = SessionStore()
        auth_runner = _SuccessfulAuthScriptRunner()
        service = BuyerService(
            store=store,
            callback_client=_RecordingCallbackClient(),  # type: ignore[arg-type]
            runner=_UnusedRunner(),  # type: ignore[arg-type]
            novnc_url='http://novnc',
            default_callback_url='http://callback',
            cdp_recovery_window_sec=0,
            cdp_recovery_interval_ms=1,
            sberid_allowlist={'litres.ru'},
            sberid_auth_retry_budget=1,
            auth_script_runner=auth_runner,  # type: ignore[arg-type]
        )
        state = await store.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=TaskAuthPayload(storageState={'invalid': True}),
        )
        await store.set_status(state.session_id, SessionStatus.RUNNING)

        auth_flow = asyncio.create_task(service._run_sberid_auth_flow(state))
        reply_id = ''
        for _ in range(50):
            refreshed = await store.get(state.session_id)
            if refreshed.waiting_reply_id:
                reply_id = refreshed.waiting_reply_id
                break
            await asyncio.sleep(0.01)
        self.assertTrue(reply_id)

        await service.submit_reply(state.session_id, reply_id, _raw_storage_state_reply())
        summary = await asyncio.wait_for(auth_flow, timeout=1)

        self.assertEqual(summary['reason_code'], AUTH_OK)
        self.assertEqual(
            auth_runner.seen_storage_state,
            {
                'cookies': [{'name': 'sid', 'value': 'cookie-secret-value'}],
                'origins': [
                    {
                        'origin': 'https://www.litres.ru',
                        'localStorage': [{'name': 'token', 'value': 'local-storage-token-secret'}],
                    }
                ],
            },
        )
        memory_dump = json.dumps(await store.get_agent_memory(state.session_id), ensure_ascii=False)
        _assert_no_raw_auth_payload(self, memory_dump)

    async def test_persistent_sync_redacts_auth_reply_and_memory_storage_state(self) -> None:
        from buyer.app.persistence import _sync_session_related

        raw_reply = _raw_storage_state_reply()
        state = SessionState(
            session_id='session-auth-redaction',
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            status=SessionStatus.RUNNING,
            agent_memory=[{'role': 'user', 'text': raw_reply}],
            pending_reply_text=raw_reply,
        )
        conn = _FakeConn()

        await _sync_session_related(conn, state)  # type: ignore[arg-type]

        memory_rows = [
            rows
            for sql, rows in conn.executemany_calls
            if 'INSERT INTO buyer_agent_memory' in sql
        ][0]
        stored_memory = str(memory_rows[0][3])
        _assert_no_raw_auth_payload(self, stored_memory)

        reply_updates = [
            args
            for sql, args in conn.execute_calls
            if 'UPDATE buyer_replies' in sql and 'message = $2' in sql
        ]
        self.assertEqual(len(reply_updates), 1)
        stored_reply = str(reply_updates[0][1])
        _assert_no_raw_auth_payload(self, stored_reply)

    async def test_sberid_script_runner_uses_cleaned_up_temp_storage_state_outside_trace_dir(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scripts_dir = root / 'scripts'
            trace_dir = root / 'trace'
            bin_dir = scripts_dir / 'node_modules' / '.bin'
            script_dir = scripts_dir / 'sberid'
            bin_dir.mkdir(parents=True)
            script_dir.mkdir(parents=True)
            (script_dir / 'litres.ts').write_text('// test script placeholder\n', encoding='utf-8')
            tsx = bin_dir / 'tsx'
            tsx.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys

storage_path = sys.argv[sys.argv.index('--storage-state-path') + 1]
output_path = sys.argv[sys.argv.index('--output-path') + 1]
with open(storage_path, encoding='utf-8') as handle:
    storage = json.load(handle)
mode = oct(os.stat(storage_path).st_mode & 0o777)
if storage['cookies'][0]['value'] != 'cookie-secret-value':
    raise SystemExit(2)
with open(output_path, 'w', encoding='utf-8') as handle:
    json.dump({
        'status': 'completed',
        'reason_code': 'auth_ok',
        'message': 'ok',
        'artifacts': {
            'observed_storage_path': storage_path,
            'observed_storage_mode': mode,
            'context_prepared_for_reuse': True
        }
    }, handle)
""",
                encoding='utf-8',
            )
            tsx.chmod(0o755)
            runner = SberIdScriptRunner(
                scripts_dir=str(scripts_dir),
                cdp_endpoint='ws://127.0.0.1/devtools/browser/test',
                timeout_sec=5,
                trace_dir=str(trace_dir),
            )

            result = await runner.run(
                session_id='session-1',
                domain='litres.ru',
                start_url='https://www.litres.ru/',
                storage_state={
                    'cookies': [{'name': 'sid', 'value': 'cookie-secret-value'}],
                    'origins': [],
                },
                attempt=1,
            )

            observed_storage_path = Path(str(result.artifacts['observed_storage_path']))
            self.assertEqual(result.reason_code, AUTH_OK)
            self.assertEqual(result.artifacts['observed_storage_mode'], '0o600')
            self.assertFalse(observed_storage_path.exists())
            self.assertFalse(str(observed_storage_path).startswith(str(trace_dir)))
            self.assertEqual(list(trace_dir.rglob('auth-storage-attempt-*.json')), [])

    async def test_sberid_script_runner_uses_unique_output_path_and_removes_stale_deterministic_output(self) -> None:
        tsx_body = r'''
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-path" ]; then
    out="$2"
    shift 2
  else
    shift
  fi
done
printf '%s\n' "{\"status\":\"completed\",\"reason_code\":\"auth_ok\",\"message\":\"ok\",\"artifacts\":{\"observed_output_path\":\"$out\"}}" > "$out"
'''
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scripts_dir = root / 'scripts'
            trace_dir = root / 'trace'
            session_dir = trace_dir / 'session-stale-auth'
            stale_output = session_dir / 'auth-script-result-attempt-01.json'
            session_dir.mkdir(parents=True)
            stale_output.write_text(
                '{"status":"completed","reason_code":"stale","message":"stale"}',
                encoding='utf-8',
            )
            self._write_fake_auth_script_tree(scripts_dir, tsx_body=tsx_body)
            runner = self._auth_runner(scripts_dir=scripts_dir, trace_dir=trace_dir)

            result = await runner.run(
                session_id='session-stale-auth',
                domain='litres.ru',
                start_url='https://www.litres.ru/',
                storage_state={'cookies': [], 'origins': []},
                attempt=1,
            )

            observed_output_path = Path(str(result.artifacts['observed_output_path']))
            self.assertEqual(result.reason_code, AUTH_OK)
            self.assertNotEqual(observed_output_path, stale_output)
            self.assertTrue(observed_output_path.name.startswith('auth-script-result-'))
            self.assertFalse(stale_output.exists())

    async def test_sberid_script_runner_treats_non_zero_payload_as_diagnostics_not_success(self) -> None:
        tsx_body = r'''
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-path" ]; then
    out="$2"
    shift 2
  else
    shift
  fi
done
printf '%s\n' '{"status":"completed","reason_code":"auth_ok","message":"ok","artifacts":{"context_prepared_for_reuse":true}}' > "$out"
printf '%s\n' 'fatal auth script error' >&2
exit 9
'''
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scripts_dir = root / 'scripts'
            trace_dir = root / 'trace'
            self._write_fake_auth_script_tree(scripts_dir, tsx_body=tsx_body)
            runner = self._auth_runner(scripts_dir=scripts_dir, trace_dir=trace_dir)

            result = await runner.run(
                session_id='session-non-zero-auth',
                domain='litres.ru',
                start_url='https://www.litres.ru/',
                storage_state={'cookies': [], 'origins': []},
                attempt=1,
            )

            self.assertEqual(result.status, 'failed')
            self.assertEqual(result.reason_code, AUTH_FAILED_INVALID_SESSION)
            self.assertEqual(result.artifacts['returncode'], 9)
            self.assertEqual(result.artifacts['script_result_payload']['status'], 'completed')
            self.assertIn('fatal auth script error', result.artifacts['stderr_tail'])

    def _write_fake_auth_script_tree(self, scripts_dir: Path, *, tsx_body: str) -> None:
        script_path = scripts_dir / 'sberid' / 'litres.ts'
        tsx_path = scripts_dir / 'node_modules' / '.bin' / 'tsx'
        script_path.parent.mkdir(parents=True)
        tsx_path.parent.mkdir(parents=True)
        script_path.write_text('// fake auth script\n', encoding='utf-8')
        tsx_path.write_text('#!/usr/bin/env bash\nset -u\n' + tsx_body, encoding='utf-8')
        tsx_path.chmod(0o755)

    def _auth_runner(self, *, scripts_dir: Path, trace_dir: Path) -> SberIdScriptRunner:
        return SberIdScriptRunner(
            scripts_dir=str(scripts_dir),
            cdp_endpoint='ws://127.0.0.1/devtools/browser/test',
            timeout_sec=5,
            trace_dir=str(trace_dir),
        )
