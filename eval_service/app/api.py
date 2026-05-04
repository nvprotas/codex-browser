from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from eval_service.app.aggregation import CHECK_NAMES, aggregate_evaluations
from eval_service.app.case_registry import CaseRegistry
from eval_service.app.dashboard import build_cases_payload, build_hosts_payload
from eval_service.app.judge_input import write_judge_input
from eval_service.app.judge_runner import JudgeRunner, write_fallback_evaluation
from eval_service.app.models import (
    CaseRunState,
    CallbackEventType,
    EvalCase,
    EvaluationResult,
    EvalRunCase,
    EvalRunManifest,
)
from eval_service.app.redaction import sanitize_for_judge_input
from eval_service.app.run_store import RunStore
from eval_service.app.runtime_helpers import (
    get_run_store as _get_run_store,
    get_run_store_from_app as _get_run_store_from_app,
)
from eval_service.app.trace_collector import collect_trace_session


router = APIRouter()
_ARTIFACT_READ_ERRORS = (OSError, ValueError, TypeError)
_INCOMPLETE_CASE_STATES = {
    CaseRunState.PENDING,
    CaseRunState.STARTING,
    CaseRunState.RUNNING,
    CaseRunState.WAITING_USER,
    CaseRunState.PAYMENT_READY,
}
_NON_JUDGE_TERMINAL_STATES = {
    CaseRunState.UNVERIFIED,
}
_CRITICAL_SUCCESS_CHECKS = ('outcome_ok', 'safety_ok', 'payment_boundary_ok')
JudgeRunScheduler = Callable[[Coroutine[Any, Any, None]], Awaitable[None]]


@router.get('/cases')
async def list_cases(request: Request) -> dict[str, list[dict[str, Any]]]:
    registry = _get_case_registry(request)
    return {'cases': [_case_item(case) for case in registry.load_cases()]}


@router.get('/runs')
async def list_runs(request: Request) -> dict[str, Any]:
    runs_dir = _get_run_store(request).runs_dir
    if not runs_dir.is_dir():
        return {'runs': []}

    runs: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for manifest_path in sorted(runs_dir.glob('*/manifest.json')):
        manifest = _safe_read_manifest(manifest_path, warnings=warnings)
        if manifest is None:
            continue
        evaluations = _load_raw_run_evaluations(manifest_path.parent, warnings=warnings)
        runs.append(_run_summary(manifest, run_dir=manifest_path.parent, evaluations=evaluations))
    return _response_with_warnings({'runs': runs}, warnings)


@router.get('/runs/{eval_run_id}')
async def get_run(eval_run_id: str, request: Request) -> dict[str, Any]:
    store = _get_run_store(request)
    manifest = _read_manifest_or_raise(store, eval_run_id)

    cases_by_id = {case.eval_case_id: case for case in _get_case_registry(request).load_cases()}
    warnings: list[dict[str, str]] = []
    run_dir = store.run_dir(eval_run_id)
    evaluations = _load_raw_run_evaluations(run_dir, warnings=warnings)
    run = {
        **_run_summary(manifest, run_dir=run_dir, evaluations=evaluations),
        'summary_path': manifest.summary_path,
        'cases': [_run_case_item(run_case, cases_by_id.get(run_case.eval_case_id)) for run_case in manifest.cases],
    }
    return _response_with_warnings({
        'run': run,
        'evaluations': _load_run_evaluations(run_dir, manifest=manifest, raw_evaluations=evaluations),
    }, warnings)


@router.post('/runs/{eval_run_id}/judge', response_model=None)
async def judge_run(eval_run_id: str, request: Request) -> dict[str, Any] | JSONResponse:
    if await _judge_async_requested(request):
        return await _schedule_judge_run(eval_run_id, request)
    return await _run_judge_batch(eval_run_id, request.app)


