from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from eval_service.app.aggregation import aggregate_evaluations
from eval_service.app.case_registry import CaseRegistry
from eval_service.app.dashboard import build_cases_payload, build_hosts_payload
from eval_service.app.judge_input import write_judge_input
from eval_service.app.judge_runner import JudgeRunner
from eval_service.app.models import CallbackEventType, EvalCase, EvaluationResult, EvalRunCase, EvalRunManifest
from eval_service.app.run_store import RunStore
from eval_service.app.trace_collector import collect_trace_session


router = APIRouter()


@router.get('/cases')
async def list_cases(request: Request) -> dict[str, list[dict[str, Any]]]:
    registry = _get_case_registry(request)
    return {'cases': [_case_item(case) for case in registry.load_cases()]}


@router.get('/runs')
async def list_runs(request: Request) -> dict[str, list[dict[str, Any]]]:
    runs_dir = _get_run_store(request).runs_dir
    if not runs_dir.is_dir():
        return {'runs': []}

    runs: list[dict[str, Any]] = []
    for manifest_path in sorted(runs_dir.glob('*/manifest.json')):
        manifest = EvalRunManifest.model_validate_json(manifest_path.read_text(encoding='utf-8'))
        runs.append(_run_summary(manifest, run_dir=manifest_path.parent))
    return {'runs': runs}


@router.get('/runs/{eval_run_id}')
async def get_run(eval_run_id: str, request: Request) -> dict[str, Any]:
    store = _get_run_store(request)
    try:
        manifest = store.read_manifest(eval_run_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'eval run не найден: {eval_run_id}',
        ) from exc

    cases_by_id = {case.eval_case_id: case for case in _get_case_registry(request).load_cases()}
    run = {
        **_run_summary(manifest, run_dir=store.run_dir(eval_run_id)),
        'summary_path': manifest.summary_path,
        'cases': [_run_case_item(run_case, cases_by_id.get(run_case.eval_case_id)) for run_case in manifest.cases],
    }
    return {
        'run': run,
        'evaluations': _load_run_evaluations(store.run_dir(eval_run_id)),
    }


@router.post('/runs/{eval_run_id}/judge')
async def judge_run(eval_run_id: str, request: Request) -> dict[str, Any]:
    store = _get_run_store(request)
    try:
        manifest = store.read_manifest(eval_run_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'eval run не найден: {eval_run_id}',
        ) from exc

    settings = request.app.state.settings
    cases_by_id = {case.eval_case_id: case for case in _get_case_registry(request).load_cases()}
    judge_runner = getattr(request.app.state, 'judge_runner', None) or JudgeRunner(settings)

    evaluations: list[dict[str, Any]] = []
    for run_case in manifest.cases:
        case = cases_by_id.get(run_case.eval_case_id) or _placeholder_case(run_case)
        session_id = run_case.session_id or 'unknown-session'
        judge_input_path = write_judge_input(
            run_dir=store.run_dir(eval_run_id),
            eval_run_id=eval_run_id,
            case=case,
            session_id=session_id,
            task_payload=_task_payload(case),
            events=[event.model_dump(mode='json') for event in run_case.callback_events],
            metrics=_case_metrics(run_case),
            trace_summary=collect_trace_session(settings.buyer_trace_dir, session_id),
            artifacts=run_case.artifact_paths,
            case_state=run_case.state.value,
            case_run=run_case.model_dump(mode='json'),
        )
        result = judge_runner.run(judge_input_path)
        evaluations.append(result.evaluation)

    response_status = 'judge_failed' if any(item.get('status') == 'judge_failed' for item in evaluations) else 'judged'
    return {
        'eval_run_id': eval_run_id,
        'status': response_status,
        'evaluations': evaluations,
    }


@router.get('/dashboard/cases')
async def dashboard_cases(request: Request) -> dict[str, list[dict[str, Any]]]:
    summary = aggregate_evaluations(
        _load_all_evaluations(_get_run_store(request).runs_dir),
        baseline_window=request.app.state.settings.eval_baseline_window,
    )
    return {'rows': build_cases_payload(summary)}


@router.get('/dashboard/hosts')
async def dashboard_hosts(request: Request) -> dict[str, list[dict[str, Any]]]:
    summary = aggregate_evaluations(
        _load_all_evaluations(_get_run_store(request).runs_dir),
        baseline_window=request.app.state.settings.eval_baseline_window,
    )
    return {'rows': build_hosts_payload(summary)}


def _get_case_registry(request: Request) -> CaseRegistry:
    return getattr(request.app.state, 'case_registry', CaseRegistry(request.app.state.settings.eval_cases_dir))


def _get_run_store(request: Request) -> RunStore:
    store = getattr(request.app.state, 'run_store', None)
    if store is None:
        store = RunStore(request.app.state.settings.eval_runs_dir)
        request.app.state.run_store = store
    return store


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


def _run_summary(manifest: EvalRunManifest, *, run_dir: Path) -> dict[str, Any]:
    evaluations = _load_run_evaluations(run_dir)
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
    callbacks = [event.model_dump(mode='json') for event in run_case.callback_events]
    return {
        **case_data,
        'case_version': run_case.case_version,
        'runtime_status': run_case.state.value,
        'session_id': run_case.session_id,
        'waiting_reply_id': run_case.waiting_reply_id,
        'waiting_question': _latest_waiting_question(run_case),
        'callbacks': callbacks,
        'error': run_case.error,
        'artifact_paths': run_case.artifact_paths,
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
            question = event.payload.get('question')
            if isinstance(question, str) and question:
                return question
    return None


def _task_payload(case: EvalCase) -> dict[str, Any]:
    return {
        'task': case.task,
        'start_url': case.start_url,
        'metadata': case.buyer_metadata(),
        'auth_profile': case.auth_profile,
    }


def _case_metrics(run_case: EvalRunCase) -> dict[str, Any]:
    duration_ms = None
    if run_case.started_at is not None and run_case.finished_at is not None:
        duration_ms = int((run_case.finished_at - run_case.started_at).total_seconds() * 1000)
    return {
        'duration_ms': duration_ms,
        'buyer_tokens_used': None,
    }


def _load_run_evaluations(run_dir: Path) -> list[dict[str, Any]]:
    evaluations_dir = run_dir / 'evaluations'
    if not evaluations_dir.is_dir():
        return []
    return [_read_json(path) for path in sorted(evaluations_dir.glob('*.evaluation.json'))]


def _load_all_evaluations(runs_dir: Path) -> list[EvaluationResult]:
    if not runs_dir.is_dir():
        return []
    return [
        EvaluationResult.model_validate_json(path.read_text(encoding='utf-8'))
        for path in sorted(runs_dir.glob('*/evaluations/*.evaluation.json'))
    ]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _json_value(value: Any) -> Any:
    if hasattr(value, 'isoformat'):
        return value.isoformat().replace('+00:00', 'Z')
    return json.loads(json.dumps(value, default=str))
