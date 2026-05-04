from __future__ import annotations

import json
from typing import Any

from eval_service.app.aggregation import CHECK_NAMES
from eval_service.app.models import CallbackEventType, EvalCase, EvalRunCase, EvalRunManifest
from eval_service.app.redaction import sanitize_for_judge_input

_CRITICAL_SUCCESS_CHECKS = ('outcome_ok', 'safety_ok', 'payment_boundary_ok')


def case_item(case: EvalCase) -> dict[str, Any]:
    return {
        'eval_case_id': case.eval_case_id,
        'case_version': case.case_version,
        'variant_id': case.variant_id,
        'title': case.title,
        'host': case.host,
        'start_url': case.start_url,
        'auth_profile': case.auth_profile,
        'expected_outcome': case.expected_outcome.stop_condition,
        'forbidden_actions': case.forbidden_actions,
        'rubric': case.rubric,
        'metadata': case.metadata,
    }


def run_summary(
    manifest: EvalRunManifest,
    *,
    evaluations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        'eval_run_id': manifest.eval_run_id,
        'status': manifest.status.value,
        'created_at': _json_value(manifest.created_at),
        'updated_at': _json_value(manifest.updated_at),
        'cases_count': len(manifest.cases),
        'waiting_count': sum(1 for case in manifest.cases if case.state.value == 'waiting_user'),
        'judged_count': len(evaluations),
        'evaluations_count': len(evaluations),
    }


def run_case_item(run_case: EvalRunCase, case: EvalCase | None) -> dict[str, Any]:
    case_data = case_item(case) if case is not None else _placeholder_case_item(run_case)
    callbacks = sanitize_for_judge_input([
        event.model_dump(mode='json') for event in run_case.callback_events
    ])
    artifact_paths = sanitize_for_judge_input(run_case.artifact_paths)
    return {
        **case_data,
        'case_version': run_case.case_version,
        'runtime_status': run_case.state.value,
        'session_id': run_case.session_id,
        'waiting_reply_id': run_case.waiting_reply_id,
        'waiting_question': _latest_waiting_question(run_case),
        'callbacks': callbacks,
        'error': run_case.error,
        'artifact_paths': artifact_paths,
    }


def placeholder_case(run_case: EvalRunCase) -> EvalCase:
    return EvalCase(
        eval_case_id=run_case.eval_case_id,
        case_version=run_case.case_version,
        variant_id='unknown',
        title=run_case.eval_case_id,
        host='unknown',
        task='unknown',
        start_url='unknown',
        expected_outcome={'target': 'unknown', 'stop_condition': 'unknown'},
    )


def task_payload(case: EvalCase) -> dict[str, Any]:
    return {
        'task': case.task,
        'start_url': case.start_url,
        'metadata': case.buyer_metadata(),
        'auth_profile': case.auth_profile,
    }


def case_metrics(
    run_case: EvalRunCase,
    *,
    trace_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    duration_ms = None
    if run_case.started_at is not None and run_case.finished_at is not None:
        duration_ms = int((run_case.finished_at - run_case.started_at).total_seconds() * 1000)
    return {
        'duration_ms': duration_ms,
        'buyer_tokens_used': _buyer_tokens_used(trace_summary),
    }


def run_evaluation_items(
    raw_evaluations: list[dict[str, Any]],
    *,
    manifest: EvalRunManifest | None = None,
) -> list[dict[str, Any]]:
    run_cases = {
        run_case.eval_case_id: run_case
        for run_case in manifest.cases
    } if manifest is not None else {}
    return [
        _evaluation_item(evaluation, run_case=run_cases.get(evaluation.get('eval_case_id')))
        for evaluation in raw_evaluations
    ]


def case_dashboard_row(row: dict[str, Any]) -> dict[str, Any]:
    baseline = _dict_value(row.get('baseline'))
    return {
        **row,
        **_dashboard_micro_ui_fields(row),
        'baseline_duration_ms': baseline.get('duration_ms'),
        'baseline_tokens': baseline.get('buyer_tokens_used'),
    }


def host_dashboard_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        **_dashboard_micro_ui_fields(row),
    }


def string_value(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, 'value'):
        return str(value.value)
    return str(value)


def _placeholder_case_item(run_case: EvalRunCase) -> dict[str, Any]:
    return {
        'eval_case_id': run_case.eval_case_id,
        'case_version': run_case.case_version,
        'variant_id': 'unknown',
        'title': run_case.eval_case_id,
        'host': 'unknown',
        'start_url': '',
        'auth_profile': None,
        'expected_outcome': '',
        'forbidden_actions': [],
        'rubric': {},
        'metadata': {},
    }


def _latest_waiting_question(run_case: EvalRunCase) -> str | None:
    for event in reversed(run_case.callback_events):
        if event.event_type == CallbackEventType.ASK_USER:
            for key in ('message', 'question'):
                value = event.payload.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _evaluation_item(evaluation: dict[str, Any], *, run_case: EvalRunCase | None = None) -> dict[str, Any]:
    metrics = _dict_value(evaluation.get('metrics'))
    checks_detail = _dict_value(evaluation.get('checks'))
    recommendations = evaluation.get('recommendations')
    recommendations_list = recommendations if isinstance(recommendations, list) else []
    artifacts, artifacts_detail = _renderable_artifacts(evaluation, run_case=run_case)
    return {
        **evaluation,
        'runtime_status': _runtime_status(evaluation, run_case),
        'checks': _renderable_checks(checks_detail),
        'checks_detail': checks_detail,
        'duration_ms': metrics.get('duration_ms'),
        'buyer_tokens_used': metrics.get('buyer_tokens_used'),
        'recommendations_count': len(recommendations_list),
        'artifacts': artifacts,
        'artifacts_detail': artifacts_detail,
        'metrics': metrics,
    }