async def _run_judge_batch(eval_run_id: str, app: Any) -> dict[str, Any]:
    store = _get_run_store_from_app(app)
    manifest = _read_manifest_or_raise(store, eval_run_id)
    _raise_if_incomplete_cases(manifest)

    settings = app.state.settings
    cases_by_id = {case.eval_case_id: case for case in _get_case_registry_from_app(app).load_cases()}
    judge_runner = getattr(app.state, 'judge_runner', None) or JudgeRunner(settings)
    run_dir = store.run_dir(eval_run_id)

    for run_case in _cases_requiring_judge(manifest, run_dir=run_dir):
        if run_case.state != CaseRunState.SKIPPED_AUTH_MISSING:
            store.update_case(eval_run_id, run_case.eval_case_id, state=CaseRunState.JUDGE_PENDING)
        case = cases_by_id.get(run_case.eval_case_id) or _placeholder_case(run_case)
        session_id = run_case.session_id or 'unknown-session'
        trace_summary = collect_trace_session(settings.buyer_trace_dir, session_id)
        judge_input_path = write_judge_input(
            run_dir=run_dir,
            eval_run_id=eval_run_id,
            case=case,
            session_id=session_id,
            task_payload=_task_payload(case),
            events=[event.model_dump(mode='json') for event in run_case.callback_events],
            metrics=_case_metrics(run_case, trace_summary=trace_summary),
            trace_summary=trace_summary,
            artifacts=run_case.artifact_paths,
            case_state=run_case.state.value,
            case_run=run_case.model_dump(mode='json'),
        )
        if run_case.state != CaseRunState.SKIPPED_AUTH_MISSING:
            store.update_case(
                eval_run_id,
                run_case.eval_case_id,
                artifact_paths={'judge_input': _relative_artifact_path(judge_input_path, run_dir)},
            )
        try:
            result = await run_in_threadpool(judge_runner.run, judge_input_path)
        except Exception as exc:
            judge_input = _read_json_object(judge_input_path)
            result = write_fallback_evaluation(
                _evaluation_path_for_judge_input(judge_input_path, judge_input),
                judge_input,
                status='judge_failed',
                reason=_judge_exception_reason(exc),
                model=settings.eval_judge_model,
            )
        _persist_judge_result(
            store,
            eval_run_id=eval_run_id,
            run_case=run_case,
            judge_input_path=judge_input_path,
            evaluation_path=result.evaluation_path,
            evaluation=result.evaluation,
        )

    raw_evaluations = _load_raw_run_evaluations(run_dir)
    summary = aggregate_evaluations(
        raw_evaluations,
        baseline_window=settings.eval_baseline_window,
    )
    store.write_summary(eval_run_id, summary)

    manifest = store.read_manifest(eval_run_id)
    evaluations = _load_run_evaluations(run_dir, manifest=manifest, raw_evaluations=raw_evaluations)
    return {
        'eval_run_id': eval_run_id,
        'status': _judge_response_status(manifest, evaluations=evaluations),
        'evaluations': evaluations,
    }


@router.get('/dashboard/cases')
async def dashboard_cases(request: Request) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    summary = aggregate_evaluations(
        _load_all_evaluations(_get_run_store(request).runs_dir, warnings=warnings),
        baseline_window=request.app.state.settings.eval_baseline_window,
    )
    return _response_with_warnings(
        {'rows': [_case_dashboard_row(row) for row in build_cases_payload(summary)]},
        warnings,
    )


@router.get('/dashboard/hosts')
async def dashboard_hosts(request: Request) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    summary = aggregate_evaluations(
        _load_all_evaluations(_get_run_store(request).runs_dir, warnings=warnings),
        baseline_window=request.app.state.settings.eval_baseline_window,
    )
    return _response_with_warnings(
        {'rows': [_host_dashboard_row(row) for row in build_hosts_payload(summary)]},
        warnings,
    )


def _get_case_registry(request: Request) -> CaseRegistry:
    return _get_case_registry_from_app(request.app)


def _get_case_registry_from_app(app: Any) -> CaseRegistry:
    return getattr(app.state, 'case_registry', CaseRegistry(app.state.settings.eval_cases_dir))


async def _judge_async_requested(request: Request) -> bool:
    value = request.query_params.get('async') or request.query_params.get('async_mode')
    if _truthy_flag(value):
        return True
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    return _truthy_flag(payload.get('async')) or _truthy_flag(payload.get('async_mode'))


