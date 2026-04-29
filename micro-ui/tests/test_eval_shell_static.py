from __future__ import annotations

import unittest
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from app.main import eval_proxy_timeout


def _app_file(path: str) -> str:
    return (BASE_DIR / 'app' / path).read_text(encoding='utf-8')


def _static_file(name: str) -> str:
    return _app_file(f'static/{name}')


class EvalShellStaticTests(unittest.TestCase):
    def test_index_mounts_eval_assets_and_shell_nodes(self) -> None:
        html = (BASE_DIR / 'app/templates/index.html').read_text(encoding='utf-8')

        expected_fragments = [
            'href="/static/eval.css"',
            'src="/static/eval.js"',
            'data-tab-target="eval-tab-panel"',
            'id="eval-cases-list"',
            'id="eval-start-run"',
            'id="eval-run-detail"',
            'id="eval-ask-user-form"',
            'id="eval-run-judge"',
            'id="eval-evaluations-body"',
            'id="eval-case-dashboard"',
            'id="eval-host-dashboard"',
        ]

        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, html)

    def test_eval_js_contains_stub_contract_paths(self) -> None:
        js = (BASE_DIR / 'app/static/eval.js').read_text(encoding='utf-8')

        expected_fragments = [
            "'GET /cases'",
            "'POST /runs'",
            "'GET /runs/{eval_run_id}'",
            "'POST /runs/{eval_run_id}/judge'",
            "'POST /runs/{eval_run_id}/cases/{eval_case_id}/reply'",
            "'GET /dashboard/cases'",
            "'GET /dashboard/hosts'",
            'eval_case_id',
            'buyer_tokens_used',
        ]

        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, js)

    def test_eval_js_has_judge_stub_without_service_error_fallback(self) -> None:
        js = (BASE_DIR / 'app/static/eval.js').read_text(encoding='utf-8')

        expected_fragments = [
            'function buildStubEvaluations',
            'evaluations: buildStubEvaluations',
            'stubRequest(contract, options)',
            'return fetchEvalService(contract, options)',
        ]

        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, js)
        eval_request_body = js[js.index('async function evalRequest') : js.index('function renderCases')]
        self.assertNotIn('eval service fallback', js)
        self.assertNotIn('catch', eval_request_body)

    def test_eval_shell_sets_same_origin_eval_base_url(self) -> None:
        html = _app_file('templates/index.html')

        self.assertIn('window.EVAL_SERVICE_BASE_URL', html)
        self.assertIn('/api/eval', html)

    def test_micro_ui_exposes_eval_proxy_to_internal_service(self) -> None:
        settings = _app_file('settings.py')
        main = _app_file('main.py')

        self.assertIn('eval_service_base_url', settings)
        self.assertIn('http://eval_service:8090', settings)
        self.assertIn("@app.api_route('/api/eval/{path:path}'", main)
        self.assertIn('settings.eval_service_base_url', main)
        self.assertIn('httpx.AsyncClient', main)

    def test_eval_proxy_timeout_allows_long_run_creation(self) -> None:
        run_timeout = eval_proxy_timeout('runs', 'POST')
        default_get_timeout = eval_proxy_timeout('runs', 'GET')
        default_post_timeout = eval_proxy_timeout('runs/run-1/judge', 'POST')

        self.assertGreaterEqual(run_timeout.read, 650.0)
        self.assertEqual(default_get_timeout.read, 60.0)
        self.assertEqual(default_post_timeout.read, 60.0)

    def test_eval_js_normalizes_real_eval_run_manifest_cases(self) -> None:
        script = _static_file('eval.js')

        expected_fragments = [
            'function normalizeRunCase',
            'callback_events',
            'runtime_status: item.runtime_status || item.state',
            "title: item.title || item.eval_case_id || '-'",
            "host: item.host || '-'",
            'callbacks,',
        ]

        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, script)

    def test_eval_js_uses_real_run_detail_after_create_and_reply(self) -> None:
        script = _static_file('eval.js')

        self.assertIn('loadRunDetail', script)
        self.assertIn('CONTRACT_PATHS.runDetail', script)
        self.assertIn('normalizeRunCase', script)
        self.assertGreaterEqual(script.count('await loadRunDetail'), 2)

    def test_eval_js_loads_latest_run_on_initial_render(self) -> None:
        script = _static_file('eval.js')

        expected_fragments = [
            'function latestRun',
            'async function loadLatestRun',
            'CONTRACT_PATHS.runs',
            'await loadLatestRun();',
            'run?.eval_run_id',
        ]

        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, script)

    def test_eval_js_reply_payload_does_not_send_session_id(self) -> None:
        script = _static_file('eval.js')
        handler = script[
            script.index("nodes.askForm.addEventListener('submit'")
            : script.index("nodes.runJudge.addEventListener('click'")
        ]
        payload_start = handler.index('payload: {')
        request_payload = handler[payload_start : handler.index('},', payload_start)]

        self.assertIn('reply_id: waiting.waiting_reply_id', request_payload)
        self.assertIn('message,', request_payload)
        self.assertNotIn('session_id:', request_payload)

    def test_eval_dashboard_uses_svg_line_charts(self) -> None:
        js = _static_file('eval.js')
        css = (BASE_DIR / 'app/static/eval.css').read_text(encoding='utf-8')

        expected_js_fragments = [
            "createElementNS('http://www.w3.org/2000/svg'",
            'eval-line-chart',
            'eval-line-path',
            'polyline',
        ]
        expected_css_fragments = [
            '.eval-line-chart',
            '.eval-line-grid',
            '.eval-line-path',
        ]

        for fragment in expected_js_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, js)
        for fragment in expected_css_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, css)
