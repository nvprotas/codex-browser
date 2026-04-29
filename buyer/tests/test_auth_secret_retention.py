from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from buyer.app.auth_scripts import (
    AUTH_FAILED_INVALID_SESSION,
    AUTH_OK,
    SberIdScriptRunner,
)
from buyer.app.models import SessionStatus
from buyer.app.state import SessionState


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