def _runtime_status(evaluation: dict[str, Any], run_case: EvalRunCase | None) -> str | None:
    if run_case is not None:
        return run_case.state.value
    status_value = evaluation.get('runtime_status') or evaluation.get('status')
    return string_value(status_value)


def _renderable_checks(checks: dict[str, Any]) -> list[str]:
    items: list[str] = []
    for check_name in _ordered_check_names(checks):
        check = checks.get(check_name)
        if isinstance(check, dict):
            check_status = string_value(check.get('status')) or 'unknown'
            label = f'{check_name}: {check_status}'
            reason = string_value(check.get('reason'))
            if check_status != 'ok' and reason:
                label = f'{label} - {reason}'
            items.append(label)
        else:
            items.append(f'{check_name}: {string_value(check) or "unknown"}')
    return items


def _ordered_check_names(checks: dict[str, Any]) -> list[str]:
    known = [check_name for check_name in CHECK_NAMES if check_name in checks]
    extra = sorted(check_name for check_name in checks if check_name not in CHECK_NAMES)
    return [*known, *extra]


def _renderable_artifacts(
    evaluation: dict[str, Any],
    *,
    run_case: EvalRunCase | None,
) -> tuple[list[str], dict[str, Any]]:
    artifacts: list[str] = []
    run_case_artifacts = run_case.artifact_paths if run_case is not None else {}
    for name, path in sorted(run_case_artifacts.items()):
        artifacts.append(f'{name}: {path}')

    raw_artifacts = evaluation.get('artifacts')
    for item in _artifact_items(raw_artifacts):
        if item not in artifacts:
            artifacts.append(item)

    evidence_refs = _collect_evidence_refs(evaluation)
    for evidence_ref in evidence_refs:
        item = _evidence_ref_item(evidence_ref)
        if item is not None and item not in artifacts:
            artifacts.append(item)

    return artifacts, {
        'run_case': run_case_artifacts,
        'evaluation': raw_artifacts,
        'evidence_refs': evidence_refs,
    }


def _artifact_items(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [f'{name}: {_renderable_value(path)}' for name, path in sorted(value.items())]
    if isinstance(value, list):
        return [_renderable_value(item) for item in value]
    if value is None:
        return []
    return [_renderable_value(value)]


def _collect_evidence_refs(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for evidence_ref in evaluation.get('evidence_refs') or []:
        if isinstance(evidence_ref, dict):
            refs.append(evidence_ref)
    checks = _dict_value(evaluation.get('checks'))
    for check in checks.values():
        if not isinstance(check, dict):
            continue
        for evidence_ref in check.get('evidence_refs') or []:
            if isinstance(evidence_ref, dict):
                refs.append(evidence_ref)
    return refs


def _evidence_ref_item(evidence_ref: dict[str, Any]) -> str | None:
    for key, label in (
        ('screenshot_path', 'screenshot'),
        ('trace_file', 'trace'),
        ('browser_actions_file', 'browser_actions'),
        ('event_id', 'event'),
    ):
        value = string_value(evidence_ref.get(key))
        if value:
            return f'{label}: {value}'
    step_index = evidence_ref.get('step_index')
    if step_index is not None:
        return f'step: {step_index}'
    record_index = evidence_ref.get('record_index')
    if record_index is not None:
        return f'record: {record_index}'
    return None


def _dashboard_micro_ui_fields(row: dict[str, Any]) -> dict[str, Any]:
    evaluations = _evaluations_history(row)
    return {
        'status': _latest_status(evaluations),
        'duration_ms': _metric_history(evaluations, 'duration_ms'),
        'buyer_tokens_used': _metric_history(evaluations, 'buyer_tokens_used'),
        'success_rate': _success_rate(evaluations),
    }


def _evaluations_history(row: dict[str, Any]) -> list[dict[str, Any]]:
    evaluations = row.get('evaluations')
    return [item for item in evaluations if isinstance(item, dict)] if isinstance(evaluations, list) else []


def _latest_status(evaluations: list[dict[str, Any]]) -> str | None:
    for evaluation in reversed(evaluations):
        status_value = string_value(evaluation.get('status'))
        if status_value:
            return status_value
    return None


def _metric_history(evaluations: list[dict[str, Any]], metric_name: str) -> list[Any]:
    return [
        _dict_value(evaluation.get('metrics')).get(metric_name)
        for evaluation in evaluations
    ]


def _success_rate(evaluations: list[dict[str, Any]]) -> str:
    total = len(evaluations)
    ok = sum(
        1
        for evaluation in evaluations
        if all(
            _dict_value(evaluation.get('checks')).get(check_name) == 'ok'
            for check_name in _CRITICAL_SUCCESS_CHECKS
        )
    )
    return f'{ok}/{total}'


def _buyer_tokens_used(trace_summary: dict[str, Any] | None) -> int | None:
    steps = _dict_value(trace_summary).get('steps')
    if not isinstance(steps, list):
        return None

    total = 0
    found = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        value = _non_negative_int_or_none(step.get('codex_tokens_used'))
        if value is None:
            continue
        total += value
        found = True
    return total if found else None


def _non_negative_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _renderable_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_value(value: Any) -> Any:
    if hasattr(value, 'isoformat'):
        return value.isoformat().replace('+00:00', 'Z')
    return json.loads(json.dumps(value, default=str))
