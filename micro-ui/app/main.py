from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .models import (
    CallbackAck,
    EventEnvelope,
    ReplySubmitRequest,
    ReplySubmitResponse,
    SessionSummary,
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
        },
    )


@app.post('/callbacks', response_model=CallbackAck)
async def callbacks(envelope: EventEnvelope) -> CallbackAck:
    accepted = await store.add(envelope)
    return CallbackAck(accepted=accepted, duplicate=not accepted)


@app.get('/api/events', response_model=list[EventEnvelope])
async def api_events(session_id: str | None = None) -> list[EventEnvelope]:
    return await store.list_events(session_id=session_id)


@app.get('/api/sessions', response_model=list[SessionSummary])
async def api_sessions() -> list[SessionSummary]:
    return await store.list_sessions()


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
