from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from buyer.app.auth_scripts import AUTH_FAILED_INVALID_SESSION, AUTH_REFRESH_REQUESTED, AuthScriptResult
from buyer.app.models import EventEnvelope, TaskAuthPayload
from buyer.app.service import BuyerService
from buyer.app.state import SessionStore


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
        suffix = idempotency_suffix or str(seq)
        return EventEnvelope(
            event_id=f'event-{seq}',
            session_id=session_id,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc),
            idempotency_key=f'{session_id}:{event_type}:{suffix}',
            payload=payload,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
        )

    async def deliver(self, callback_url: str, envelope: EventEnvelope, *, headers: dict[str, str] | None = None) -> None:
        _ = callback_url
        _ = headers
        self.delivered.append(envelope)


class _UnusedRunner:
    async def run_step(self, **_: Any) -> None:
        raise AssertionError('generic runner не должен запускаться в auth unit test')


class _RefreshRequestedAuthScriptRunner:
    def __init__(self, reason_code: str = AUTH_REFRESH_REQUESTED) -> None:
        self.reason_code = reason_code
        self.calls = 0

    def registry_snapshot(self) -> list[dict[str, str]]:
        return [{'domain': 'litres.ru', 'lifecycle': 'publish', 'script': 'sberid/litres.ts'}]

    async def run(self, **_: Any) -> AuthScriptResult:
        self.calls += 1
        return AuthScriptResult(
            status='failed',
            reason_code=self.reason_code,
            message='auth script requested fallback',
            artifacts={'context_prepared_for_reuse': False},
        )


def _service(
    *,
    store: SessionStore,
    auth_script_runner: _RefreshRequestedAuthScriptRunner | None = None,
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
        auth_script_runner=auth_script_runner or _RefreshRequestedAuthScriptRunner(),  # type: ignore[arg-type]
    )


class AuthReplyRemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_inline_auth_does_not_ask_user_for_storage_state(self) -> None:
        store = SessionStore()
        service = _service(store=store)
        state = await store.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=TaskAuthPayload(storageState={'cookies': []}),
        )

        async def fail_on_ask_user(*_: Any, **__: Any) -> str:
            raise AssertionError('auth-flow не должен запрашивать storageState через ask_user')

        service._ask_user_for_reply = fail_on_ask_user  # type: ignore[method-assign]

        summary = await service._run_sberid_auth_flow(state)

        self.assertEqual(summary['reason_code'], 'auth_inline_invalid_payload')
        self.assertEqual(summary['mode'], 'guest')
        self.assertEqual(summary['path'], 'guest')
        self.assertFalse(summary.get('handoff'))

    async def test_auth_refresh_script_failure_falls_back_without_user_auth_reply(self) -> None:
        store = SessionStore()
        auth_runner = _RefreshRequestedAuthScriptRunner(reason_code=AUTH_FAILED_INVALID_SESSION)
        service = _service(store=store, auth_script_runner=auth_runner)
        state = await store.create_session(
            task='Купить книгу',
            start_url='https://www.litres.ru/',
            callback_url='http://callback',
            novnc_url='http://novnc',
            metadata={},
            auth=TaskAuthPayload(storageState={'cookies': [], 'origins': []}),
        )

        async def fail_on_ask_user(*_: Any, **__: Any) -> str:
            raise AssertionError('auth-flow не должен просить новый auth-пакет через reply')

        service._ask_user_for_reply = fail_on_ask_user  # type: ignore[method-assign]

        summary = await service._run_sberid_auth_flow(state)

        self.assertEqual(summary['reason_code'], AUTH_FAILED_INVALID_SESSION)
        self.assertEqual(summary['mode'], 'sberid')
        self.assertEqual(summary['path'], 'heuristic')
        self.assertEqual(auth_runner.calls, 1)
        memory_dump = str(await store.get_agent_memory(state.session_id))
        self.assertIn('[SBERID_AUTH_HEURISTIC_REQUIRED]', memory_dump)

    def test_service_no_longer_parses_auth_from_user_reply(self) -> None:
        self.assertFalse(hasattr(BuyerService, '_parse_auth_from_user_reply'))
