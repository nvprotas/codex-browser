from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from buyer.app.purchase_scripts import PURCHASE_SCRIPT_FAILED, PurchaseScriptRunner
from buyer.app.script_runtime import read_script_result_payload


class ScriptRuntimeTests(unittest.TestCase):
    def test_read_script_result_payload_falls_back_to_stdout_on_invalid_utf8_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'result.json'
            output_path.write_bytes(b'\xff\xfe')

            result = read_script_result_payload(output_path, '{"status":"completed"}')

        self.assertEqual(result, {'status': 'completed'})

    def test_read_script_result_payload_keeps_non_object_json_from_output_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / 'result.json'
            output_path.write_text('["unexpected"]', encoding='utf-8')

            result = read_script_result_payload(output_path, '{"status":"completed"}')

        self.assertEqual(result, ['unexpected'])


class PurchaseScriptRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_ignores_and_removes_stale_purchase_output(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scripts_dir = root / 'scripts'
            trace_dir = root / 'trace'
            session_dir = trace_dir / 'session-1'
            stale_output = session_dir / 'purchase-script-result.json'
            stale_output.parent.mkdir(parents=True)
            stale_output.write_text(
                '{"status":"completed","reason_code":"stale","message":"stale","order_id":"old-order"}',
                encoding='utf-8',
            )
            self._write_fake_script_tree(scripts_dir, tsx_body='exit 0\n')
            runner = self._runner(scripts_dir=scripts_dir, trace_dir=trace_dir)

            result = await self._run_with_resolved_cdp(runner, session_id='session-1')

            self.assertEqual(result.status, PURCHASE_SCRIPT_FAILED)
            self.assertEqual(result.reason_code, 'purchase_script_invalid_json')
            self.assertIsNone(result.order_id)
            self.assertFalse(stale_output.exists())

    async def test_runner_treats_non_zero_payload_as_diagnostics_not_success(self) -> None:
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
printf '%s\n' '{"status":"completed","reason_code":"purchase_ready","message":"ready","order_id":"order-123","artifacts":{"payment_frame_src":"https://payecom.ru/pay_ru?orderId=order-123"}}' > "$out"
printf '%s\n' 'fatal playwright error' >&2
exit 7
'''
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scripts_dir = root / 'scripts'
            trace_dir = root / 'trace'
            self._write_fake_script_tree(scripts_dir, tsx_body=tsx_body)
            runner = self._runner(scripts_dir=scripts_dir, trace_dir=trace_dir)

            result = await self._run_with_resolved_cdp(runner, session_id='session-2')

        self.assertEqual(result.status, PURCHASE_SCRIPT_FAILED)
        self.assertEqual(result.reason_code, 'purchase_script_process_failed')
        self.assertIsNone(result.order_id)
        self.assertEqual(result.artifacts['script_result_payload']['status'], 'completed')
        self.assertIn('fatal playwright error', result.artifacts['stderr_tail'])

    def _write_fake_script_tree(self, scripts_dir: Path, *, tsx_body: str) -> None:
        script_path = scripts_dir / 'purchase' / 'litres.ts'
        tsx_path = scripts_dir / 'node_modules' / '.bin' / 'tsx'
        script_path.parent.mkdir(parents=True)
        tsx_path.parent.mkdir(parents=True)
        script_path.write_text('// fake purchase script\n', encoding='utf-8')
        tsx_path.write_text('#!/usr/bin/env bash\nset -u\n' + tsx_body, encoding='utf-8')
        tsx_path.chmod(0o755)

    def _runner(self, *, scripts_dir: Path, trace_dir: Path) -> PurchaseScriptRunner:
        return PurchaseScriptRunner(
            scripts_dir=str(scripts_dir),
            cdp_endpoint='http://browser:9223',
            timeout_sec=5,
            trace_dir=str(trace_dir),
        )

    async def _run_with_resolved_cdp(self, runner: PurchaseScriptRunner, *, session_id: str) -> Any:
        async def fake_resolve_cdp_endpoint(_: str) -> str:
            return 'ws://browser/devtools/browser/fake'

        with patch('buyer.app.purchase_scripts.resolve_cdp_endpoint', new=fake_resolve_cdp_endpoint):
            return await runner.run(
                session_id=session_id,
                domain='litres.ru',
                start_url='https://www.litres.ru/',
                task='Открой litres. Ищи книгу одиссея гомера',
            )
