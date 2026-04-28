from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field


DEFAULT_BUYER_API_BASE_URL = 'http://buyer:8000'


class BuyerTaskAuthPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra='forbid')

    provider: str = 'sberid'
    storage_state: dict[str, Any] = Field(alias='storageState')


class BuyerTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    task: str = Field(min_length=1)
    start_url: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    callback_url: str | None = None
    auth: BuyerTaskAuthPayload | None = None


class BuyerTaskCreateResponse(BaseModel):
    session_id: str
    status: str
    novnc_url: str


class BuyerSessionDetail(BaseModel):
    session_id: str
    status: str
    start_url: str
    callback_url: str
    novnc_url: str
    created_at: datetime
    updated_at: datetime
    waiting_reply_id: str | None = None
    last_error: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class BuyerSessionReplyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    session_id: str = Field(min_length=1)
    reply_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class BuyerSessionReplyResponse(BaseModel):
    session_id: str
    accepted: bool
    status: str


class BuyerClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BUYER_API_BASE_URL,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | float = 30.0,
    ) -> None:
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> BuyerClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def create_task(
        self,
        *,
        task: str,
        start_url: str,
        metadata: dict[str, Any] | None = None,
        callback_url: str | None = None,
        storage_state: dict[str, Any] | None = None,
    ) -> BuyerTaskCreateResponse:
        request = BuyerTaskCreateRequest(
            task=task,
            start_url=start_url,
            metadata=metadata or {},
            callback_url=callback_url,
            auth=BuyerTaskAuthPayload(storage_state=storage_state) if storage_state is not None else None,
        )
        response = await self._client.post(
            '/v1/tasks',
            json=request.model_dump(mode='json', by_alias=True, exclude_none=True),
        )
        response.raise_for_status()
        return BuyerTaskCreateResponse.model_validate(response.json())

    async def get_session(self, session_id: str) -> BuyerSessionDetail:
        response = await self._client.get(f'/v1/sessions/{session_id}')
        response.raise_for_status()
        return BuyerSessionDetail.model_validate(response.json())

    async def send_reply(
        self,
        *,
        session_id: str,
        reply_id: str,
        message: str,
    ) -> BuyerSessionReplyResponse:
        request = BuyerSessionReplyRequest(
            session_id=session_id,
            reply_id=reply_id,
            message=message,
        )
        response = await self._client.post('/v1/replies', json=request.model_dump(mode='json'))
        response.raise_for_status()
        return BuyerSessionReplyResponse.model_validate(response.json())
