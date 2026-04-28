from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from statistics import median
from typing import Any

from eval_service.app.models import EvaluationResult


CHECK_NAMES = (
    'outcome_ok',
    'safety_ok',
    'payment_boundary_ok',
    'evidence_ok',
    'recommendations_ok',
)
CHECK_STATUSES = ('ok', 'not_ok', 'skipped')
BASELINE_CHECKS = ('outcome_ok', 'safety_ok', 'payment_boundary_ok')
METRIC_NAMES = ('duration_ms', 'buyer_tokens_used')


def load_evaluations(run_dir: Path | str) -> list[EvaluationResult]:
    root = Path(run_dir)
    if root.is_file():
        return [_load_evaluation_file(root)]

    evaluations_dir = root / 'evaluations'
    search_dir = evaluations_dir if evaluations_dir.is_dir() else root
    return [
        _load_evaluation_file(path)
        for path in sorted(search_dir.glob('*.evaluation.json'))
    ]


def aggregate_run(run_dir: Path | str, *, baseline_window: int = 5) -> dict[str, Any]:
    return aggregate_evaluations(load_evaluations(run_dir), baseline_window=baseline_window)


def aggregate_evaluations(
    evaluations: Iterable[EvaluationResult | Mapping[str, Any]],
    *,
    baseline_window: int = 5,
) -> dict[str, Any]:
    items = _sorted_evaluations(evaluations)
    checks = _empty_checks_summary()
    skipped_reasons: dict[str, dict[str, Any]] = {}
    not_ok_cases: list[dict[str, str]] = []
    recommendation_categories: Counter[str] = Counter()
    recommendation_priorities: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    for evaluation in items:
        status_counts[_enum_value(evaluation.status)] += 1

        for recommendation in evaluation.recommendations:
            recommendation_categories[_enum_value(recommendation.category)] += 1
            recommendation_priorities[_enum_value(recommendation.priority)] += 1

        for check_name in CHECK_NAMES:
            check = evaluation.checks[check_name]
            check_status = _enum_value(check.status)
            checks[check_name][check_status] += 1

            if check_status == 'skipped':
                checks[check_name]['skipped_reasons'][check.reason] += 1
                skipped_case = _case_ref(evaluation, check=check_name, reason=check.reason)
                reason_summary = skipped_reasons.setdefault(check.reason, {'count': 0, 'cases': []})
                reason_summary['count'] += 1
                reason_summary['cases'].append(skipped_case)

            if check_status == 'not_ok':
                case_ref = _case_ref(evaluation, check=check_name, reason=check.reason)
                checks[check_name]['not_ok_cases'].append(case_ref)
                not_ok_cases.append(case_ref)

    return {
        'totals': {
            'evaluations': len(items),
            'judged': status_counts['judged'],
            'judge_skipped': status_counts['judge_skipped'],
            'judge_failed': status_counts['judge_failed'],
            'recommendations': sum(len(evaluation.recommendations) for evaluation in items),
        },
        'checks': _finalize_checks(checks),
        'skipped_reasons': _finalize_reason_summary(skipped_reasons),
        'not_ok_cases': not_ok_cases,
        'recommendations': {
            'total': sum(recommendation_categories.values()),
            'by_category': dict(sorted(recommendation_categories.items())),
            'by_priority': dict(sorted(recommendation_priorities.items())),
        },
        'metrics': _metrics_summary(items),
        'baseline': compute_baselines(items, baseline_window=baseline_window),
        'evaluations': [_evaluation_row(evaluation) for evaluation in items],
    }


def compute_baselines(
    evaluations: Iterable[EvaluationResult | Mapping[str, Any]],
    *,
    baseline_window: int,
) -> dict[str, dict[str, int | float | None]]:
    if baseline_window < 1:
        raise ValueError('baseline_window должен быть положительным')

    by_case: dict[str, list[EvaluationResult]] = defaultdict(list)
    for evaluation in _sorted_evaluations(evaluations):
        if _is_baseline_eligible(evaluation):
            by_case[evaluation.eval_case_id].append(evaluation)

    baselines: dict[str, dict[str, int | float | None]] = {}
    for eval_case_id in sorted(by_case):
        selected = by_case[eval_case_id][-baseline_window:]
        baselines[eval_case_id] = {
            'sample_size': len(selected),
            'window': baseline_window,
            'duration_ms': _median_metric(selected, 'duration_ms'),
            'buyer_tokens_used': _median_metric(selected, 'buyer_tokens_used'),
        }
    return baselines


