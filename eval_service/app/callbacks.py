from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import Field

from eval_service.app.buyer_client import BuyerClient
from eval_service.app.models import (
    BuyerCallbackEnvelope,
    CallbackEventType,
    CaseRunState,
    EvalRunCase,
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
    eval_run_id, eval_case_id = _require_eval_ids(envelope)
    store = _get_run_store(request)

    try:
        manifest = store.append_callback_event(
            eval_run_id,
            eval_case_id,
            envelope,
            **_state_updates_for_callback(envelope),
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
    reply_id = reply.reply_id or _require_case_value(case.waiting_reply_id, 'reply_id')
    buyer_response = await _get_buyer_client(request).send_reply(
        session_id=session_id,
        reply_id=reply_id,
        message=reply.message,
    )
    accepted = bool(_response_field(buyer_response, 'accepted'))
    buyer_status = str(_response_field(buyer_response, 'status'))

    if accepted:
        manifest = store.update_case(
            eval_run_id,
            eval_case_id,
            state=CaseRunState.RUNNING,
            waiting_reply_id=None,
        )
        case = _find_case(manifest.cases, eval_case_id)
        await _schedule_orchestrator_resume(request, eval_run_id, eval_case_id)

    return OperatorReplyResponse(
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        session_id=session_id,
        reply_id=reply_id,
        accepted=accepted,
        buyer_status=buyer_status,
        state=case.state,
    )


async def _schedule_orchestrator_resume(request: Request, eval_run_id: str, eval_case_id: str) -> None:
    resume_coro: Coroutine[Any, Any, Any] = get_run_orchestrator(request).resume_after_operator_reply(
        eval_run_id=eval_run_id,
        eval_case_id=eval_case_id,
        callback_url=str(request.url_for('receive_buyer_callback')),
    )
    scheduler = getattr(request.app.state, 'orchestrator_resume_scheduler', None)
    if scheduler is not None:
        await scheduler(resume_coro)
        return

    tasks = getattr(request.app.state, 'orchestrator_resume_tasks', None)
    if tasks is None:
        tasks = set()
        request.app.state.orchestrator_resume_tasks = tasks
    task = asyncio.create_task(resume_coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _state_updates_for_callback(envelope: BuyerCallbackEnvelope) -> dict[str, Any]:
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


def _require_eval_ids(envelope: BuyerCallbackEnvelope) -> tuple[str, str]:
    if not envelope.eval_run_id or not envelope.eval_case_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='callback должен содержать eval_run_id и eval_case_id',
        )
    return envelope.eval_run_id, envelope.eval_case_id


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


def _find_case(cases: list[EvalRunCase], eval_case_id: str) -> EvalRunCase:
    for case in cases:
        if case.eval_case_id == eval_case_id:
            return case
    raise KeyError(eval_case_id)


def _response_field(response: object, field_name: str) -> object:
    if isinstance(response, dict):
        return response[field_name]
    return getattr(response, field_name)
