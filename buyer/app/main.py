from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .auth_scripts import SberIdScriptRunner, parse_allowlist
from .callback import CallbackClient
from .knowledge_analyzer import PostSessionKnowledgeAnalyzer
from .models import (
    SessionDetail,
    SessionReplyRequest,
    SessionReplyResponse,
    SessionView,
    TaskCreateRequest,
    TaskCreateResponse,
)
from .purchase_scripts import PurchaseScriptRunner
from .persistence import PostgresSessionRepository
from .runner import AgentRunner
from .runtime import (
    InMemoryRuntimeCoordinator,
    RedisRuntimeCoordinator,
    RuntimeCoordinator,
    parse_domain_limits,
)
from .service import BuyerService
from .settings import Settings, get_settings
from .state import (
    InMemorySessionRepository,
    ReplyValidationError,
    SessionConflictError,
    SessionNotFoundError,
    SessionState,
    SessionStore,
)


def _build_session_store(settings: Settings) -> SessionStore:
    if settings.state_backend == 'postgres':
        repository = PostgresSessionRepository(
            database_url=settings.database_url,
            min_pool_size=settings.postgres_pool_min_size,
            max_pool_size=settings.postgres_pool_max_size,
        )
    else:
        repository = InMemorySessionRepository()
    return SessionStore(
        repository=repository,
        max_active_jobs_per_worker=settings.max_active_jobs_per_worker,
        domain_active_limit_default=settings.domain_active_limit_default,
        domain_active_limits=parse_domain_limits(settings.domain_active_limits),
        status_ttl_sec=settings.status_ttl_sec,
    )


def _build_runtime_coordinator(settings: Settings) -> RuntimeCoordinator:
    domain_limits = parse_domain_limits(settings.domain_active_limits)
    if settings.runtime_backend == 'redis':
        return RedisRuntimeCoordinator(
            redis_url=settings.redis_url,
            key_prefix=settings.redis_key_prefix,
            worker_id=settings.buyer_worker_id,
            max_active_jobs_per_worker=settings.max_active_jobs_per_worker,
            max_handoff_sessions=settings.max_handoff_sessions,
            domain_active_limit_default=settings.domain_active_limit_default,
            domain_active_limits=domain_limits,
            lock_ttl_sec=settings.runtime_lock_ttl_sec,
            marker_ttl_sec=settings.runtime_marker_ttl_sec,
        )
    return InMemoryRuntimeCoordinator(
        worker_id=settings.buyer_worker_id,
        max_active_jobs_per_worker=settings.max_active_jobs_per_worker,
        max_handoff_sessions=settings.max_handoff_sessions,
        domain_active_limit_default=settings.domain_active_limit_default,
        domain_active_limits=domain_limits,
        lock_ttl_sec=settings.runtime_lock_ttl_sec,
        marker_ttl_sec=settings.runtime_marker_ttl_sec,
    )


settings = get_settings()
runtime_coordinator = _build_runtime_coordinator(settings)
store = _build_session_store(settings)
callback_client = CallbackClient(settings, runtime_coordinator=runtime_coordinator)
runner = AgentRunner(settings)
knowledge_analyzer = PostSessionKnowledgeAnalyzer(settings)
auth_script_runner = SberIdScriptRunner(
    scripts_dir=settings.auth_scripts_dir,
    cdp_endpoint=settings.browser_cdp_endpoint,
    timeout_sec=settings.auth_script_timeout_sec,
    trace_dir=settings.buyer_trace_dir,
)
purchase_script_runner = PurchaseScriptRunner(
    scripts_dir=settings.auth_scripts_dir,
    cdp_endpoint=settings.browser_cdp_endpoint,
    timeout_sec=settings.purchase_script_timeout_sec,
    trace_dir=settings.buyer_trace_dir,
)
service = BuyerService(
    store=store,
    callback_client=callback_client,
    runner=runner,
    novnc_url=settings.novnc_public_url,
    default_callback_url=settings.middle_callback_url,
    cdp_recovery_window_sec=settings.cdp_recovery_window_sec,
    cdp_recovery_interval_ms=settings.cdp_recovery_interval_ms,
    sberid_allowlist=parse_allowlist(settings.sberid_allowlist),
    sberid_auth_retry_budget=settings.sberid_auth_retry_budget,
    auth_script_runner=auth_script_runner,
    purchase_script_allowlist=parse_allowlist(settings.purchase_script_allowlist),
    purchase_script_runner=purchase_script_runner,
    knowledge_analyzer=knowledge_analyzer,
    runtime_coordinator=runtime_coordinator,
)

app = FastAPI(title='buyer-mvp', version='0.1.0')


@app.get('/healthz')
async def healthz() -> dict[str, str]:
    return {'status': 'ok'}


@app.on_event('startup')
async def startup() -> None:
    await runtime_coordinator.initialize()
    await store.initialize()


@app.on_event('shutdown')
async def shutdown() -> None:
    await service.shutdown_post_session_analysis()
    await callback_client.aclose()
    await store.aclose()
    await runtime_coordinator.aclose()


@app.post('/v1/tasks', response_model=TaskCreateResponse, status_code=201)
async def create_task(request: TaskCreateRequest) -> TaskCreateResponse:
    try:
        state = await service.create_session(
            task=request.task,
            start_url=request.start_url,
            callback_url=request.callback_url,
            metadata=request.metadata,
            auth=request.auth,
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
