from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
