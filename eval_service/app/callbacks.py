from __future__ import annotations

import asyncio
import hmac
from collections.abc import Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import Field

from eval_service.app.callback_urls import build_buyer_callback_token, build_buyer_callback_url
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
from eval_service.app.runtime_helpers import (
    find_case as _find_case,
    get_buyer_client as _get_buyer_client,
    get_run_store as _get_run_store,
    is_terminal_case_state as _is_terminal_case_state,
    response_field as _response_field,
)


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
    _validate_callback_token(request)
    store = _get_run_store(request)
    eval_run_id, eval_case_id = _resolve_eval_ids(envelope, store)

    try:
        manifest = store.read_manifest(eval_run_id)
        case = _find_case(manifest.cases, eval_case_id)
        _validate_callback_session(case, envelope)
        _validate_callback_payload(envelope)
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
        manifest = _restore_operator_waiting_if_still_claimed(store, eval_run_id, eval_case_id, reply_id)
        case = _find_case(manifest.cases, eval_case_id)
        if case.state != CaseRunState.WAITING_USER:
            try:
                await _schedule_orchestrator_resume(request, eval_run_id, eval_case_id)
            except Exception as resume_exc:
                _mark_resume_failure(store, eval_run_id, resume_exc)
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
        if case.state != CaseRunState.WAITING_USER:
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
        callback_token=build_buyer_callback_token(request),
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
    if envelope.event_type in {CallbackEventType.AGENT_STEP_STARTED, CallbackEventType.HANDOFF_RESUMED}:
        if case.state == CaseRunState.WAITING_USER or case.waiting_reply_id is not None:
            return {
                'state': CaseRunState.RUNNING,
                'waiting_reply_id': None,
            }
        return {}
    if envelope.event_type == CallbackEventType.PAYMENT_READY:
        return {
            'state': CaseRunState.PAYMENT_READY,
            'waiting_reply_id': None,
        }
    if envelope.event_type == CallbackEventType.PAYMENT_UNVERIFIED:
        return {
            'state': CaseRunState.UNVERIFIED,
            'finished_at': envelope.occurred_at,
            'waiting_reply_id': None,
            'error': _payment_unverified_error(envelope),
        }
    if envelope.event_type == CallbackEventType.SCENARIO_FINISHED:
        scenario_status = _payload_status(envelope)
        if scenario_status == 'failed':
            return {
                'state': CaseRunState.FAILED,
                'finished_at': envelope.occurred_at,
                'waiting_reply_id': None,
                'error': _scenario_failure_error(envelope),
            }
        if scenario_status == 'unverified':
            return {
                'state': CaseRunState.UNVERIFIED,
                'finished_at': envelope.occurred_at,
                'waiting_reply_id': None,
                'error': _scenario_unverified_error(envelope),
            }
        return {
            'state': CaseRunState.FINISHED,
            'finished_at': envelope.occurred_at,
            'waiting_reply_id': None,
            'error': None,
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


def _validate_callback_token(request: Request) -> None:
    secret = getattr(request.app.state.settings, 'eval_callback_secret', None)
    if secret is None or not secret.strip():
        return

    token = request.query_params.get('token') or request.headers.get('X-Eval-Callback-Token')
    if token is None or not hmac.compare_digest(token, secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='invalid callback token',
        )


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


def _payload_status(envelope: BuyerCallbackEnvelope) -> str | None:
    status_value = envelope.payload.get('status')
    if isinstance(status_value, str) and status_value:
        return status_value.strip().lower()
    return None


def _validate_callback_payload(envelope: BuyerCallbackEnvelope) -> None:
    if envelope.event_type == CallbackEventType.PAYMENT_READY:
        _payload_string(envelope, 'order_id')
        _payload_string(envelope, 'order_id_host')
        _payload_string(envelope, 'message')
        return
    if envelope.event_type == CallbackEventType.PAYMENT_UNVERIFIED:
        _payload_string(envelope, 'order_id')
        _payload_string(envelope, 'order_id_host')
        _payload_string(envelope, 'provider')
        _payload_string(envelope, 'message')
        _payload_string(envelope, 'reason')
        return
    if envelope.event_type == CallbackEventType.SCENARIO_FINISHED:
        scenario_status = _payload_status(envelope)
        if scenario_status not in {'completed', 'failed', 'unverified'}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail='callback scenario_finished должен содержать payload.status=completed|failed|unverified',
            )
        _payload_string(envelope, 'message')


def _payment_unverified_error(envelope: BuyerCallbackEnvelope) -> str:
    reason = envelope.payload.get('reason')
    message = envelope.payload.get('message')
    if isinstance(reason, str) and reason:
        if isinstance(message, str) and message:
            return f'payment unverified: {reason}: {message}'
        return f'payment unverified: {reason}'
    return f'payment unverified: {envelope.payload}'


def _scenario_unverified_error(envelope: BuyerCallbackEnvelope) -> str:
    message = envelope.payload.get('message')
    if isinstance(message, str) and message:
        return f'payment unverified: {message}'
    return f'payment unverified: {envelope.payload}'


def _scenario_failure_error(envelope: BuyerCallbackEnvelope) -> str:
    message = envelope.payload.get('message')
    if isinstance(message, str) and message:
        return message
    return f'buyer scenario failed: {envelope.payload}'


def _validate_callback_session(case: EvalRunCase, envelope: BuyerCallbackEnvelope) -> None:
    if case.session_id is not None and case.session_id != envelope.session_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='callback session_id не совпадает с eval case session_id',
        )


def _require_case_value(value: str | None, name: str) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'case не содержит активный {name}',
        )
    return value
