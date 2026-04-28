from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from buyer.app.auth_scripts import SberIdScriptRunner
from buyer.app.prompt_builder import build_agent_prompt
from buyer.app.runner import (
    _browser_actions_have_mutating_commands,
    _build_browser_actions_metrics,
    _build_codex_command,
    _build_model_attempt_specs,
    _extract_codex_tokens_used,
    _read_new_jsonl_records,
)
from buyer.app.settings import Settings
from buyer.tools.cdp_tool import (
    HTML_STDOUT_LIMIT,
    LINKS_DEFAULT_LIMIT,
    SNAPSHOT_DEFAULT_LIMIT,
    TEXT_STDOUT_LIMIT,
    _format_html_result,
    _format_text_result,
    parser,
)


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

    def test_text_stdout_is_truncated(self) -> None:
        content = 'x' * (TEXT_STDOUT_LIMIT + 7)

        result = _format_text_result(text=content, selector='body', url='https://www.litres.ru/')

        self.assertTrue(result['ok'])
        self.assertEqual(result['selector'], 'body')
        self.assertEqual(result['text_size'], TEXT_STDOUT_LIMIT + 7)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['text']), TEXT_STDOUT_LIMIT)

    def test_text_stdout_can_be_full_when_explicitly_requested(self) -> None:
        content = 'x' * (TEXT_STDOUT_LIMIT + 7)

        result = _format_text_result(text=content, selector='body', url='https://www.litres.ru/', full=True)

        self.assertEqual(result['text_size'], TEXT_STDOUT_LIMIT + 7)
        self.assertFalse(result['truncated'])
        self.assertEqual(len(result['text']), TEXT_STDOUT_LIMIT + 7)

    def test_structured_commands_parse_stably(self) -> None:
        cli = parser()

        text = cli.parse_args(['text', '--selector', 'body'])
        text_limited = cli.parse_args(['text', '--selector', 'body', '--max-chars', '8000'])
        text_full = cli.parse_args(['text', '--selector', 'body', '--full'])
        exists = cli.parse_args(['exists', '--selector', '[data-testid="x"]'])
        attr = cli.parse_args(['attr', '--selector', 'a', '--name', 'href'])
        links = cli.parse_args(['links', '--selector', 'main', '--limit', '12'])
        links_default = cli.parse_args(['links'])
        snapshot = cli.parse_args(['snapshot', '--selector', 'body', '--limit', '20'])
        snapshot_default = cli.parse_args(['snapshot'])
        html = cli.parse_args(['html', '--full'])

        self.assertEqual(text.command, 'text')
        self.assertEqual(text.max_chars, TEXT_STDOUT_LIMIT)
        self.assertEqual(text_limited.max_chars, 8000)
        self.assertTrue(text_full.full)
        self.assertEqual(exists.command, 'exists')
        self.assertEqual(attr.name, 'href')
        self.assertEqual(links.limit, 12)
        self.assertEqual(links_default.limit, LINKS_DEFAULT_LIMIT)
        self.assertEqual(snapshot.limit, 20)
        self.assertEqual(snapshot_default.limit, SNAPSHOT_DEFAULT_LIMIT)
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
        self.assertIn('snapshot --limit 60', prompt)
        self.assertIn('links --limit 50', prompt)
        self.assertIn('--timeout-ms 3000', prompt)
        self.assertIn('`text` используй только точечно', prompt)
        self.assertIn('`text --selector body` допускается только как fallback и с лимитом', prompt)
        self.assertIn('Не печатай полный HTML в stdout', prompt)
        self.assertIn('`html --path <file>` и `screenshot` используй только как fallback', prompt)


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
                {
                    'ts': '2026-04-23T23:00:05.500000+00:00',
                    'event': 'browser_command_started',
                    'command': 'snapshot',
                    'details': {'selector': 'body'},
                },
                {
                    'ts': '2026-04-23T23:00:06.000000+00:00',
                    'event': 'browser_command_failed',
                    'command': 'snapshot',
                    'duration_ms': 500,
                    'details': {'selector': 'body'},
                    'error': 'CDP_COMMAND_ERROR: timeout',
                },
            ]
            path.write_text('\n'.join(json.dumps(item) for item in records), encoding='utf-8')

            metrics = _build_browser_actions_metrics(path)

        self.assertEqual(metrics['command_duration_ms'], 2300)
        self.assertEqual(metrics['inter_command_idle_ms'], 3700)
        self.assertEqual(metrics['browser_busy_union_ms'], 2300)
        self.assertEqual(metrics['html_commands'], 1)
        self.assertEqual(metrics['html_bytes'], 271066)
        self.assertEqual(metrics['command_breakdown']['html']['html_bytes'], 271066)
        self.assertEqual(metrics['command_breakdown']['snapshot']['errors'], 1)
        self.assertEqual(metrics['command_errors'], 1)
        self.assertEqual(metrics['top_idle_gaps'][0]['duration_ms'], 3500)

    def test_trace_metrics_use_union_for_overlapping_commands(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'actions.jsonl'
            records = [
                {'ts': '2026-04-23T23:00:00.000000+00:00', 'event': 'browser_command_started', 'command': 'links'},
                {'ts': '2026-04-23T23:00:00.100000+00:00', 'event': 'browser_command_started', 'command': 'snapshot'},
                {'ts': '2026-04-23T23:00:00.700000+00:00', 'event': 'browser_command_finished', 'command': 'links', 'ok': True, 'duration_ms': 700},
                {'ts': '2026-04-23T23:00:00.900000+00:00', 'event': 'browser_command_finished', 'command': 'snapshot', 'ok': True, 'duration_ms': 800},
            ]
            path.write_text('\n'.join(json.dumps(item) for item in records), encoding='utf-8')

            metrics = _build_browser_actions_metrics(path)

        self.assertEqual(metrics['command_duration_ms'], 1500)
        self.assertEqual(metrics['browser_busy_union_ms'], 900)
        self.assertEqual(metrics['inter_command_idle_ms'], 0)

    def test_new_jsonl_reader_keeps_partial_line_for_next_read(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'actions.jsonl'
            path.write_text(
                json.dumps({'event': 'browser_command_started', 'command': 'goto'})
                + '\n'
                + '{"event":"browser_command_finished"',
                encoding='utf-8',
            )

            offset, records = _read_new_jsonl_records(path, offset=0)

            self.assertEqual(records, [{'event': 'browser_command_started', 'command': 'goto'}])
            self.assertEqual(offset, path.read_text(encoding='utf-8').index('{"event":"browser_command_finished"'))

            with path.open('a', encoding='utf-8') as fh:
                fh.write(',"command":"goto","ok":true}\n')

            next_offset, next_records = _read_new_jsonl_records(path, offset=offset)

            self.assertGreater(next_offset, offset)
            self.assertEqual(next_records, [{'event': 'browser_command_finished', 'command': 'goto', 'ok': True}])

    def test_model_attempts_default_to_single_legacy_model(self) -> None:
        attempts = _build_model_attempt_specs(Settings(codex_model='gpt-5.4', buyer_model_strategy='single'))

        self.assertEqual([(item.role, item.model) for item in attempts], [('single', 'gpt-5.4')])

    def test_codex_tokens_used_sums_multiple_attempts(self) -> None:
        tokens = _extract_codex_tokens_used(stdout_text='tokens used 10', stderr_text='tokens used 1,250')

        self.assertEqual(tokens, 1260)

    def test_model_attempts_use_fast_then_strong_fallback(self) -> None:
        attempts = _build_model_attempt_specs(
            Settings(
                codex_model='gpt-5.4',
                buyer_model_strategy='fast_then_strong',
                buyer_fast_codex_model='gpt-5.4-mini',
                buyer_strong_codex_model=None,
            )
        )

        self.assertEqual(
            [(item.role, item.model) for item in attempts],
            [('fast', 'gpt-5.4-mini'), ('strong', 'gpt-5.4')],
        )

    def test_model_attempts_fallback_to_default_strong_model(self) -> None:
        attempts = _build_model_attempt_specs(
            Settings(
                codex_model=None,
                buyer_model_strategy='fast_then_strong',
                buyer_fast_codex_model='gpt-5.4-mini',
                buyer_strong_codex_model=None,
            )
        )

        self.assertEqual(attempts[-1].model, 'gpt-5.4')

    def test_codex_command_disables_image_generation_with_minimal_effort(self) -> None:
        cmd = _build_codex_command(
            settings=Settings(
                codex_reasoning_effort='minimal',
                codex_reasoning_summary='none',
                codex_web_search='disabled',
                codex_image_generation_enabled=False,
            ),
            schema_path=Path('/tmp/schema.json'),
            output_path='/tmp/output.json',
            prompt='task',
            model='gpt-test',
        )

        self.assertEqual(cmd[:2], ['codex', 'exec'])
        self.assertIn('--json', cmd)
        self.assertIn('model_reasoning_effort="minimal"', cmd)
        self.assertIn('model_reasoning_summary="none"', cmd)
        self.assertIn('web_search="disabled"', cmd)
        self.assertIn('features.image_generation=false', cmd)
        self.assertLess(cmd.index('-c'), cmd.index('task'))

    def test_codex_command_keeps_image_generation_when_explicitly_enabled(self) -> None:
        cmd = _build_codex_command(
            settings=Settings(
                codex_reasoning_effort='minimal',
                codex_reasoning_summary='none',
                codex_web_search='disabled',
                codex_image_generation_enabled=True,
            ),
            schema_path=Path('/tmp/schema.json'),
            output_path='/tmp/output.json',
            prompt='task',
            model='gpt-test',
        )

        self.assertEqual(cmd[:2], ['codex', 'exec'])
        self.assertIn('--json', cmd)
        self.assertIn('-c', cmd)
        self.assertIn('model_reasoning_effort="minimal"', cmd)
        self.assertIn('model_reasoning_summary="none"', cmd)
        self.assertIn('web_search="disabled"', cmd)
        self.assertNotIn('features.image_generation=false', cmd)
        self.assertLess(cmd.index('-c'), cmd.index('task'))

    def test_mutating_action_detector_blocks_dirty_retry(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'actions.jsonl'
            records = [
                {'event': 'browser_command_started', 'command': 'goto'},
                {'event': 'browser_command_finished', 'command': 'snapshot', 'ok': True},
                {'event': 'browser_command_started', 'command': 'click'},
            ]
            path.write_text('\n'.join(json.dumps(item) for item in records), encoding='utf-8')

            self.assertTrue(_browser_actions_have_mutating_commands(path))

    def test_mutating_action_detector_allows_read_only_retry(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / 'actions.jsonl'
            records = [
                {'event': 'browser_command_started', 'command': 'goto'},
                {'event': 'browser_command_finished', 'command': 'html', 'ok': True},
                {'event': 'browser_command_finished', 'command': 'snapshot', 'ok': True},
            ]
            path.write_text('\n'.join(json.dumps(item) for item in records), encoding='utf-8')

            self.assertFalse(_browser_actions_have_mutating_commands(path))


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


class LitresAuthScriptSmokeTests(unittest.TestCase):
    def test_litres_auth_helpers_when_tsx_is_installed(self) -> None:
        buyer_root = Path(__file__).resolve().parents[1]
        tsx = buyer_root / 'scripts' / 'node_modules' / '.bin' / 'tsx'
        if not tsx.is_file():
            self.skipTest('buyer/scripts/node_modules не установлен')

        scripts_dir = buyer_root / 'scripts'
        command = [
            str(tsx),
            '-e',
            (
                "import { authEntryUrl, hostFromUrl, isSameOrSubdomain, sberIdTargetLabels } from './sberid/litres.ts';"
                "const labels = sberIdTargetLabels();"
                "console.log(JSON.stringify({"
                "entry: authEntryUrl('https://www.litres.ru/'),"
                "host: hostFromUrl('https://www.litres.ru/auth/login/'),"
                "same: isSameOrSubdomain('login.litres.ru', 'litres.ru'),"
                "firstLabels: labels.slice(0, 2)"
                "}));"
            ),
        ]
        completed = subprocess.run(command, cwd=scripts_dir, check=True, text=True, capture_output=True)

        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload,
            {
                'entry': 'https://www.litres.ru/auth/login/',
                'host': 'litres.ru',
                'same': True,
                'firstLabels': ['litres-sb-icon', 'litres-sb-img'],
            },
        )


class SberIdScriptRegistryTests(unittest.TestCase):
    def test_brandshop_is_registered_as_publish_script(self) -> None:
        runner = SberIdScriptRunner(
            scripts_dir='buyer/scripts',
            cdp_endpoint='http://browser:9223',
            timeout_sec=90,
            trace_dir='/tmp',
        )

        registry = {item['domain']: item for item in runner.registry_snapshot()}
        self.assertEqual(
            registry['brandshop.ru'],
            {
                'domain': 'brandshop.ru',
                'lifecycle': 'publish',
                'script': 'sberid/brandshop.ts',
            },
        )


class BrandshopAuthScriptSmokeTests(unittest.TestCase):
    def test_brandshop_auth_helpers_when_tsx_is_installed(self) -> None:
        buyer_root = Path(__file__).resolve().parents[1]
        tsx = buyer_root / 'scripts' / 'node_modules' / '.bin' / 'tsx'
        if not tsx.is_file():
            self.skipTest('buyer/scripts/node_modules не установлен')

        scripts_dir = buyer_root / 'scripts'
        command = [
            str(tsx),
            '-e',
            (
                "import { authEntryUrl, hostFromUrl, isSameOrSubdomain, sberIdTargetLabels } from './sberid/brandshop.ts';"
                "const labels = sberIdTargetLabels();"
                "console.log(JSON.stringify({"
                "entry: authEntryUrl('https://brandshop.ru/new/?foo=bar#top'),"
                "host: hostFromUrl('https://api.brandshop.ru/xhr/checkout/sber_id/callback'),"
                "same: isSameOrSubdomain('api.brandshop.ru', 'brandshop.ru'),"
                "firstLabels: labels.slice(0, 2)"
                "}));"
            ),
        ]
        completed = subprocess.run(command, cwd=scripts_dir, check=True, text=True, capture_output=True)

        payload = json.loads(completed.stdout)
        self.assertEqual(
            payload,
            {
                'entry': 'https://brandshop.ru/',
                'host': 'api.brandshop.ru',
                'same': True,
                'firstLabels': ['brandshop-sber-social-btn', 'brandshop-role-button-sber-id'],
            },
        )
