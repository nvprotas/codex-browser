from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

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

    def test_purchase_script_allowlist_setting_is_not_part_of_default_runtime_contract(self) -> None:
        settings = Settings(_env_file=None)

        self.assertFalse(hasattr(settings, 'purchase_script_allowlist'))

    def test_default_app_runtime_does_not_wire_purchase_script_runner(self) -> None:
        from buyer.app.main import service

        self.assertFalse(hasattr(service, '_purchase_script_runner'))
        self.assertFalse(hasattr(service, '_purchase_script_allowlist'))
