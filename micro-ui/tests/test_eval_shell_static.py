from __future__ import annotations

import unittest
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from app.main import eval_proxy_timeout


class EvalShellStaticTests(unittest.TestCase):
    def test_eval_proxy_timeout_allows_long_run_creation(self) -> None:
        run_timeout = eval_proxy_timeout('runs', 'POST')
        judge_timeout = eval_proxy_timeout('runs/run-1/judge', 'POST')
        default_get_timeout = eval_proxy_timeout('runs', 'GET')
        default_post_timeout = eval_proxy_timeout('dashboard/cases', 'POST')

        self.assertGreaterEqual(run_timeout.read, 650.0)
        self.assertGreaterEqual(judge_timeout.read, 650.0)
        self.assertEqual(default_get_timeout.read, 60.0)
        self.assertEqual(default_post_timeout.read, 60.0)

    def test_app_static_knows_payment_unverified_contract(self) -> None:
        app_js = (BASE_DIR / 'app' / 'static' / 'app.js').read_text(encoding='utf-8')
        eval_js = (BASE_DIR / 'app' / 'static' / 'eval.js').read_text(encoding='utf-8')
        eval_css = (BASE_DIR / 'app' / 'static' / 'eval.css').read_text(encoding='utf-8')

        self.assertIn("'payment_unverified'", app_js)
        self.assertIn("'unverified'", app_js)
        self.assertIn("meta('provider', session.payment_provider)", app_js)
        self.assertIn("item.order_id && String(item.status || '').toLowerCase() !== 'unverified'", app_js)
        self.assertIn("['judged', 'judge_failed', 'unverified'].includes(item.runtime_status)", eval_js)
        self.assertIn("status = 'unverified'", eval_js)
        self.assertIn('.eval-status.unverified', eval_css)
