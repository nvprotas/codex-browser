from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval_service.app.aggregation import aggregate_evaluations, aggregate_run, compute_baselines, load_evaluations


def _check(status: str, reason: str) -> dict[str, Any]:
    return {'status': status, 'reason': reason, 'evidence_refs': []}


def _evaluation(
    eval_run_id: str,
    eval_case_id: str,
    *,
    host: str = 'shop.example',
    duration_ms: int | None = 1000,
    buyer_tokens_used: int | None = 100,
    outcome: str = 'ok',
    safety: str = 'ok',
    payment: str = 'ok',
    evidence: str = 'ok',
    recommendations_ok: str = 'ok',
    outcome_reason: str = 'Цель достигнута.',
    safety_reason: str = 'Опасных действий нет.',
    payment_reason: str = 'Остановлено на SberPay.',
    evidence_reason: str = 'Есть trace refs.',
    recommendations_reason: str = 'Рекомендации применимы.',
    recommendations: list[dict[str, Any]] | None = None,
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
            'safety_ok': _check(safety, safety_reason),
            'payment_boundary_ok': _check(payment, payment_reason),
            'evidence_ok': _check(evidence, evidence_reason),
            'recommendations_ok': _check(recommendations_ok, recommendations_reason),
        },
        'evidence_refs': [],
        'recommendations': recommendations or [],
        'judge_metadata': {'backend': 'codex_exec', 'model': 'gpt-5.5'},
    }


def _recommendation(category: str, priority: str, draft_text: str) -> dict[str, Any]:
    return {
        'category': category,
        'priority': priority,
        'rationale': f'Причина для {draft_text}.',
        'evidence_refs': [],
        'draft_text': draft_text,
    }


def test_aggregate_evaluations_counts_checks_metrics_recommendations_and_cases_deterministically() -> None:
    evaluations = [
        _evaluation(
            'eval-20260428-120200',
            'case-b',
            host='books.example',
            duration_ms=None,
            buyer_tokens_used=None,
            outcome='skipped',
            safety='skipped',
            payment='skipped',
            evidence='skipped',
            recommendations_ok='skipped',
            outcome_reason='Нет auth-профиля.',
            safety_reason='Нет auth-профиля.',
            payment_reason='Нет auth-профиля.',
            evidence_reason='Нет auth-профиля.',
            recommendations_reason='Нет auth-профиля.',
        ),
        _evaluation(
            'eval-20260428-120100',
            'case-a',
            duration_ms=3000,
            buyer_tokens_used=200,
            outcome='not_ok',
            payment='skipped',
            outcome_reason='Не найден нужный товар.',
            payment_reason='CAPTCHA на checkout.',
            recommendations=[
                _recommendation('prompt', 'high', 'Уточнить критерии товара.'),
                _recommendation('playbook', 'medium', 'Добавить обход checkout modal.'),
            ],
        ),
        _evaluation('eval-20260428-120000', 'case-a', duration_ms=1000, buyer_tokens_used=100),
    ]

    summary = aggregate_evaluations(evaluations, baseline_window=5)

    assert summary['totals'] == {
        'evaluations': 3,
        'judged': 3,
        'judge_skipped': 0,
        'judge_failed': 0,
        'recommendations': 2,
    }
    assert summary['checks']['outcome_ok'] == {
        'ok': 1,
        'not_ok': 1,
        'skipped': 1,
        'skipped_reasons': {'Нет auth-профиля.': 1},
        'not_ok_cases': [
            {
                'eval_run_id': 'eval-20260428-120100',
                'eval_case_id': 'case-a',
                'host': 'shop.example',
                'check': 'outcome_ok',
                'reason': 'Не найден нужный товар.',
            }
        ],
    }
    assert summary['checks']['payment_boundary_ok']['skipped_reasons'] == {
        'CAPTCHA на checkout.': 1,
        'Нет auth-профиля.': 1,
    }
    assert summary['skipped_reasons']['Нет auth-профиля.']['count'] == 5
    assert summary['not_ok_cases'] == [
        {
            'eval_run_id': 'eval-20260428-120100',
            'eval_case_id': 'case-a',
            'host': 'shop.example',
            'check': 'outcome_ok',
            'reason': 'Не найден нужный товар.',
        }
    ]
    assert summary['recommendations'] == {
        'total': 2,
        'by_category': {'playbook': 1, 'prompt': 1},
        'by_priority': {'high': 1, 'medium': 1},
    }
    assert summary['metrics']['duration_ms']['median'] == 2000
    assert summary['metrics']['buyer_tokens_used']['median'] == 150
    assert [
        (row['eval_run_id'], row['eval_case_id'])
        for row in summary['evaluations']
    ] == [
        ('eval-20260428-120000', 'case-a'),
        ('eval-20260428-120100', 'case-a'),
        ('eval-20260428-120200', 'case-b'),
    ]


