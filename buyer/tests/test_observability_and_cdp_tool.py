from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from buyer.app.prompt_builder import build_agent_prompt
from buyer.app.runner import _build_browser_actions_metrics
from buyer.tools.cdp_tool import HTML_STDOUT_LIMIT, _format_html_result, parser


class CdpToolOutputTests(unittest.TestCase):
    def test_html_stdout_is_truncated(self) -> None:
        content = 'x' * (HTML_STDOUT_LIMIT + 7)

        result = _format_html_result(content=content, url='https://www.litres.ru/')

        self.assertTrue(result['ok'])
        self.assertEqual(result['html_size'], HTML_STDOUT_LIMIT + 7)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['html']), HTML_STDOUT_LIMIT)

    def test_html_stdout_can_be_full_when_explicitly_requested(self) -> None:
        content = 'x' * (HTML_STDOUT_LIMIT + 7)

        result = _format_html_result(content=content, url='https://www.litres.ru/', full=True)

        self.assertEqual(result['html_size'], HTML_STDOUT_LIMIT + 7)
        self.assertFalse(result['truncated'])
        self.assertEqual(len(result['html']), HTML_STDOUT_LIMIT + 7)

    def test_structured_commands_parse_stably(self) -> None:
        cli = parser()

        exists = cli.parse_args(['exists', '--selector', '[data-testid="x"]'])
        attr = cli.parse_args(['attr', '--selector', 'a', '--name', 'href'])
        links = cli.parse_args(['links', '--selector', 'main', '--limit', '12'])
        snapshot = cli.parse_args(['snapshot', '--selector', 'body', '--limit', '20'])
        html = cli.parse_args(['html', '--full'])

        self.assertEqual(exists.command, 'exists')
        self.assertEqual(attr.name, 'href')
        self.assertEqual(links.limit, 12)
        self.assertEqual(snapshot.limit, 20)
        self.assertTrue(html.full)

    def test_prompt_discourages_full_html_stdout(self) -> None:
        prompt = build_agent_prompt(
            task='Открой litres. Ищи книгу одиссея гомера',
            start_url='https://www.litres.ru/',
            browser_cdp_endpoint='http://browser:9223',
            cdp_preflight_summary='OK',
            metadata={},
            auth_payload=None,
            auth_context=None,
            memory=[],
            latest_user_reply=None,
        )

        self.assertIn('snapshot', prompt)
        self.assertIn('links', prompt)
        self.assertIn('Не печатай полный HTML в stdout', prompt)
        self.assertIn('html --path', prompt)


class BrowserActionMetricsTests(unittest.TestCase):
    def test_trace_metrics_are_built_from_jsonl(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'actions.jsonl'
            records = [
                {
                    'ts': '2026-04-23T23:00:00.000000+00:00',
                    'event': 'browser_command_started',
                    'command': 'goto',
                    'details': {'url': 'https://www.litres.ru/'},
                },
                {
                    'ts': '2026-04-23T23:00:01.500000+00:00',
                    'event': 'browser_command_finished',
                    'command': 'goto',
                    'ok': True,
                    'duration_ms': 1500,
                    'result': {'url': 'https://www.litres.ru/'},
                },
                {
                    'ts': '2026-04-23T23:00:05.000000+00:00',
                    'event': 'browser_command_started',
                    'command': 'html',
                    'details': {'path': None},
                },
                {
                    'ts': '2026-04-23T23:00:05.300000+00:00',
                    'event': 'browser_command_finished',
                    'command': 'html',
                    'ok': True,
                    'duration_ms': 300,
                    'result': {'html_size': 271066},
                },
            ]
            path.write_text('\n'.join(json.dumps(item) for item in records), encoding='utf-8')

            metrics = _build_browser_actions_metrics(path)

        self.assertEqual(metrics['command_duration_ms'], 1800)
        self.assertEqual(metrics['inter_command_idle_ms'], 3500)
        self.assertEqual(metrics['html_commands'], 1)
        self.assertEqual(metrics['html_bytes'], 271066)
        self.assertEqual(metrics['command_breakdown']['html']['html_bytes'], 271066)


class LitresPurchaseScriptSmokeTests(unittest.TestCase):
    def test_litres_helpers_when_tsx_is_installed(self) -> None:
        buyer_root = Path(__file__).resolve().parents[1]
        tsx = buyer_root / 'scripts' / 'node_modules' / '.bin' / 'tsx'
        if not tsx.is_file():
            self.skipTest('buyer/scripts/node_modules не установлен')

        scripts_dir = buyer_root / 'scripts'
        command = [
            str(tsx),
            '-e',
            (
                "import { cartRowsMatchQuery, extractLitresQuery, isSberPaymentUrl, parseOrderId } from './purchase/litres.ts';"
                "const query = extractLitresQuery('Открой litres и дойди до шага оплаты. Ищи книгу одиссея гомера');"
                "const order = parseOrderId('https://www.litres.ru/purchase/ppd/?order=1585051118&method=sbp');"
                "const sber = isSberPaymentUrl('https://www.litres.ru/purchase/ppd/?order=1585051118&method=sbp&system=sbersbp');"
                "const cart = cartRowsMatchQuery('одиссея гомера', ['Одиссея Гомер']);"
                "console.log(JSON.stringify({ query, order, sber, cart }));"
            ),
        ]
        completed = subprocess.run(command, cwd=scripts_dir, check=True, text=True, capture_output=True)

        payload = json.loads(completed.stdout)
        self.assertEqual(payload, {'query': 'одиссея гомера', 'order': '1585051118', 'sber': True, 'cart': True})