async def _schedule_judge_run(eval_run_id: str, request: Request) -> JSONResponse:
    app = request.app
    store = _get_run_store_from_app(app)
    manifest = _read_manifest_or_raise(store, eval_run_id)
    _raise_if_incomplete_cases(manifest)

    active_task = _active_judge_task(app, eval_run_id)
    if active_task is not None:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=_judge_progress_response(store, eval_run_id, status_value='judge_pending'),
        )

    pending_cases = _mark_cases_judge_pending(store, manifest)
    if not pending_cases:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=_judge_progress_response(
                store,
                eval_run_id,
                status_value=_judge_response_status(manifest, evaluations=[]),
            ),
        )

    run_coro = _run_judge_batch_background(eval_run_id, app)
    scheduler: JudgeRunScheduler | None = getattr(app.state, 'judge_run_scheduler', None)
    if scheduler is not None:
        try:
            await scheduler(run_coro)
        except Exception:
            run_coro.close()
            raise
    else:
        task = asyncio.create_task(run_coro)
        _judge_tasks(app)[eval_run_id] = task
        task.add_done_callback(lambda done_task: _finalize_judge_task(app, eval_run_id, done_task))

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=_judge_progress_response(store, eval_run_id, status_value='judge_pending'),
    )


async def _run_judge_batch_background(eval_run_id: str, app: Any) -> None:
    await _run_judge_batch(eval_run_id, app)


def _judge_tasks(app: Any) -> dict[str, asyncio.Task[None]]:
    tasks = getattr(app.state, 'judge_tasks', None)
    if tasks is None:
        tasks = {}
        app.state.judge_tasks = tasks
    return tasks


def _active_judge_task(app: Any, eval_run_id: str) -> asyncio.Task[None] | None:
    task = _judge_tasks(app).get(eval_run_id)
    if task is None or task.done():
        return None
    return task


def _finalize_judge_task(app: Any, eval_run_id: str, task: asyncio.Task[None]) -> None:
    tasks = _judge_tasks(app)
    if tasks.get(eval_run_id) is task:
        tasks.pop(eval_run_id, None)
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:  # noqa: BLE001 - фоновая задача должна оставить понятный статус
        _mark_pending_judge_cases_failed(app, eval_run_id, exc)


def _mark_pending_judge_cases_failed(app: Any, eval_run_id: str, exc: Exception) -> None:
    store = _get_run_store_from_app(app)
    try:
        manifest = store.read_manifest(eval_run_id)
    except _ARTIFACT_READ_ERRORS:
        return
    error = f'judge background failure: {exc.__class__.__name__}: {exc}'
    for run_case in manifest.cases:
        if run_case.state == CaseRunState.JUDGE_PENDING:
            store.update_case(
                eval_run_id,
                run_case.eval_case_id,
                state=CaseRunState.JUDGE_FAILED,
                error=error,
            )


def _mark_cases_judge_pending(store: RunStore, manifest: EvalRunManifest) -> list[str]:
    run_dir = store.run_dir(manifest.eval_run_id)
    pending_case_ids: list[str] = []
    for run_case in _cases_requiring_judge(manifest, run_dir=run_dir):
        if run_case.state == CaseRunState.SKIPPED_AUTH_MISSING:
            pending_case_ids.append(run_case.eval_case_id)
            continue
        store.update_case(manifest.eval_run_id, run_case.eval_case_id, state=CaseRunState.JUDGE_PENDING)
        pending_case_ids.append(run_case.eval_case_id)
    return pending_case_ids


def _cases_requiring_judge(manifest: EvalRunManifest, *, run_dir: Path) -> list[EvalRunCase]:
    return [
        run_case
        for run_case in manifest.cases
        if (
            run_case.state not in _NON_JUDGE_TERMINAL_STATES
            and not (
                run_case.state == CaseRunState.JUDGED
                and _has_valid_evaluation_for_case(run_dir, run_case.eval_case_id)
            )
        )
    ]


def _has_valid_evaluation_for_case(run_dir: Path, eval_case_id: str) -> bool:
    path = run_dir / 'evaluations' / f'{eval_case_id}.evaluation.json'
    return _safe_read_evaluation_object(path, warnings=None) is not None if path.is_file() else False


def _judge_progress_response(store: RunStore, eval_run_id: str, *, status_value: str) -> dict[str, Any]:
    manifest = store.read_manifest(eval_run_id)
    run_dir = store.run_dir(eval_run_id)
    raw_evaluations = _load_raw_run_evaluations(run_dir)
    return {
        'eval_run_id': eval_run_id,
        'status': status_value,
        'evaluations': _load_run_evaluations(run_dir, manifest=manifest, raw_evaluations=raw_evaluations),
    }


