from __future__ import annotations

import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


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

    def test_eval_js_has_judge_stub_and_service_fallback(self) -> None:
        js = (BASE_DIR / 'app/static/eval.js').read_text(encoding='utf-8')

        expected_fragments = [
            'function buildStubEvaluations',
            'evaluations: buildStubEvaluations',
            'eval service fallback',
            'stubRequest(contract, options)',
        ]

        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, js)

    def test_eval_dashboard_uses_svg_line_charts(self) -> None:
        js = (BASE_DIR / 'app/static/eval.js').read_text(encoding='utf-8')
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