def _load_evaluation_file(path: Path) -> EvaluationResult:
    return EvaluationResult.model_validate_json(path.read_text(encoding='utf-8'))


def _sorted_evaluations(evaluations: Iterable[EvaluationResult | Mapping[str, Any]]) -> list[EvaluationResult]:
    items = [_as_evaluation(evaluation) for evaluation in evaluations]
    return sorted(
        items,
        key=lambda item: (
            item.eval_run_id,
            item.eval_case_id,
            item.case_version,
            item.session_id,
        ),
    )


def _as_evaluation(evaluation: EvaluationResult | Mapping[str, Any]) -> EvaluationResult:
    if isinstance(evaluation, EvaluationResult):
        return evaluation
    return EvaluationResult.model_validate(evaluation)


def _empty_checks_summary() -> dict[str, dict[str, Any]]:
    return {
        check_name: {
            'ok': 0,
            'not_ok': 0,
            'skipped': 0,
            'skipped_reasons': Counter(),
            'not_ok_cases': [],
        }
        for check_name in CHECK_NAMES
    }


def _finalize_checks(checks: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for check_name in CHECK_NAMES:
        summary = checks[check_name]
        finalized[check_name] = {
            'ok': summary['ok'],
            'not_ok': summary['not_ok'],
            'skipped': summary['skipped'],
            'skipped_reasons': dict(sorted(summary['skipped_reasons'].items())),
            'not_ok_cases': summary['not_ok_cases'],
        }
    return finalized


def _finalize_reason_summary(skipped_reasons: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        reason: {
            'count': summary['count'],
            'cases': sorted(
                summary['cases'],
                key=lambda item: (item['eval_run_id'], item['eval_case_id'], item['check']),
            ),
        }
        for reason, summary in sorted(skipped_reasons.items())
    }


def _case_ref(evaluation: EvaluationResult, *, check: str, reason: str) -> dict[str, str]:
    return {
        'eval_run_id': evaluation.eval_run_id,
        'eval_case_id': evaluation.eval_case_id,
        'host': evaluation.host,
        'check': check,
        'reason': reason,
    }


def _metrics_summary(evaluations: list[EvaluationResult]) -> dict[str, dict[str, int | float | None]]:
    return {
        metric_name: {'median': _median_metric(evaluations, metric_name)}
        for metric_name in METRIC_NAMES
    }


def _median_metric(evaluations: list[EvaluationResult], metric_name: str) -> int | float | None:
    values = [
        getattr(evaluation.metrics, metric_name)
        for evaluation in evaluations
        if getattr(evaluation.metrics, metric_name) is not None
    ]
    return _median(values)


def _median(values: list[int]) -> int | float | None:
    if not values:
        return None
    value = median(values)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _is_baseline_eligible(evaluation: EvaluationResult) -> bool:
    return all(
        _enum_value(evaluation.checks[check_name].status) == 'ok'
        for check_name in BASELINE_CHECKS
    )


def _evaluation_row(evaluation: EvaluationResult) -> dict[str, Any]:
    return {
        'eval_run_id': evaluation.eval_run_id,
        'eval_case_id': evaluation.eval_case_id,
        'case_version': evaluation.case_version,
        'session_id': evaluation.session_id,
        'host': evaluation.host,
        'status': _enum_value(evaluation.status),
        'checks': {
            check_name: _enum_value(evaluation.checks[check_name].status)
            for check_name in CHECK_NAMES
        },
        'metrics': {
            'duration_ms': evaluation.metrics.duration_ms,
            'buyer_tokens_used': evaluation.metrics.buyer_tokens_used,
        },
        'recommendations': len(evaluation.recommendations),
    }


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, 'value') else str(value)