def test_compute_baselines_uses_last_successful_evaluations_per_case() -> None:
    evaluations = [
        _evaluation('eval-20260428-120400', 'case-a', duration_ms=4000, buyer_tokens_used=300),
        _evaluation(
            'eval-20260428-120200',
            'case-a',
            duration_ms=9000,
            buyer_tokens_used=900,
            outcome='not_ok',
            outcome_reason='Не дошел до нужного товара.',
        ),
        _evaluation('eval-20260428-120100', 'case-a', duration_ms=1000, buyer_tokens_used=100),
        _evaluation('eval-20260428-120300', 'case-a', duration_ms=2000, buyer_tokens_used=150),
        _evaluation(
            'eval-20260428-120100',
            'case-b',
            host='books.example',
            duration_ms=None,
            buyer_tokens_used=500,
        ),
    ]

    baselines = compute_baselines(evaluations, baseline_window=2)

    assert baselines == {
        'case-a': {
            'sample_size': 2,
            'window': 2,
            'duration_ms': 3000,
            'buyer_tokens_used': 225,
        },
        'case-b': {
            'sample_size': 1,
            'window': 2,
            'duration_ms': None,
            'buyer_tokens_used': 500,
        },
    }


def test_load_evaluations_reads_evaluation_json_files_in_path_order(tmp_path: Path) -> None:
    evaluations_dir = tmp_path / 'evaluations'
    evaluations_dir.mkdir()
    first = _evaluation('eval-20260428-120000', 'case-a')
    second = _evaluation('eval-20260428-120100', 'case-b', host='books.example')
    (evaluations_dir / 'b.evaluation.json').write_text(json.dumps(second), encoding='utf-8')
    (evaluations_dir / 'a.evaluation.json').write_text(json.dumps(first), encoding='utf-8')

    evaluations = load_evaluations(tmp_path)

    assert [item.eval_case_id for item in evaluations] == ['case-a', 'case-b']


def test_aggregate_run_builds_summary_from_run_evaluation_files(tmp_path: Path) -> None:
    evaluations_dir = tmp_path / 'eval-20260428-120000' / 'evaluations'
    evaluations_dir.mkdir(parents=True)
    (evaluations_dir / 'case-a.evaluation.json').write_text(
        json.dumps(_evaluation('eval-20260428-120000', 'case-a')),
        encoding='utf-8',
    )
    (evaluations_dir / 'case-b.evaluation.json').write_text(
        json.dumps(
            _evaluation(
                'eval-20260428-120000',
                'case-b',
                host='books.example',
                outcome='not_ok',
                outcome_reason='Не найден товар.',
            )
        ),
        encoding='utf-8',
    )

    summary = aggregate_run(tmp_path / 'eval-20260428-120000', baseline_window=3)

    assert summary['totals']['evaluations'] == 2
    assert summary['checks']['outcome_ok']['not_ok'] == 1
    assert summary['baseline']['case-a']['duration_ms'] == 1000
