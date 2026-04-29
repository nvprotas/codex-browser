from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import main as micro_main
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
    async def test_store_publishes_accepted_event_to_global_subscriber(self) -> None:
        store = CallbackStore()
        queue = await store.subscribe_all()

        try:
            accepted = await store.add(_event())

            received = await asyncio.wait_for(queue.get(), timeout=0.2)
        finally:
            await store.unsubscribe_all(queue)

        self.assertTrue(accepted)
        self.assertEqual(received.session_id, 'session-1')
        self.assertEqual(received.event_type, 'agent_stream_event')

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


class _FakeBuyerResponse:
    status_code = 200
    content = b'{"session_id":"session-1","accepted":true,"status":"failed"}'
    headers = {'content-type': 'application/json'}

    def json(self) -> dict[str, Any]:
        return {'session_id': 'session-1', 'accepted': True, 'status': 'failed'}

    def raise_for_status(self) -> None:
        return


class _RecordingAsyncClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, *, timeout: Any) -> None:
        self.timeout = timeout

    async def __aenter__(self) -> _RecordingAsyncClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return

    async def post(self, target: str, *, json: dict[str, Any]) -> _FakeBuyerResponse:
        self.calls.append({'target': target, 'json': json, 'timeout': self.timeout})
        return _FakeBuyerResponse()


class MicroUiStopProxyTests(unittest.TestCase):
    def test_api_stop_session_forwards_to_buyer(self) -> None:
        _RecordingAsyncClient.calls = []

        with patch.object(micro_main.httpx, 'AsyncClient', new=_RecordingAsyncClient):
            response = TestClient(micro_main.app).post(
                '/api/sessions/session-1/stop',
                json={'reason': 'Оператор остановил сценарий'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                'forwarded': True,
                'buyer_response': {'session_id': 'session-1', 'accepted': True, 'status': 'failed'},
            },
        )
        self.assertEqual(len(_RecordingAsyncClient.calls), 1)
        self.assertTrue(_RecordingAsyncClient.calls[0]['target'].endswith('/v1/sessions/session-1/stop'))
        self.assertEqual(_RecordingAsyncClient.calls[0]['json'], {'reason': 'Оператор остановил сценарий'})