def _judge_response_status(manifest: EvalRunManifest, *, evaluations: list[dict[str, Any]]) -> str:
    if any(item.get('status') == 'judge_failed' for item in evaluations):
        return 'judge_failed'
    if any(run_case.state == CaseRunState.UNVERIFIED for run_case in manifest.cases):
        return 'unverified'
    return 'judged'


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return False


def _read_manifest_or_raise(store: RunStore, eval_run_id: str) -> EvalRunManifest:
    try:
        return store.read_manifest(eval_run_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'eval run не найден: {eval_run_id}',
        ) from exc
    except _ARTIFACT_READ_ERRORS as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_artifact_warning(store.manifest_path(eval_run_id), exc),
        ) from exc


def _safe_read_manifest(path: Path, *, warnings: list[dict[str, str]]) -> EvalRunManifest | None:
    try:
        return EvalRunManifest.model_validate_json(path.read_text(encoding='utf-8'))
    except _ARTIFACT_READ_ERRORS as exc:
        warnings.append(_artifact_warning(path, exc))
        return None


def _case_item(case: EvalCase) -> dict[str, Any]:
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


def _run_summary(
    manifest: EvalRunManifest,
    *,
    run_dir: Path,
    evaluations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evaluations = evaluations if evaluations is not None else _load_raw_run_evaluations(run_dir)
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


def _run_case_item(run_case: EvalRunCase, case: EvalCase | None) -> dict[str, Any]:
    case_data = _case_item(case) if case is not None else _placeholder_case_item(run_case)
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


def _placeholder_case(run_case: EvalRunCase) -> EvalCase:
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


def _latest_waiting_question(run_case: EvalRunCase) -> str | None:
    for event in reversed(run_case.callback_events):
        if event.event_type == CallbackEventType.ASK_USER:
            for key in ('message', 'question'):
                value = event.payload.get(key)
                if isinstance(value, str) and value:
                    return value
    return None


def _task_payload(case: EvalCase) -> dict[str, Any]:
    return {
        'task': case.task,
        'start_url': case.start_url,
        'metadata': case.buyer_metadata(),
        'auth_profile': case.auth_profile,
    }


def _case_metrics(
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


def _load_run_evaluations(
    run_dir: Path,
    *,
    manifest: EvalRunManifest | None = None,
    raw_evaluations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    run_cases = {
        run_case.eval_case_id: run_case
        for run_case in manifest.cases
    } if manifest is not None else {}
    raw_evaluations = (
        raw_evaluations
        if raw_evaluations is not None
        else _load_raw_run_evaluations(run_dir)
    )
    return [
        _evaluation_item(evaluation, run_case=run_cases.get(evaluation.get('eval_case_id')))
        for evaluation in raw_evaluations
    ]


def _load_raw_run_evaluations(
    run_dir: Path,
    *,
    warnings: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    evaluations_dir = run_dir / 'evaluations'
    if not evaluations_dir.is_dir():
        return []
    evaluations: list[dict[str, Any]] = []
    for path in sorted(evaluations_dir.glob('*.evaluation.json')):
        evaluation = _safe_read_evaluation_object(path, warnings=warnings)
        if evaluation is not None:
            evaluations.append(evaluation)
    return evaluations


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
    return _string_value(status_value)


def _renderable_checks(checks: dict[str, Any]) -> list[str]:
    items: list[str] = []
    for check_name in _ordered_check_names(checks):
        check = checks.get(check_name)
        if isinstance(check, dict):
            check_status = _string_value(check.get('status')) or 'unknown'
            label = f'{check_name}: {check_status}'
            reason = _string_value(check.get('reason'))
            if check_status != 'ok' and reason:
                label = f'{label} - {reason}'
            items.append(label)
        else:
            items.append(f'{check_name}: {_string_value(check) or "unknown"}')
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
        value = _string_value(evidence_ref.get(key))
        if value:
            return f'{label}: {value}'
    step_index = evidence_ref.get('step_index')
    if step_index is not None:
        return f'step: {step_index}'
    record_index = evidence_ref.get('record_index')
    if record_index is not None:
        return f'record: {record_index}'
    return None


def _case_dashboard_row(row: dict[str, Any]) -> dict[str, Any]:
    baseline = _dict_value(row.get('baseline'))
    return {
        **row,
        **_dashboard_micro_ui_fields(row),
        'baseline_duration_ms': baseline.get('duration_ms'),
        'baseline_tokens': baseline.get('buyer_tokens_used'),
    }


def _host_dashboard_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        **_dashboard_micro_ui_fields(row),
    }


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
        status_value = _string_value(evaluation.get('status'))
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


def _raise_if_incomplete_cases(manifest: EvalRunManifest) -> None:
    incomplete_cases = [
        {
            'eval_case_id': run_case.eval_case_id,
            'case_version': run_case.case_version,
            'state': run_case.state.value,
        }
        for run_case in manifest.cases
        if run_case.state in _INCOMPLETE_CASE_STATES
    ]
    if not incomplete_cases:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            'message': 'judge нельзя запускать, пока есть незавершенные cases',
            'incomplete_cases': incomplete_cases,
        },
    )


def _persist_judge_result(
    store: RunStore,
    *,
    eval_run_id: str,
    run_case: EvalRunCase,
    judge_input_path: Path,
    evaluation_path: Path,
    evaluation: dict[str, Any],
) -> None:
    next_state = _judge_case_state(run_case, evaluation)
    artifact_paths = {
        'judge_input': _relative_artifact_path(judge_input_path, store.run_dir(eval_run_id)),
        'evaluation': _relative_artifact_path(evaluation_path, store.run_dir(eval_run_id)),
    }
    if next_state is None:
        store.update_case(eval_run_id, run_case.eval_case_id, artifact_paths=artifact_paths)
    else:
        store.update_case(
            eval_run_id,
            run_case.eval_case_id,
            state=next_state,
            artifact_paths=artifact_paths,
        )


def _judge_case_state(run_case: EvalRunCase, evaluation: dict[str, Any]) -> CaseRunState | None:
    if run_case.state == CaseRunState.SKIPPED_AUTH_MISSING and evaluation.get('status') == 'judge_skipped':
        return None
    if evaluation.get('status') == 'judged':
        return CaseRunState.JUDGED
    return CaseRunState.JUDGE_FAILED


def _relative_artifact_path(path: Path, run_dir: Path) -> str:
    try:
        return str(path.relative_to(run_dir))
    except ValueError:
        return str(path)


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


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, 'value'):
        return str(value.value)
    return str(value)


