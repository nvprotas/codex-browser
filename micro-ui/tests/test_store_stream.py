from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import EventEnvelope
from app.store import CallbackStore


def _event(*, event_id: str = 'event-1', idempotency_key: str = 'session-1:key') -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        session_id='session-1',
        event_type='agent_stream_event',
        occurred_at=datetime.now(timezone.utc),
        idempotency_key=idempotency_key,
        payload={
            'step': 1,
            'source': 'browser',
            'stream': 'browser_actions',
            'sequence': 1,
            'items': [{'command': 'goto'}],
            'message': 'goto',
        },
    )


class CallbackStoreStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_publishes_accepted_event_to_session_subscriber(self) -> None:
        store = CallbackStore()
        queue = await store.subscribe('session-1')

        try:
            accepted = await store.add(_event())

            received = await asyncio.wait_for(queue.get(), timeout=0.2)
        finally:
            await store.unsubscribe('session-1', queue)

        self.assertTrue(accepted)
        self.assertEqual(received.event_type, 'agent_stream_event')
        self.assertEqual(received.payload['items'][0]['command'], 'goto')

    async def test_store_does_not_publish_duplicate_event(self) -> None:
        store = CallbackStore()
        queue = await store.subscribe('session-1')

        try:
            first = await store.add(_event())
            duplicate = await store.add(_event(event_id='event-2'))
            received = await asyncio.wait_for(queue.get(), timeout=0.2)
        finally:
            await store.unsubscribe('session-1', queue)

        self.assertTrue(first)
        self.assertFalse(duplicate)
        self.assertEqual(received.event_id, 'event-1')
        self.assertTrue(queue.empty())
