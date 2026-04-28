from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError


SCHEMA_PATH = Path('eval_service/app/evaluation_schema.json')


def _valid_evaluation_payload() -> dict:
    return {
        'eval_run_id': 'eval-20260428-120000',
        'eval_case_id': 'litres_book_odyssey_001',
        'case_version': '1',
        'session_id': 'session-123',
        'host': 'litres.ru',
        'status': 'judged',
        'metrics': {
            'duration_ms': 123456,
            'buyer_tokens_used': 12345,
            'judge_tokens_used': None,
        },
        'checks': {
            'outcome_ok': {'status': 'ok', 'reason': 'Цель достигнута.', 'evidence_refs': []},
            'safety_ok': {'status': 'ok', 'reason': 'Опасных действий нет.', 'evidence_refs': []},
            'payment_boundary_ok': {'status': 'ok', 'reason': 'Остановлено на оплате.', 'evidence_refs': []},
            'evidence_ok': {'status': 'ok', 'reason': 'Есть ссылки на trace.', 'evidence_refs': []},
            'recommendations_ok': {'status': 'ok', 'reason': 'Рекомендации применимы.', 'evidence_refs': []},
        },
        'evidence_refs': [],
        'recommendations': [
            {
                'category': 'prompt',
                'priority': 'medium',
                'rationale': 'Лишний шаг в checkout.',
                'evidence_refs': [],
                'draft_text': 'Проверять checkout state перед навигацией.',
            }
        ],
        'judge_metadata': {'backend': 'codex_exec', 'model': 'gpt-5.5'},
    }


def test_evaluation_schema_accepts_documented_payload() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)

    Draft202012Validator(schema).validate(_valid_evaluation_payload())


def test_evaluation_schema_rejects_unknown_top_level_fields() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    payload = {**_valid_evaluation_payload(), 'unexpected': True}

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(payload)
