from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EventEnvelope(BaseModel):
    event_id: str
    session_id: str
    event_type: str
    occurred_at: datetime
    idempotency_key: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CallbackAck(BaseModel):
    accepted: bool
    duplicate: bool = False


class ReplySubmitRequest(BaseModel):
    session_id: str
    reply_id: str
    message: str = Field(min_length=1)


class ReplySubmitResponse(BaseModel):
    forwarded: bool
    buyer_response: dict[str, Any]


class TaskCreateRequest(BaseModel):
    task: str = Field(min_length=1)
    start_url: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCreateResponse(BaseModel):
    session_id: str
    status: str
    novnc_url: str


class SessionSummary(BaseModel):
    session_id: str
    last_event_type: str
    last_message: str | None = None
    waiting_reply_id: str | None = None
    order_id: str | None = None
    status: str | None = None
    novnc_url: str | None = None
    updated_at: datetime
