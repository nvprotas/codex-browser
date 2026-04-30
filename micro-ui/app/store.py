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
        self._subscribers_by_session: dict[str, set[asyncio.Queue[EventEnvelope]]] = {}
        self._global_subscribers: set[asyncio.Queue[EventEnvelope]] = set()

    async def add(self, envelope: EventEnvelope) -> bool:
        async with self._lock:
            if envelope.event_id in self._seen_event_ids or envelope.idempotency_key in self._seen_idempotency_keys:
                return False

            self._seen_event_ids.add(envelope.event_id)
            self._seen_idempotency_keys.add(envelope.idempotency_key)
            bucket = self._events_by_session.setdefault(envelope.session_id, [])
            bucket.append(envelope)
            self._publish_locked(envelope)
            return True

    async def subscribe(self, session_id: str) -> asyncio.Queue[EventEnvelope]:
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._subscribers_by_session.setdefault(session_id, set()).add(queue)
        return queue

    async def subscribe_all(self) -> asyncio.Queue[EventEnvelope]:
        queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=200)
        async with self._lock:
            self._global_subscribers.add(queue)
        return queue

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue[EventEnvelope]) -> None:
        async with self._lock:
            subscribers = self._subscribers_by_session.get(session_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers_by_session.pop(session_id, None)

    async def unsubscribe_all(self, queue: asyncio.Queue[EventEnvelope]) -> None:
        async with self._lock:
            self._global_subscribers.discard(queue)

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
                ask_question = None
                ask_options: list[str] = []
                ask_asked_at = None
                order_id = None
                order_id_host = None
                status = None
                novnc_url = None

                for event in events:
                    if event.event_type == 'ask_user':
                        waiting_reply_id = event.payload.get('reply_id')
                        message = event.payload.get('message')
                        question = message if isinstance(message, str) and message else event.payload.get('question')
                        options = event.payload.get('options')
                        ask_question = question if isinstance(question, str) else None
                        ask_options = (
                            [item for item in options if isinstance(item, str)]
                            if isinstance(options, list)
                            else []
                        )
                        ask_asked_at = event.occurred_at
                    if event.event_type in {'agent_step_started', 'handoff_resumed', 'payment_ready', 'operator_reply'}:
                        waiting_reply_id = None
                        ask_question = None
                        ask_options = []
                        ask_asked_at = None
                    if event.event_type == 'payment_ready':
                        raw_order_id = event.payload.get('order_id')
                        raw_order_id_host = event.payload.get('order_id_host')
                        order_id = raw_order_id if isinstance(raw_order_id, str) and raw_order_id else None
                        order_id_host = (
                            raw_order_id_host
                            if isinstance(raw_order_id_host, str) and raw_order_id_host
                            else None
                        )
                    if event.event_type == 'scenario_finished':
                        status = event.payload.get('status')
                        waiting_reply_id = None
                        ask_question = None
                        ask_options = []
                        ask_asked_at = None
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
                        ask_question=ask_question,
                        ask_options=ask_options,
                        ask_asked_at=ask_asked_at,
                        order_id=order_id,
                        order_id_host=order_id_host,
                        status=status,
                        novnc_url=novnc_url,
                        updated_at=last_event.occurred_at,
                    )
                )

            summaries.sort(key=lambda item: item.updated_at, reverse=True)
            return summaries

    def _publish_locked(self, envelope: EventEnvelope) -> None:
        subscribers = self._subscribers_by_session.get(envelope.session_id, set())
        for queue in list(subscribers):
            _offer(queue, envelope)
        for queue in list(self._global_subscribers):
            _offer(queue, envelope)


def _offer(queue: asyncio.Queue[EventEnvelope], envelope: EventEnvelope) -> None:
    try:
        queue.put_nowait(envelope)
        return
    except asyncio.QueueFull:
        pass

    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        queue.put_nowait(envelope)
    except asyncio.QueueFull:
        pass

def _extract_message(event: EventEnvelope) -> str | None:
    for key in ('message', 'question', 'summary'):
        value = event.payload.get(key)
        if isinstance(value, str):
            return value
    return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
