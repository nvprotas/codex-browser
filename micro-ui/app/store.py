from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .models import EventEnvelope, SessionSummary


class CallbackStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._events_by_session: dict[str, list[EventEnvelope]] = {}
        self._seen_event_ids: set[str] = set()
        self._seen_idempotency_keys: set[str] = set()

    async def add(self, envelope: EventEnvelope) -> bool:
        async with self._lock:
            if envelope.event_id in self._seen_event_ids or envelope.idempotency_key in self._seen_idempotency_keys:
                return False

            self._seen_event_ids.add(envelope.event_id)
            self._seen_idempotency_keys.add(envelope.idempotency_key)
            bucket = self._events_by_session.setdefault(envelope.session_id, [])
            bucket.append(envelope)
            return True

    async def list_events(self, session_id: str | None = None) -> list[EventEnvelope]:
        async with self._lock:
            if session_id is not None:
                return list(self._events_by_session.get(session_id, []))

            all_events: list[EventEnvelope] = []
            for events in self._events_by_session.values():
                all_events.extend(events)
            all_events.sort(key=lambda item: item.occurred_at)
            return all_events

    async def list_sessions(self) -> list[SessionSummary]:
        async with self._lock:
            summaries: list[SessionSummary] = []
            for session_id, events in self._events_by_session.items():
                if not events:
                    continue
                last_event = events[-1]
                waiting_reply_id = None
                order_id = None
                status = None
                novnc_url = None

                for event in events:
                    if event.event_type == 'ask_user':
                        waiting_reply_id = event.payload.get('reply_id')
                    if event.event_type == 'payment_ready':
                        order_id = event.payload.get('order_id')
                    if event.event_type == 'scenario_finished':
                        status = event.payload.get('status')
                        waiting_reply_id = None
                    if event.event_type == 'session_started':
                        novnc_url = event.payload.get('novnc_url')

                if status is None:
                    status = 'running' if waiting_reply_id is None else 'waiting_user'

                summaries.append(
                    SessionSummary(
                        session_id=session_id,
                        last_event_type=last_event.event_type,
                        last_message=_extract_message(last_event),
                        waiting_reply_id=waiting_reply_id,
                        order_id=order_id,
                        status=status,
                        novnc_url=novnc_url,
                        updated_at=last_event.occurred_at,
                    )
                )

            summaries.sort(key=lambda item: item.updated_at, reverse=True)
            return summaries



def _extract_message(event: EventEnvelope) -> str | None:
    value = event.payload.get('message')
    if isinstance(value, str):
        return value
    return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
