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
        default_get_timeout = eval_proxy_timeout('runs', 'GET')
        default_post_timeout = eval_proxy_timeout('runs/run-1/judge', 'POST')

        self.assertGreaterEqual(run_timeout.read, 650.0)
        self.assertEqual(default_get_timeout.read, 60.0)
        self.assertEqual(default_post_timeout.read, 60.0)
