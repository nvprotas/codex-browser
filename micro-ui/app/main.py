from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import (
    CallbackAck,
    EventEnvelope,
    ReplySubmitRequest,
    ReplySubmitResponse,
    SessionSummary,
    StopSessionRequest,
    StopSessionResponse,
    TaskCreateRequest,
    TaskCreateResponse,
)
from .settings import get_settings
from .store import CallbackStore

settings = get_settings()
store = CallbackStore()

app = FastAPI(title='micro-ui-mvp', version='0.1.0')

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / 'templates'))
app.mount('/static', StaticFiles(directory=str(BASE_DIR / 'static')), name='static')


def eval_proxy_timeout(path: str, method: str) -> httpx.Timeout:
    if method.upper() == 'POST' and path.strip('/') == 'runs':
        return httpx.Timeout(650.0)
    return httpx.Timeout(60.0)


@app.get('/healthz')
async def healthz() -> dict[str, str]:
    return {'status': 'ok'}


@app.get('/', response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name='index.html',
        context={
            'poll_interval_ms': settings.ui_poll_interval_sec * 1000,
            'eval_service_public_base_url': '/api/eval',
        },
    )


@app.post('/callbacks', response_model=CallbackAck)
async def callbacks(envelope: EventEnvelope) -> CallbackAck:
    accepted = await store.add(envelope)
    return CallbackAck(accepted=accepted, duplicate=not accepted)


@app.get('/api/events', response_model=list[EventEnvelope])
async def api_events(session_id: str | None = None) -> list[EventEnvelope]:
    return await store.list_events(session_id=session_id)


@app.get('/api/events/stream')
async def api_events_stream(request: Request, session_id: str | None = None) -> StreamingResponse:
    queue = await store.subscribe(session_id) if session_id else await store.subscribe_all()

    async def event_source():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    envelope = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'
                    continue
                yield f'data: {envelope.model_dump_json()}\n\n'
        finally:
            if session_id:
                await store.unsubscribe(session_id, queue)
            else:
                await store.unsubscribe_all(queue)

    return StreamingResponse(
        event_source(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.get('/api/sessions', response_model=list[SessionSummary])
async def api_sessions() -> list[SessionSummary]:
    return await store.list_sessions()


@app.api_route('/api/eval/{path:path}', methods=['GET', 'POST'])
async def api_eval_proxy(path: str, request: Request) -> Response:
    target = f"{settings.eval_service_base_url.rstrip('/')}/{path}"
    body = await request.body()
    headers = {'Content-Type': request.headers.get('content-type', 'application/json')}

    try:
        timeout = eval_proxy_timeout(path, request.method)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                request.method,
                target,
                params=request.query_params,
                content=body if body else None,
                headers=headers,
            )
            response.raise_for_status()
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=response.headers.get('content-type', 'application/json'),
            )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text) from exc
    except Exception as exc:  # noqa: BLE001 - UI должен показать причину eval_service
        raise HTTPException(status_code=502, detail=f'Не удалось обратиться к eval_service: {exc}') from exc


@app.post('/api/tasks', response_model=TaskCreateResponse, status_code=201)
async def api_tasks(request: TaskCreateRequest) -> TaskCreateResponse:
    target = f"{settings.buyer_base_url}/v1/tasks"
    payload = request.model_dump()

    try:
        timeout = httpx.Timeout(15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(target, json=payload)
            data = response.json()
            response.raise_for_status()
            return TaskCreateResponse.model_validate(data)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001 - пробрасываем причину для UI
        raise HTTPException(status_code=502, detail=f'Не удалось запустить задачу в buyer: {exc}') from exc


@app.post('/api/reply', response_model=ReplySubmitResponse)
async def api_reply(request: ReplySubmitRequest) -> ReplySubmitResponse:
    target = f"{settings.buyer_base_url}/v1/replies"
    payload = request.model_dump()

    try:
        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(target, json=payload)
            data = response.json()
            response.raise_for_status()
            return ReplySubmitResponse(forwarded=True, buyer_response=data)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001 - пробрасываем причину для UI
        raise HTTPException(status_code=502, detail=f'Не удалось отправить reply в buyer: {exc}') from exc


@app.post('/api/sessions/{session_id}/stop', response_model=StopSessionResponse)
async def api_stop_session(session_id: str, request: StopSessionRequest | None = None) -> StopSessionResponse:
    target = f"{settings.buyer_base_url}/v1/sessions/{session_id}/stop"
    payload = request.model_dump() if request is not None else {}

    try:
        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(target, json=payload)
            data = response.json()
            response.raise_for_status()
            return StopSessionResponse(forwarded=True, buyer_response=data)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001 - пробрасываем причину для UI
        raise HTTPException(status_code=502, detail=f'Не удалось остановить сессию в buyer: {exc}') from exc
