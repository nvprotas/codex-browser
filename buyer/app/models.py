from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionStatus(StrEnum):
    QUEUED = 'queued'
    CREATED = 'created'
    RUNNING = 'running'
    WAITING_USER = 'waiting_user'
    COMPLETED = 'completed'
    FAILED = 'failed'


class AuthProvider(StrEnum):
    SBERID = 'sberid'


class TaskAuthPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra='ignore')

    provider: str = Field(default=AuthProvider.SBERID.value, description='Провайдер авторизации')
    storage_state: dict[str, Any] | None = Field(
        default=None,
        alias='storageState',
        description='Playwright storageState для восстановления сессии',
    )


class TaskCreateRequest(BaseModel):
    task: str = Field(min_length=1, description='Текст задачи для агента buyer')
    start_url: str = Field(min_length=1, description='URL магазина для начала сценария')
    callback_url: str | None = Field(default=None, description='Куда buyer отправляет callback-события')
    metadata: dict[str, Any] = Field(default_factory=dict)
    auth: TaskAuthPayload | None = Field(default=None, description='Опциональный auth-пакет для SberId')


class TaskCreateResponse(BaseModel):
    session_id: str
    status: SessionStatus
    novnc_url: str


class SessionReplyRequest(BaseModel):
    session_id: str
    reply_id: str
    message: str = Field(min_length=1)


class SessionReplyResponse(BaseModel):
    session_id: str
    accepted: bool
    status: SessionStatus
    reason_code: str | None = None


class EventEnvelope(BaseModel):
    event_id: str
    session_id: str
    event_type: str
    occurred_at: datetime
    idempotency_key: str
    payload: dict[str, Any]


class SessionView(BaseModel):
    session_id: str
    status: SessionStatus
    start_url: str
    callback_url: str
    novnc_url: str
    created_at: datetime
    updated_at: datetime
    waiting_reply_id: str | None = None
    waiting_deadline_at: datetime | None = None
    browser_slot_id: str | None = None
    last_error: str | None = None


class SessionDetail(SessionView):
    events: list[EventEnvelope] = Field(default_factory=list)


class AgentOutput(BaseModel):
    status: str = Field(description='needs_user_input|completed|failed')
    message: str
    order_id: str | None = None
    artifacts: dict[str, Any] = Field(default_factory=dict)


class CallbackAck(BaseModel):
    accepted: bool
    duplicate: bool = False
