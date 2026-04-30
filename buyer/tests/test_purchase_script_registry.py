from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from buyer.app.auth_scripts import parse_allowlist
from buyer.app.purchase_scripts import PurchaseScriptRunner
from buyer.app.settings import Settings


class PurchaseScriptRegistryTests(unittest.TestCase):
    def test_litres_is_not_registered_for_purchase_scripts(self) -> None:
        with TemporaryDirectory() as tmpdir:
            runner = PurchaseScriptRunner(
                scripts_dir='buyer/scripts',
                cdp_endpoint='http://browser:9223',
                timeout_sec=90,
                trace_dir=tmpdir,
            )

        registry = {item['domain']: item for item in runner.registry_snapshot()}

        self.assertNotIn('litres.ru', registry)

    def test_default_purchase_script_allowlist_is_empty(self) -> None:
        settings = Settings(_env_file=None)

        self.assertEqual(settings.purchase_script_allowlist, '')
        self.assertEqual(parse_allowlist(settings.purchase_script_allowlist), set())
