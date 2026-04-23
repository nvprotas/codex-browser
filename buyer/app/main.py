from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .callback import CallbackClient
from .models import (
    SessionDetail,
    SessionReplyRequest,
    SessionReplyResponse,
    SessionView,
    TaskCreateRequest,
    TaskCreateResponse,
)
from .runner import AgentRunner
from .service import BuyerService
from .settings import get_settings
from .state import ReplyValidationError, SessionConflictError, SessionNotFoundError, SessionState, SessionStore

settings = get_settings()
store = SessionStore(max_active_sessions=settings.max_active_sessions)
callback_client = CallbackClient(settings)
runner = AgentRunner(settings)
service = BuyerService(
    store=store,
    callback_client=callback_client,
    runner=runner,
    novnc_url=settings.novnc_public_url,
    default_callback_url=settings.middle_callback_url,
    cdp_recovery_window_sec=settings.cdp_recovery_window_sec,
    cdp_recovery_interval_ms=settings.cdp_recovery_interval_ms,
)

app = FastAPI(title='buyer-mvp', version='0.1.0')


@app.get('/healthz')
async def healthz() -> dict[str, str]:
    return {'status': 'ok'}


@app.post('/v1/tasks', response_model=TaskCreateResponse, status_code=201)
async def create_task(request: TaskCreateRequest) -> TaskCreateResponse:
    try:
        state = await service.create_session(
            task=request.task,
            start_url=request.start_url,
            callback_url=request.callback_url,
            metadata=request.metadata,
        )
    except SessionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return TaskCreateResponse(session_id=state.session_id, status=state.status, novnc_url=state.novnc_url)


@app.get('/v1/sessions', response_model=list[SessionView])
async def list_sessions() -> list[SessionView]:
    sessions = await service.list_sessions()
    return [_to_view(item) for item in sessions]


@app.get('/v1/sessions/{session_id}', response_model=SessionDetail)
async def get_session(session_id: str) -> SessionDetail:
    try:
        state = await service.get_session(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _to_detail(state)


@app.post('/v1/replies', response_model=SessionReplyResponse)
async def submit_reply(request: SessionReplyRequest) -> SessionReplyResponse:
    try:
        state = await service.submit_reply(
            session_id=request.session_id,
            reply_id=request.reply_id,
            message=request.message,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ReplyValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return SessionReplyResponse(session_id=state.session_id, accepted=True, status=state.status)


def _to_view(state: SessionState) -> SessionView:
    return SessionView(
        session_id=state.session_id,
        status=state.status,
        start_url=state.start_url,
        callback_url=state.callback_url,
        novnc_url=state.novnc_url,
        created_at=state.created_at,
        updated_at=state.updated_at,
        waiting_reply_id=state.waiting_reply_id,
        last_error=state.last_error,
    )


def _to_detail(state: SessionState) -> SessionDetail:
    return SessionDetail(
        **_to_view(state).model_dump(),
        events=state.events,
    )
