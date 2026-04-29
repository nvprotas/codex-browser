from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from eval_service.app.aggregation import CHECK_NAMES, METRIC_NAMES, aggregate_evaluations


def build_dashboard_payload(
    evaluations_or_summary: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    *,
    baseline_window: int = 5,
) -> dict[str, Any]:
    summary = _as_summary(evaluations_or_summary, baseline_window=baseline_window)
    return {
        'cases': build_cases_payload(summary),
        'hosts': build_hosts_payload(summary),
    }


def build_cases_payload(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _summary_rows(summary)
    baselines = summary.get('baseline', {})
    by_case = _group_rows(rows, 'eval_case_id')

    payload: list[dict[str, Any]] = []
    for eval_case_id in sorted(by_case):
        group_rows = by_case[eval_case_id]
        baseline = baselines.get(eval_case_id)
        payload.append(
            {
                'eval_case_id': eval_case_id,
                'total': len(group_rows),
                'hosts': sorted({row['host'] for row in group_rows}),
                'checks': _check_counts(group_rows),
                'metrics': _metrics_summary(group_rows),
                'recommendations': sum(row['recommendations'] for row in group_rows),
                'baseline': baseline,
                'evaluations': [
                    _row_with_baseline_delta(row, baseline)
                    for row in _sort_rows(group_rows)
                ],
            }
        )
    return payload


def build_hosts_payload(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _summary_rows(summary)
    by_host = _group_rows(rows, 'host')

    payload: list[dict[str, Any]] = []
    for host in sorted(by_host):
        group_rows = by_host[host]
        payload.append(
            {
                'host': host,
                'total': len(group_rows),
                'cases': sorted({row['eval_case_id'] for row in group_rows}),
                'checks': _check_counts(group_rows),
                'metrics': _metrics_summary(group_rows),
                'recommendations': sum(row['recommendations'] for row in group_rows),
                'worst_cases': _worst_cases(group_rows),
                'evaluations': _sort_rows(group_rows),
            }
        )
    return payload


def _as_summary(
    evaluations_or_summary: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    *,
    baseline_window: int,
) -> dict[str, Any]:
    if isinstance(evaluations_or_summary, Mapping) and 'evaluations' in evaluations_or_summary:
        return dict(evaluations_or_summary)
    return aggregate_evaluations(evaluations_or_summary, baseline_window=baseline_window)


def _summary_rows(summary: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in summary.get('evaluations', [])]


def _group_rows(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    return groups


def _check_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts = {
        check_name: {'ok': 0, 'not_ok': 0, 'skipped': 0}
        for check_name in CHECK_NAMES
    }
    for row in rows:
        for check_name in CHECK_NAMES:
            counts[check_name][row['checks'][check_name]] += 1
    return counts


def _metrics_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, int | float | None]]:
    return {
        metric_name: {'median': _median([row['metrics'][metric_name] for row in rows])}
        for metric_name in METRIC_NAMES
    }


def _median(values: list[int | None]) -> int | float | None:
    filtered = sorted(value for value in values if value is not None)
    if not filtered:
        return None
    middle = len(filtered) // 2
    if len(filtered) % 2:
        return filtered[middle]
    value = (filtered[middle - 1] + filtered[middle]) / 2
    if value.is_integer():
        return int(value)
    return value


def _row_with_baseline_delta(row: dict[str, Any], baseline: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(row)
    result['baseline_delta'] = {
        metric_name: _metric_delta(row['metrics'][metric_name], baseline.get(metric_name) if baseline else None)
        for metric_name in METRIC_NAMES
    }
    return result


def _metric_delta(value: int | None, baseline: int | float | None) -> int | float | None:
    if value is None or baseline is None:
        return None
    delta = value - baseline
    if isinstance(delta, float) and delta.is_integer():
        return int(delta)
    return delta


def _worst_cases(rows: list[dict[str, Any]]) -> list[dict[str, int | str]]:
    by_case = _group_rows(rows, 'eval_case_id')
    worst: list[dict[str, int | str]] = []
    for eval_case_id, case_rows in by_case.items():
        status_counter: Counter[str] = Counter()
        for row in case_rows:
            status_counter.update(row['checks'].values())
        item = {
            'eval_case_id': eval_case_id,
            'not_ok': status_counter['not_ok'],
            'skipped': status_counter['skipped'],
            'recommendations': sum(row['recommendations'] for row in case_rows),
        }
        if item['not_ok'] or item['skipped'] or item['recommendations']:
            worst.append(item)
    return sorted(
        worst,
        key=lambda item: (
            -int(item['not_ok']),
            -int(item['skipped']),
            -int(item['recommendations']),
            str(item['eval_case_id']),
        ),
    )


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            row['eval_run_id'],
            row['eval_case_id'],
            row['session_id'],
        ),
    )
