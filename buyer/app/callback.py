from __future__ import annotations

import asyncio
from datetime import timezone
from random import random
from uuid import uuid4

import httpx

from .models import EventEnvelope
from .settings import Settings


class CallbackDeliveryError(RuntimeError):
    pass


class CallbackClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(settings.callback_timeout_sec))

    def build_envelope(self, session_id: str, event_type: str, payload: dict, idempotency_suffix: str | None = None) -> EventEnvelope:
        event_id = str(uuid4())
        idempotency = f'{session_id}:{event_type}:{idempotency_suffix or event_id}'
        return EventEnvelope(
            event_id=event_id,
            session_id=session_id,
            event_type=event_type,
            occurred_at=self._utc_now(),
            idempotency_key=idempotency,
            payload=payload,
        )

    async def deliver(self, callback_url: str, envelope: EventEnvelope) -> None:
        attempts = self._settings.callback_retries
        backoff = self._settings.callback_backoff_sec

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.post(callback_url, json=envelope.model_dump(mode='json'))
                response.raise_for_status()
                return
            except Exception as exc:  # noqa: BLE001 - важно сохранить первопричину доставки
                last_error = exc
                if attempt == attempts:
                    break
                # Небольшой jitter, чтобы повторные попытки не шли синхронно пачками.
                await asyncio.sleep(backoff * (2 ** (attempt - 1)) + random() * 0.2)

        raise CallbackDeliveryError(f'Не удалось доставить callback {envelope.event_type}: {last_error}')

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _utc_now():
        from datetime import datetime

        return datetime.now(timezone.utc)
