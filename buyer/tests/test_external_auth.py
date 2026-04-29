from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

import httpx

from buyer.app.auth_scripts import AUTH_OK, AuthScriptResult
from buyer.app.external_auth import (
    ExternalSberCookiesClient,
    ExternalSberCookiesResult,
    cookies_payload_to_storage_state,
)
from buyer.app.models import EventEnvelope, TaskAuthPayload
from buyer.app.service import BuyerService
from buyer.app.state import SessionStore


def _valid_payload() -> dict[str, Any]:
    return {
        'cookies': [
            {
                'name': 'id_user2',
                'value': 'secret-cookie-value',
                'domain': 'id.sber.ru',
                'path': '/',
                'expires': -1,
                'httpOnly': True,
                'secure': True,
                'sameSite': 'Lax',
            }
        ],
        'updatedAt': '2026-04-29T10:00:00Z',
        'count': 1,
    }


class _RecordingCallbackClient:
    def __init__(self) -> None:
        self.delivered: list[EventEnvelope] = []

    def build_envelope(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_suffix: str | None = None,
        *,
        eval_run_id: str | None = None,
        eval_case_id: str | None = None,
    ) -> EventEnvelope:
        seq = len(self.delivered) + 1
        return EventEnvelope(
            event_id=f'event-{seq}',
            session_id=session_id,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc),
            idempotency_key=f'{session_id}:{event_type}:{idempotency_suffix or seq}',
            payload=payload,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
        )

    async def deliver(self, callback_url: str, envelope: EventEnvelope, *, headers: dict[str, str] | None = None) -> None:
        _ = callback_url, headers
        self.delivered.append(envelope)


class _UnusedRunner:
    async def run_step(self, **_: Any) -> None:
        raise AssertionError('generic runner не должен запускаться в auth unit test')


class _RecordingAuthScriptRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.storage_states: list[dict[str, Any]] = []

    def registry_snapshot(self) -> list[dict[str, str]]:
        return [{'domain': 'litres.ru', 'lifecycle': 'publish', 'script': 'sberid/litres.ts'}]

    async def run(self, **kwargs: Any) -> AuthScriptResult:
        self.calls += 1
        self.storage_states.append(kwargs['storage_state'])
        return AuthScriptResult(
            status='completed',
            reason_code=AUTH_OK,
            message='auth ok',
            artifacts={'context_prepared_for_reuse': True},
        )


class _FakeExternalAuthClient:
    def __init__(self, result: ExternalSberCookiesResult) -> None:
        self.result = result
        self.calls = 0

    async def fetch_storage_state(self) -> ExternalSberCookiesResult:
        self.calls += 1
        return self.result


def _service(
    *,
    store: SessionStore,
    auth_script_runner: _RecordingAuthScriptRunner | None = None,
    external_auth_client: _FakeExternalAuthClient | None = None,
) -> BuyerService:
    return BuyerService(
        store=store,
        callback_client=_RecordingCallbackClient(),  # type: ignore[arg-type]
        runner=_UnusedRunner(),  # type: ignore[arg-type]
        novnc_url='http://novnc',
        default_callback_url='http://callback',
        cdp_recovery_window_sec=0,
        cdp_recovery_interval_ms=1,
        sberid_allowlist={'litres.ru'},
        sberid_auth_retry_budget=0,
        auth_script_runner=auth_script_runner or _RecordingAuthScriptRunner(),  # type: ignore[arg-type]
        external_auth_client=external_auth_client,  # type: ignore[arg-type]
    )


class ExternalAuthPayloadTests(unittest.TestCase):
    def test_cookies_payload_to_storage_state_accepts_valid_payload(self) -> None:
        result = cookies_payload_to_storage_state(_valid_payload())

        self.assertEqual(result.reason_code, 'auth_external_loaded')
        self.assertEqual(result.storage_state, {'cookies': _valid_payload()['cookies'], 'origins': []})
        self.assertEqual(
            result.metadata,
            {
                'cookie_count': 1,
                'domains': ['id.sber.ru'],
                'updated_at': '2026-04-29T10:00:00Z',
            },
        )
        self.assertNotIn('secret-cookie-value', repr(result.metadata))

    def test_cookies_payload_to_storage_state_rejects_empty_payload(self) -> None:
        result = cookies_payload_to_storage_state({'cookies': [], 'updatedAt': '2026-04-29T10:00:00Z'})

        self.assertEqual(result.reason_code, 'auth_external_empty_payload')
        self.assertIsNone(result.storage_state)

    def test_cookies_payload_to_storage_state_rejects_invalid_cookie_shape(self) -> None:
        result = cookies_payload_to_storage_state({'cookies': [{'name': 'id_user2', 'value': 'secret'}]})

        self.assertEqual(result.reason_code, 'auth_external_invalid_payload')
        self.assertIsNone(result.storage_state)
        self.assertNotIn('secret', repr(result.metadata))
        self.assertNotIn('secret', result.message or '')


class ExternalAuthClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_returns_loaded_result_with_mock_transport(self) -> None:
        requested: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            return httpx.Response(200, json=_valid_payload())

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = ExternalSberCookiesClient(
            base_url='https://auth.example/',
            timeout_sec=1,
            retries=0,
            http_client=http_client,
        )
        try:
            result = await client.fetch_storage_state()
        finally:
            await client.aclose()

        self.assertEqual(requested, ['https://auth.example/api/v1/cookies'])
        self.assertEqual(result.reason_code, 'auth_external_loaded')
        self.assertEqual(result.metadata['cookie_count'], 1)

    async def test_client_maps_timeout(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException('timed out', request=request)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = ExternalSberCookiesClient(
            base_url='https://auth.example',
            timeout_sec=1,
            retries=1,
            http_client=http_client,
        )
        try:
            result = await client.fetch_storage_state()
        finally:
            await client.aclose()

        self.assertEqual(result.reason_code, 'auth_external_timeout')
        self.assertIsNone(result.storage_state)
        self.assertEqual(result.metadata['attempts'], 2)

    async def test_client_retries_empty_payload_before_guest_fallback(self) -> None:
        responses = [
            {'cookies': []},
            _valid_payload(),
        ]

        async def handler(request: httpx.Request) -> httpx.Response:
            _ = request
            return httpx.Response(200, json=responses.pop(0))

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = ExternalSberCookiesClient(
            base_url='https://auth.example',
            timeout_sec=1,
            retries=1,
            http_client=http_client,
        )
        try:
            result = await client.fetch_storage_state()
        finally:
            await client.aclose()

        self.assertEqual(result.reason_code, 'auth_external_loaded')
        self.assertIsNotNone(result.storage_state)
        self.assertEqual(result.metadata['attempts'], 2)


class ExternalAuthServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_uses_external_auth_when_inline_missing(self) -> None:
        store = SessionStore()
        auth_runner = _RecordingAuthScriptRunner()
        external_result = cookies_payload_to_storage_state(_valid_payload())
        external_client = _FakeExternalAuthClient(external_result)
        service = _service(store=store, auth_script_runner=auth_runner, external_auth_client=external_client)
        state = await store.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )

        summary = await service._run_sberid_auth_flow(state)
        refreshed = await store.get(state.session_id)

        self.assertEqual(external_client.calls, 1)
        self.assertIsNotNone(refreshed.auth)
        self.assertEqual(refreshed.auth.storage_state, external_result.storage_state)
        self.assertEqual(auth_runner.storage_states, [external_result.storage_state])
        self.assertEqual(summary['source'], 'external_cookies_api')
        self.assertEqual(summary['reason_code'], AUTH_OK)
        self.assertEqual(summary['external_auth']['cookie_count'], 1)
        self.assertNotIn('secret-cookie-value', repr(summary['external_auth']))

    async def test_service_skips_external_client_when_inline_auth_present(self) -> None:
        store = SessionStore()
        auth_runner = _RecordingAuthScriptRunner()
        external_client = _FakeExternalAuthClient(cookies_payload_to_storage_state(_valid_payload()))
        service = _service(store=store, auth_script_runner=auth_runner, external_auth_client=external_client)
        inline_storage_state = {'cookies': [], 'origins': []}
        state = await store.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=TaskAuthPayload(storageState=inline_storage_state),
        )

        summary = await service._run_sberid_auth_flow(state)

        self.assertEqual(external_client.calls, 0)
        self.assertEqual(auth_runner.storage_states, [inline_storage_state])
        self.assertEqual(summary['source'], 'inline')
        self.assertEqual(summary['reason_code'], AUTH_OK)

    async def test_external_failure_continues_guest_with_reason_code(self) -> None:
        store = SessionStore()
        auth_runner = _RecordingAuthScriptRunner()
        external_client = _FakeExternalAuthClient(
            ExternalSberCookiesResult(
                reason_code='auth_external_timeout',
                storage_state=None,
                metadata={'attempts': 2},
                message='timed out',
            )
        )
        service = _service(store=store, auth_script_runner=auth_runner, external_auth_client=external_client)
        state = await store.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=None,
        )

        summary = await service._run_sberid_auth_flow(state)

        self.assertEqual(external_client.calls, 1)
        self.assertEqual(auth_runner.calls, 0)
        self.assertEqual(summary['source'], 'external_cookies_api')
        self.assertEqual(summary['reason_code'], 'auth_external_timeout')
        self.assertEqual(summary['mode'], 'guest')
        self.assertEqual(summary['path'], 'guest')
        self.assertEqual(summary['external_auth']['attempts'], 2)
