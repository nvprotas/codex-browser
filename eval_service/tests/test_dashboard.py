from __future__ import annotations

from typing import Any

from eval_service.app.dashboard import build_dashboard_payload


def _check(status: str, reason: str) -> dict[str, Any]:
    return {'status': status, 'reason': reason, 'evidence_refs': []}


def _evaluation(
    eval_run_id: str,
    eval_case_id: str,
    *,
    host: str,
    duration_ms: int | None,
    buyer_tokens_used: int | None,
    outcome: str = 'ok',
    safety: str = 'ok',
    payment: str = 'ok',
    outcome_reason: str = 'Цель достигнута.',
    recommendations: int = 0,
) -> dict[str, Any]:
    return {
        'eval_run_id': eval_run_id,
        'eval_case_id': eval_case_id,
        'case_version': '1',
        'session_id': f'session-{eval_run_id}-{eval_case_id}',
        'host': host,
        'status': 'judged',
        'metrics': {
            'duration_ms': duration_ms,
            'buyer_tokens_used': buyer_tokens_used,
            'judge_tokens_used': None,
        },
        'checks': {
            'outcome_ok': _check(outcome, outcome_reason),
            'safety_ok': _check(safety, 'Опасных действий нет.'),
            'payment_boundary_ok': _check(payment, 'Остановлено на SberPay.'),
            'evidence_ok': _check('ok', 'Есть trace refs.'),
            'recommendations_ok': _check('ok', 'Рекомендации применимы.'),
        },
        'evidence_refs': [],
        'recommendations': [
            {
                'category': 'prompt',
                'priority': 'medium',
                'rationale': f'Причина {index}.',
                'evidence_refs': [],
                'draft_text': f'Рекомендация {index}.',
            }
            for index in range(recommendations)
        ],
        'judge_metadata': {'backend': 'codex_exec', 'model': 'gpt-5.5'},
    }


def test_dashboard_payload_groups_evaluations_by_case_and_host_with_baseline_deltas() -> None:
    evaluations = [
        _evaluation(
            'eval-20260428-120200',
            'case-a',
            host='shop.example',
            duration_ms=3000,
            buyer_tokens_used=180,
            outcome='not_ok',
            outcome_reason='Не найден нужный товар.',
            recommendations=2,
        ),
        _evaluation(
            'eval-20260428-120000',
            'case-a',
            host='shop.example',
            duration_ms=1000,
            buyer_tokens_used=100,
        ),
        _evaluation(
            'eval-20260428-120100',
            'case-b',
            host='books.example',
            duration_ms=2000,
            buyer_tokens_used=250,
            recommendations=1,
        ),
        _evaluation(
            'eval-20260428-120300',
            'case-a',
            host='shop.example',
            duration_ms=2000,
            buyer_tokens_used=140,
        ),
    ]

    payload = build_dashboard_payload(evaluations, baseline_window=2)

    assert [case['eval_case_id'] for case in payload['cases']] == ['case-a', 'case-b']
    case_a = payload['cases'][0]
    assert case_a['total'] == 3
    assert case_a['hosts'] == ['shop.example']
    assert case_a['checks']['outcome_ok'] == {'ok': 2, 'not_ok': 1, 'skipped': 0}
    assert case_a['metrics']['duration_ms']['median'] == 2000
    assert case_a['recommendations'] == 2
    assert case_a['baseline'] == {
        'sample_size': 2,
        'window': 2,
        'duration_ms': 1500,
        'buyer_tokens_used': 120,
    }
    assert [
        row['baseline_delta']
        for row in case_a['evaluations']
    ] == [
        {'duration_ms': -500, 'buyer_tokens_used': -20},
        {'duration_ms': 1500, 'buyer_tokens_used': 60},
        {'duration_ms': 500, 'buyer_tokens_used': 20},
    ]

    assert [host['host'] for host in payload['hosts']] == ['books.example', 'shop.example']
    shop = payload['hosts'][1]
    assert shop['total'] == 3
    assert shop['cases'] == ['case-a']
    assert shop['checks']['outcome_ok'] == {'ok': 2, 'not_ok': 1, 'skipped': 0}
    assert shop['metrics']['buyer_tokens_used']['median'] == 140
    assert shop['recommendations'] == 2
    assert shop['worst_cases'] == [
        {
            'eval_case_id': 'case-a',
            'not_ok': 1,
            'skipped': 0,
            'recommendations': 2,
        }
    ]
