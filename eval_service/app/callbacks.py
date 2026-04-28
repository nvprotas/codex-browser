from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import Field

from eval_service.app.buyer_client import BuyerClient
from eval_service.app.callback_urls import build_buyer_callback_url
from eval_service.app.models import (
    BuyerCallbackEnvelope,
    CallbackEventType,
    CaseRunState,
    EvalRunCase,
    EvalRunManifest,
    EvalRunStatus,
    StrictBaseModel,
)
from eval_service.app.orchestrator import get_run_orchestrator
from eval_service.app.run_store import RunStore


router = APIRouter()


class CallbackAcceptedResponse(StrictBaseModel):
    eval_run_id: str
    eval_case_id: str
    state: CaseRunState


class OperatorReplyRequest(StrictBaseModel):
    message: str = Field(min_length=1)
    reply_id: str | None = Field(default=None, min_length=1)


class OperatorReplyResponse(StrictBaseModel):
    eval_run_id: str
    eval_case_id: str
    session_id: str
    reply_id: str
    accepted: bool
    buyer_status: str
    state: CaseRunState


@router.post('/callbacks/buyer', response_model=CallbackAcceptedResponse)
async def receive_buyer_callback(
    envelope: BuyerCallbackEnvelope,
    request: Request,
) -> CallbackAcceptedResponse:
    store = _get_run_store(request)
    eval_run_id, eval_case_id = _resolve_eval_ids(envelope, store)

    try:
        manifest = store.read_manifest(eval_run_id)
        case = _find_case(manifest.cases, eval_case_id)
        envelope = envelope.model_copy(update={'eval_run_id': eval_run_id, 'eval_case_id': eval_case_id})
        manifest = store.append_callback_event(
            eval_run_id,
            eval_case_id,
            envelope,
            **_state_updates_for_callback(envelope, case),
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'eval run не найден: {eval_run_id}',
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'eval case не найден: {eval_case_id}',
        ) from exc

    case = _find_case(manifest.cases, eval_case_id)
    return CallbackAcceptedResponse(
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        state=case.state,
    )


@router.post('/runs/{eval_run_id}/cases/{eval_case_id}/reply', response_model=OperatorReplyResponse)
async def send_operator_reply(
    eval_run_id: str,
    eval_case_id: str,
    reply: OperatorReplyRequest,
    request: Request,
) -> OperatorReplyResponse:
    store = _get_run_store(request)
    try:
        manifest = store.read_manifest(eval_run_id)
        case = _find_case(manifest.cases, eval_case_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'eval run не найден: {eval_run_id}',
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'eval case не найден: {eval_case_id}',
        ) from exc

    if case.state != CaseRunState.WAITING_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'case не ожидает ответ оператора: state={case.state}',
        )

    session_id = _require_case_value(case.session_id, 'session_id')
    current_reply_id = _require_case_value(case.waiting_reply_id, 'reply_id')
    if reply.reply_id is not None and reply.reply_id != current_reply_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='reply_id не совпадает с активным ожиданием оператора',
        )
    reply_id = reply.reply_id or current_reply_id
    store.update_case(
        eval_run_id,
        eval_case_id,
        state=CaseRunState.RUNNING,
        waiting_reply_id=None,
    )
    try:
        buyer_response = await _get_buyer_client(request).send_reply(
            session_id=session_id,
            reply_id=reply_id,
            message=reply.message,
        )
    except Exception:
        _restore_operator_waiting_if_still_claimed(store, eval_run_id, eval_case_id, reply_id)
        raise

    accepted = bool(_response_field(buyer_response, 'accepted'))
    buyer_status = str(_response_field(buyer_response, 'status'))
    manifest = store.read_manifest(eval_run_id)
    case = _find_case(manifest.cases, eval_case_id)

    if accepted:
        should_resume = case.state != CaseRunState.WAITING_USER
        if case.state == CaseRunState.WAITING_USER and case.waiting_reply_id == reply_id:
            manifest = store.update_case(
                eval_run_id,
                eval_case_id,
                state=CaseRunState.RUNNING,
                waiting_reply_id=None,
            )
            case = _find_case(manifest.cases, eval_case_id)
            should_resume = True
        if should_resume:
            await _schedule_orchestrator_resume(request, eval_run_id, eval_case_id)
    else:
        manifest = _restore_operator_waiting_if_still_claimed(store, eval_run_id, eval_case_id, reply_id)
        case = _find_case(manifest.cases, eval_case_id)

    return OperatorReplyResponse(
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        session_id=session_id,
        reply_id=reply_id,
        accepted=accepted,
        buyer_status=buyer_status,
        state=case.state,
    )


