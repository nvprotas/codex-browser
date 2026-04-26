from __future__ import annotations

import asyncio
import logging
from datetime import timezone
from random import random
from uuid import uuid4

import httpx

from .models import EventEnvelope
from .runtime import RuntimeCoordinator
from .settings import Settings

logger = logging.getLogger('uvicorn.error')


class CallbackDeliveryError(RuntimeError):
    pass


class CallbackClient:
    def __init__(self, settings: Settings, runtime_coordinator: RuntimeCoordinator | None = None) -> None:
        self._settings = settings
        self._runtime_coordinator = runtime_coordinator
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
            await self._record_callback_attempt_marker(envelope, attempt=attempt, attempts_total=attempts)
            try:
                response = await self._client.post(callback_url, json=envelope.model_dump(mode='json'))
                response.raise_for_status()
                await self._clear_callback_attempt_marker(envelope.event_id)
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

    async def _record_callback_attempt_marker(self, envelope: EventEnvelope, *, attempt: int, attempts_total: int) -> None:
        if self._runtime_coordinator is None:
            return
        try:
            await self._runtime_coordinator.record_callback_attempt(
                session_id=envelope.session_id,
                event_id=envelope.event_id,
                event_type=envelope.event_type,
                attempt=attempt,
                attempts_total=attempts_total,
            )
        except Exception as exc:  # noqa: BLE001 - marker не должен блокировать сам callback
            logger.warning('callback_attempt_marker_failed event_id=%s error=%s', envelope.event_id, exc)

    async def _clear_callback_attempt_marker(self, event_id: str) -> None:
        if self._runtime_coordinator is None:
            return
        try:
            await self._runtime_coordinator.clear_callback_attempt(event_id)
        except Exception as exc:  # noqa: BLE001 - marker не должен превращать успешный callback в failure
            logger.warning('callback_attempt_marker_clear_failed event_id=%s error=%s', event_id, exc)

    @staticmethod
    def _utc_now():
        from datetime import datetime

        return datetime.now(timezone.utc)
