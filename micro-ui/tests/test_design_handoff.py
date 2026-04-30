from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import EventEnvelope
from app.store import CallbackStore


def _ask_event() -> EventEnvelope:
    return EventEnvelope(
        event_id='evt-ask',
        session_id='session-ask',
        event_type='ask_user',
        occurred_at=datetime(2026, 4, 28, 11, 18, 12, tzinfo=timezone.utc),
        idempotency_key='session-ask:ask',
        payload={
            'reply_id': 'reply-1',
            'question': 'Подтвердите адрес доставки?',
            'options': ['Да', 'Другой адрес'],
        },
    )


def _session_event(
    *,
    event_id: str,
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, object] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        session_id='session-ask',
        event_type=event_type,
        occurred_at=occurred_at,
        idempotency_key=f'session-ask:{event_id}',
        payload=payload or {},
    )


class CallbackStoreAskUserSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_sessions_exposes_ask_user_context(self) -> None:
        store = CallbackStore()

        accepted = await store.add(_ask_event())
        summaries = await store.list_sessions()

        self.assertTrue(accepted)
        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary.status, 'waiting_user')
        self.assertEqual(summary.waiting_reply_id, 'reply-1')
        self.assertEqual(summary.last_message, 'Подтвердите адрес доставки?')
        self.assertEqual(summary.ask_question, 'Подтвердите адрес доставки?')
        self.assertEqual(summary.ask_options, ['Да', 'Другой адрес'])
        self.assertEqual(summary.ask_asked_at, datetime(2026, 4, 28, 11, 18, 12, tzinfo=timezone.utc))

    async def test_list_sessions_prefers_message_over_legacy_question(self) -> None:
        store = CallbackStore()

        await store.add(
            _session_event(
                event_id='evt-ask-message',
                event_type='ask_user',
                occurred_at=datetime(2026, 4, 28, 11, 18, 12, tzinfo=timezone.utc),
                payload={
                    'reply_id': 'reply-1',
                    'message': 'Канонический вопрос из message',
                    'question': 'Legacy question',
                    'options': ['Да'],
                },
            )
        )
        summaries = await store.list_sessions()

        summary = summaries[0]
        self.assertEqual(summary.last_message, 'Канонический вопрос из message')
        self.assertEqual(summary.ask_question, 'Канонический вопрос из message')
        self.assertEqual(summary.ask_options, ['Да'])

    async def test_list_sessions_clears_waiting_context_on_agent_progression(self) -> None:
        store = CallbackStore()

        await store.add(_ask_event())
        await store.add(
            _session_event(
                event_id='evt-step-started',
                event_type='agent_step_started',
                occurred_at=datetime(2026, 4, 28, 11, 19, 12, tzinfo=timezone.utc),
                payload={'step': 2, 'message': 'Продолжаю сценарий.'},
            )
        )
        summaries = await store.list_sessions()

        summary = summaries[0]
        self.assertEqual(summary.status, 'running')
        self.assertIsNone(summary.waiting_reply_id)
        self.assertIsNone(summary.ask_question)
        self.assertEqual(summary.ask_options, [])
        self.assertIsNone(summary.ask_asked_at)

    async def test_list_sessions_exposes_payment_ready_order_host(self) -> None:
        store = CallbackStore()

        await store.add(
            _session_event(
                event_id='evt-payment-ready',
                event_type='payment_ready',
                occurred_at=datetime(2026, 4, 28, 11, 20, 12, tzinfo=timezone.utc),
                payload={
                    'order_id': 'brandshop-order-123',
                    'order_id_host': 'yoomoney.ru',
                    'message': 'Платежный шаг готов.',
                },
            )
        )
        summaries = await store.list_sessions()

        summary = summaries[0]
        self.assertEqual(summary.status, 'running')
        self.assertEqual(summary.order_id, 'brandshop-order-123')
        self.assertEqual(summary.order_id_host, 'yoomoney.ru')
        self.assertEqual(summary.last_message, 'Платежный шаг готов.')
