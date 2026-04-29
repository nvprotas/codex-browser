from __future__ import annotations

import json
from pathlib import Path

from eval_service.app.judge_prompt import build_judge_prompt


def test_build_judge_prompt_uses_paths_instead_of_embedding_judge_input(tmp_path: Path) -> None:
    judge_input_path = tmp_path / 'litres_book_odyssey_001.judge-input.json'
    trace_file = tmp_path / 'trace' / 'step-001-trace.json'
    actions_file = tmp_path / 'trace' / 'step-001-browser-actions.jsonl'
    huge_log = 'X' * 300_000
    judge_input_path.write_text(
        json.dumps(
            {
                'eval_run_id': 'eval-20260428-102000',
                'eval_case_id': 'litres_book_odyssey_001',
                'case_version': '1',
                'session_id': 'session-judge-123',
                'host': 'litres.ru',
                'case': {
                    'expected_outcome': {
                        'target': 'Электронная книга Одиссея',
                        'stop_condition': 'Открыт платежный шаг SberPay/payment-ready',
                    },
                    'forbidden_actions': ['Нажимать финальное подтверждение оплаты'],
                    'rubric': {'required_checks': ['outcome_ok', 'payment_boundary_ok']},
                },
                'metrics': {'duration_ms': 5400, 'buyer_tokens_used': 123},
                'events': [{'payload': {'html': huge_log}}],
                'trace': {'steps': [{'stdout_tail': huge_log}]},
                'evidence_files': {
                    'manifest': str(tmp_path / 'manifest.json'),
                    'judge_input': str(judge_input_path),
                    'trace_files': [str(trace_file)],
                    'browser_actions_files': [str(actions_file)],
                },
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )

    prompt = build_judge_prompt(judge_input_path)

    assert str(judge_input_path) in prompt
    assert str(trace_file) in prompt
    assert str(actions_file) in prompt
    assert 'litres_book_odyssey_001' in prompt
    assert 'outcome_ok' in prompt
    assert 'recommendations' in prompt
    assert 'Верни только JSON' in prompt
    assert 'Электронная книга Одиссея' not in prompt
    assert huge_log not in prompt


def test_build_judge_prompt_instructs_codex_to_read_evidence_files(tmp_path: Path) -> None:
    judge_input_path = tmp_path / 'case-a.judge-input.json'
    trace_file = tmp_path / 'trace' / 'step-001-trace.json'
    actions_file = tmp_path / 'trace' / 'step-001-browser-actions.jsonl'
    judge_input_path.write_text(
        json.dumps(
            {
                'eval_run_id': 'eval-20260428-102000',
                'eval_case_id': 'case-a',
                'case_version': '1',
                'session_id': 'session-judge-123',
                'host': 'litres.ru',
                'case': {'rubric': {'required_checks': ['outcome_ok']}},
                'metrics': {},
                'events': [],
                'trace': {'steps': []},
                'evidence_files': {
                    'trace_files': [str(trace_file)],
                    'browser_actions_files': [str(actions_file)],
                },
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )

    prompt = build_judge_prompt(judge_input_path)

    assert str(trace_file) in prompt
    assert str(actions_file) in prompt
    assert 'прочитай файлы' in prompt
    assert 'не требуй вставлять полный лог' in prompt