def _renderable_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_all_evaluations(
    runs_dir: Path,
    *,
    warnings: list[dict[str, str]] | None = None,
) -> list[EvaluationResult]:
    if not runs_dir.is_dir():
        return []
    evaluations: list[EvaluationResult] = []
    for path in sorted(runs_dir.glob('*/evaluations/*.evaluation.json')):
        try:
            evaluations.append(EvaluationResult.model_validate_json(path.read_text(encoding='utf-8')))
        except _ARTIFACT_READ_ERRORS as exc:
            if warnings is not None:
                warnings.append(_artifact_warning(path, exc))
    return evaluations


def _safe_read_json_object(
    path: Path,
    *,
    warnings: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    try:
        return _read_json_object(path)
    except _ARTIFACT_READ_ERRORS as exc:
        if warnings is not None:
            warnings.append(_artifact_warning(path, exc))
        return None


def _safe_read_evaluation_object(
    path: Path,
    *,
    warnings: list[dict[str, str]] | None,
) -> dict[str, Any] | None:
    try:
        payload = _read_json_object(path)
        EvaluationResult.model_validate(payload)
        return payload
    except _ARTIFACT_READ_ERRORS as exc:
        if warnings is not None:
            warnings.append(_artifact_warning(path, exc))
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('JSON root must be an object')
    return payload


def _response_with_warnings(
    response: dict[str, Any],
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    if warnings:
        response['warnings'] = warnings
    return response


def _artifact_warning(path: Path, exc: BaseException) -> dict[str, str]:
    return {
        'path': str(path),
        'error': f'{exc.__class__.__name__}: {exc}',
    }


def _evaluation_path_for_judge_input(judge_input_path: Path, judge_input: dict[str, Any]) -> Path:
    eval_case_id = _string_value(judge_input.get('eval_case_id')) or judge_input_path.stem.removesuffix('.judge-input')
    return judge_input_path.with_name(f'{eval_case_id}.evaluation.json')


def _judge_exception_reason(exc: Exception) -> str:
    return f'judge runner failed: {exc.__class__.__name__}: {exc}'


def _json_value(value: Any) -> Any:
    if hasattr(value, 'isoformat'):
        return value.isoformat().replace('+00:00', 'Z')
    return json.loads(json.dumps(value, default=str))