def _restore_operator_waiting_if_still_claimed(
    store: RunStore,
    eval_run_id: str,
    eval_case_id: str,
    reply_id: str,
) -> EvalRunManifest:
    manifest = store.read_manifest(eval_run_id)
    case = _find_case(manifest.cases, eval_case_id)
    if case.state == CaseRunState.RUNNING and case.waiting_reply_id is None:
        return store.update_case(
            eval_run_id,
            eval_case_id,
            state=CaseRunState.WAITING_USER,
            waiting_reply_id=reply_id,
        )
    return manifest


async def _schedule_orchestrator_resume(request: Request, eval_run_id: str, eval_case_id: str) -> None:
    store = _get_run_store(request)
    resume_coro: Coroutine[Any, Any, Any] = get_run_orchestrator(request).resume_after_operator_reply(
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        callback_url=build_buyer_callback_url(request),
    )
    scheduler = getattr(request.app.state, 'orchestrator_resume_scheduler', None)
    if scheduler is not None:
        try:
            await scheduler(resume_coro)
        except Exception as exc:
            resume_coro.close()
            _mark_resume_failure(store, eval_run_id, exc)
        return

    tasks = getattr(request.app.state, 'orchestrator_resume_tasks', None)
    if tasks is None:
        tasks = set()
        request.app.state.orchestrator_resume_tasks = tasks
    task = asyncio.create_task(resume_coro)
    tasks.add(task)
    task.add_done_callback(lambda done_task: _finalize_resume_task(done_task, tasks, store, eval_run_id))


def _state_updates_for_callback(envelope: BuyerCallbackEnvelope, case: EvalRunCase) -> dict[str, Any]:
    if _is_terminal_case_state(case.state):
        return {'session_id': case.session_id}
    if envelope.event_type == CallbackEventType.ASK_USER:
        return {
            'state': CaseRunState.WAITING_USER,
            'waiting_reply_id': _payload_string(envelope, 'reply_id'),
        }
    if envelope.event_type == CallbackEventType.PAYMENT_READY:
        return {
            'state': CaseRunState.PAYMENT_READY,
            'waiting_reply_id': None,
        }
    if envelope.event_type == CallbackEventType.SCENARIO_FINISHED:
        return {
            'state': CaseRunState.FINISHED,
            'finished_at': envelope.occurred_at,
            'waiting_reply_id': None,
        }
    return {}


def _finalize_resume_task(
    task: asyncio.Task[Any],
    tasks: set[asyncio.Task[Any]],
    store: RunStore,
    eval_run_id: str,
) -> None:
    tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        _mark_resume_failure(store, eval_run_id, exc)


def _mark_resume_failure(store: RunStore, eval_run_id: str, _exc: Exception) -> None:
    store.update_run_status(eval_run_id, EvalRunStatus.FAILED)


def _get_run_store(request: Request) -> RunStore:
    store = getattr(request.app.state, 'run_store', None)
    if store is None:
        store = RunStore(request.app.state.settings.eval_runs_dir)
        request.app.state.run_store = store
    return store


def _get_buyer_client(request: Request) -> BuyerClient:
    client = getattr(request.app.state, 'buyer_client', None)
    if client is None:
        client = BuyerClient(request.app.state.settings.buyer_api_base_url)
        request.app.state.buyer_client = client
    return client


def _resolve_eval_ids(envelope: BuyerCallbackEnvelope, store: RunStore) -> tuple[str, str]:
    if envelope.eval_run_id and envelope.eval_case_id:
        return envelope.eval_run_id, envelope.eval_case_id

    try:
        resolved = store.find_case_by_session_id(envelope.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'callback session_id не найден в eval manifests: {envelope.session_id}',
        )
    return resolved


def _payload_string(envelope: BuyerCallbackEnvelope, key: str) -> str:
    value = envelope.payload.get(key)
    if not isinstance(value, str) or not value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'callback {envelope.event_type} должен содержать payload.{key}',
        )
    return value


def _require_case_value(value: str | None, name: str) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'case не содержит активный {name}',
        )
    return value


def _is_terminal_case_state(state: CaseRunState) -> bool:
    return state in {
        CaseRunState.SKIPPED_AUTH_MISSING,
        CaseRunState.FINISHED,
        CaseRunState.TIMEOUT,
        CaseRunState.JUDGED,
        CaseRunState.JUDGE_FAILED,
    }


def _find_case(cases: list[EvalRunCase], eval_case_id: str) -> EvalRunCase:
    for case in cases:
        if case.eval_case_id == eval_case_id:
            return case
    raise KeyError(eval_case_id)


def _response_field(response: object, field_name: str) -> object:
    if isinstance(response, dict):
        return response[field_name]
    return getattr(response, field_name)
