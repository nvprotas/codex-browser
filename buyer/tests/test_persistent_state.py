from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timezone

from buyer.app.models import EventEnvelope, SessionStatus, TaskAuthPayload
from buyer.app.persistence import (
    SCHEMA_MIGRATIONS,
    PostgresSessionRepository,
    _build_artifact_refs,
    _sanitize_auth_context,
    _sanitize_persistent_metadata,
)
from buyer.app.state import InMemorySessionRepository, ReplyValidationError, SessionStore


def _event(session_id: str, event_type: str = 'session_started') -> EventEnvelope:
    return EventEnvelope(
        event_id=f'event-{event_type}',
        session_id=session_id,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        idempotency_key=f'{session_id}:{event_type}:test',
        payload={'message': 'ok'},
    )


class PersistentStateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_restores_session_events_replies_and_memory_between_instances(self) -> None:
        repository = InMemorySessionRepository()
        first = SessionStore(repository=repository, max_active_sessions=1)
        created = await first.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={'city': 'Москва'},
            auth=None,
        )

        await first.set_status(created.session_id, SessionStatus.RUNNING)
        await first.append_event(created.session_id, _event(created.session_id))
        await first.add_agent_memory(created.session_id, 'user', 'Купить книгу')
        await first.set_waiting_question(created.session_id, 'Какую книгу искать?', 'reply-1')

        second = SessionStore(repository=repository, max_active_sessions=1)
        restored = await second.get(created.session_id)

        self.assertEqual(restored.status, SessionStatus.WAITING_USER)
        self.assertEqual(restored.waiting_reply_id, 'reply-1')
        self.assertEqual(restored.waiting_question, 'Какую книгу искать?')
        self.assertEqual(restored.metadata, {'city': 'Москва'})
        self.assertEqual([event.event_type for event in restored.events], ['session_started'])
        self.assertEqual(await second.get_agent_memory(created.session_id), [{'role': 'user', 'text': 'Купить книгу'}])

    async def test_active_runtime_store_accepts_waiting_reply(self) -> None:
        repository = InMemorySessionRepository()
        store = SessionStore(repository=repository, max_active_sessions=1)
        created = await store.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )
        await store.set_waiting_question(created.session_id, 'Какую книгу искать?', 'reply-1')

        replied = await store.apply_reply(created.session_id, 'reply-1', 'Одиссея')
        self.assertEqual(replied.status, SessionStatus.RUNNING)
        self.assertEqual(await store.pop_reply(created.session_id), 'Одиссея')
        with self.assertRaises(ReplyValidationError):
            await store.pop_reply(created.session_id)

    async def test_restarted_store_rejects_reply_for_stale_waiting_session_without_runtime_runner(self) -> None:
        repository = InMemorySessionRepository()
        first = SessionStore(repository=repository, max_active_sessions=1)
        created = await first.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )
        await first.set_waiting_question(created.session_id, 'Какую книгу искать?', 'reply-1')

        restarted = SessionStore(repository=repository, max_active_sessions=1)

        with self.assertRaises(ReplyValidationError) as caught:
            await restarted.apply_reply(created.session_id, 'reply-1', 'Одиссея')

        self.assertIn('активного runner', str(caught.exception))
        restored = await restarted.get(created.session_id)
        self.assertEqual(restored.status, SessionStatus.WAITING_USER)
        self.assertEqual(restored.waiting_reply_id, 'reply-1')

    async def test_persistent_backend_does_not_restore_storage_state(self) -> None:
        repository = InMemorySessionRepository(persist_auth_payload=False)
        auth = TaskAuthPayload(
            storageState={
                'cookies': [{'name': 'sid', 'value': 'secret'}],
                'origins': [{'origin': 'https://example.com', 'localStorage': [{'name': 'token', 'value': 'secret'}]}],
            }
        )
        first = SessionStore(repository=repository, max_active_sessions=1)
        created = await first.create_session(
            task='Купить товар',
            start_url='https://example.com/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=auth,
        )

        self.assertIsNotNone(created.auth)

        second = SessionStore(repository=repository, max_active_sessions=1)
        restored = await second.get(created.session_id)

        self.assertIsNone(restored.auth)

    async def test_schema_migrations_define_required_persistent_tables_without_storage_state_column(self) -> None:
        sql = '\n'.join(migration_sql for _, migration_sql in SCHEMA_MIGRATIONS)

        for table in (
            'buyer_sessions',
            'buyer_events',
            'buyer_replies',
            'buyer_artifacts',
            'buyer_auth_context',
            'buyer_agent_memory',
        ):
            self.assertIn(table, sql)

        self.assertNotIn('storage_state', sql.lower())
        self.assertNotIn('storageState', sql)

    async def test_persistent_auth_and_artifact_metadata_redacts_storage_state(self) -> None:
        context = _sanitize_auth_context(
            {
                'provider': 'sberid',
                'storageState': {'cookies': [{'name': 'sid', 'value': 'secret'}], 'origins': []},
                'artifacts': {'storage_state_path': '/tmp/auth-storage-attempt-01.json', 'trace_path': '/tmp/auth-trace.jsonl'},
            }
        )
        refs = _build_artifact_refs(
            session_id='session-1',
            artifacts=[
                {
                    'auth': context,
                    'trace': {'trace_file': '/tmp/step-trace.json'},
                    'storage_state_path': '/tmp/auth-storage-attempt-01.json',
                }
            ],
        )
        dumped = str(context) + ' ' + ' '.join(f'{ref.uri} {ref.metadata}' for ref in refs)

        self.assertIn('/tmp/auth-trace.jsonl', dumped)
        self.assertIn('/tmp/step-trace.json', dumped)
        self.assertNotIn('secret', dumped)
        self.assertNotIn('auth-storage-attempt-01', dumped)
        self.assertNotIn('storageState', dumped)

    async def test_event_payload_for_storage_redacts_sensitive_auth_data(self) -> None:
        from buyer.app.persistence import _serialize_event_payload_for_storage

        payload = {
            'status': 'completed',
            'message': 'ok',
            'order_id': 'order-123',
            'artifacts': {
                'trace': {'trace_file': '/tmp/step-trace.json'},
                'stdout_tail': 'stdout-tail-secret',
                'stderr_tail': 'stderr-tail-secret',
                'auth': {
                    'provider': 'sberid',
                    'storageState': {
                        'cookies': [{'name': 'sid', 'value': 'cookie-secret'}],
                        'origins': [{'origin': 'https://example.com', 'localStorage': [{'name': 'token', 'value': 'ls-secret'}]}],
                    },
                    'storage_state_path': '/tmp/auth-storage-attempt-01.json',
                    'accessToken': 'access-secret',
                    'refresh_token': 'refresh-secret',
                },
                'authorization': 'Bearer auth-secret',
            },
        }

        stored = _serialize_event_payload_for_storage(payload)
        dumped = json.dumps(stored, ensure_ascii=False)

        self.assertIn('/tmp/step-trace.json', dumped)
        self.assertIn('order-123', dumped)
        for forbidden in (
            'cookie-secret',
            'ls-secret',
            'auth-storage-attempt-01',
            'access-secret',
            'refresh-secret',
            'auth-secret',
            'stdout-tail-secret',
            'stderr-tail-secret',
            'storageState',
        ):
            self.assertNotIn(forbidden, dumped)

    async def test_persistent_metadata_redacts_token_like_key_variants(self) -> None:
        metadata = _sanitize_persistent_metadata(
            {
                'accessToken': 'access-secret',
                'refresh_token': 'refresh-secret',
                'idToken': 'id-secret',
                'session-token': 'session-secret',
                'api_key': 'api-secret',
                'clientSecret': 'client-secret',
                'password': 'password-secret',
                'safe_path': '/tmp/trace.json',
                'nested': {'authorization_code': 'auth-code-secret', 'safe': 'public'},
            }
        )
        dumped = json.dumps(metadata, ensure_ascii=False)

        self.assertIn('/tmp/trace.json', dumped)
        self.assertIn('public', dumped)
        for forbidden in (
            'access-secret',
            'refresh-secret',
            'id-secret',
            'session-secret',
            'api-secret',
            'client-secret',
            'password-secret',
            'auth-code-secret',
        ):
            self.assertNotIn(forbidden, dumped)

    async def test_restarted_store_ignores_stale_active_sessions_without_runtime_task(self) -> None:
        repository = InMemorySessionRepository()
        first = SessionStore(repository=repository, max_active_sessions=1)
        created = await first.create_session(
            task='Первая задача',
            start_url='https://example.com/first',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )
        await first.set_status(created.session_id, SessionStatus.RUNNING)

        restarted = SessionStore(repository=repository, max_active_sessions=1)
        next_state = await restarted.create_session(
            task='Вторая задача после рестарта',
            start_url='https://example.com/second',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )

        self.assertNotEqual(created.session_id, next_state.session_id)

    @unittest.skipUnless(os.environ.get('BUYER_TEST_DATABASE_URL'), 'BUYER_TEST_DATABASE_URL не задан')
    async def test_postgres_repository_restores_state_between_store_instances(self) -> None:
        database_url = os.environ['BUYER_TEST_DATABASE_URL']
        first_repository = PostgresSessionRepository(database_url=database_url, min_pool_size=1, max_pool_size=1)
        first = SessionStore(repository=first_repository, max_active_sessions=1)
        await first.initialize()

        created = await first.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={'test': 'postgres'},
            auth=TaskAuthPayload(storageState={'cookies': [{'name': 'sid', 'value': 'secret'}], 'origins': []}),
        )
        await first.set_status(created.session_id, SessionStatus.RUNNING)
        await first.append_event(created.session_id, _event(created.session_id, 'session_started'))
        await first.add_agent_memory(created.session_id, 'user', 'Купить книгу')
        await first.set_waiting_question(created.session_id, 'Какую книгу искать?', 'reply-postgres-1')
        await first.aclose()

        second_repository = PostgresSessionRepository(database_url=database_url, min_pool_size=1, max_pool_size=1)
        second = SessionStore(repository=second_repository, max_active_sessions=1)
        await second.initialize()
        try:
            restored = await second.get(created.session_id)

            self.assertEqual(restored.status, SessionStatus.WAITING_USER)
            self.assertEqual(restored.waiting_reply_id, 'reply-postgres-1')
            self.assertIsNone(restored.auth)
            self.assertEqual([event.event_type for event in restored.events], ['session_started'])
            self.assertEqual(await second.get_agent_memory(created.session_id), [{'role': 'user', 'text': 'Купить книгу'}])
        finally:
            await second_repository.delete_sessions([created.session_id])
            await second.aclose()
