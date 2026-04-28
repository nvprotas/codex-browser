from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from eval_service.app.auth_profiles import AuthProfileLoadResult, AuthProfileSkipReason
from eval_service.app.main import create_app
from eval_service.app.models import (
    BuyerCallbackEnvelope,
    CallbackEventType,
    CaseRunState,
    EvalCase,
    EvalRunStatus,
    ExpectedOutcome,
)
from eval_service.app.run_store import RunStore
from eval_service.app.settings import Settings


class FakeCaseRegistry:
    def __init__(self, cases: list[EvalCase]) -> None:
        self._cases = cases

    def load_cases(self) -> list[EvalCase]:
        return list(self._cases)


class FakeAuthProfileLoader:
    def __init__(self, results: dict[str | None, AuthProfileLoadResult]) -> None:
        self.results = results
        self.loaded: list[str | None] = []

    def load(self, auth_profile: str | None) -> AuthProfileLoadResult:
        self.loaded.append(auth_profile)
        return self.results.get(auth_profile, AuthProfileLoadResult())


class FakeBuyerClient:
    def __init__(self, store: RunStore, on_create: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.store = store
        self.on_create = on_create
        self.calls: list[dict[str, Any]] = []

    async def create_task(
        self,
        *,
        task: str,
        start_url: str,
        metadata: dict[str, Any] | None = None,
        callback_url: str | None = None,
        storage_state: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        call = {
            'task': task,
            'start_url': start_url,
            'metadata': metadata or {},
            'callback_url': callback_url,
            'storage_state': storage_state,
        }
        session_number = len(self.calls) + 1
        call['session_id'] = f'session-{session_number}'
        self.calls.append(call)
        if self.on_create is not None:
            self.on_create(call)
        return {'session_id': call['session_id'], 'status': 'running', 'novnc_url': 'http://novnc.test'}


class FakeTimer:
    def __init__(self, store: RunStore | None = None) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self.store = store

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_post_runs_selected_cases_creates_manifest_and_calls_buyer_sequentially(tmp_path: Path) -> None:
    def finish_case_and_assert_sequence(call: dict[str, Any]) -> None:
        if call['metadata']['eval_case_id'] == 'case-b':
            first_case = store.read_manifest('eval-run-001').cases[0]
            assert first_case.state == CaseRunState.FINISHED
        _append_payment_ready(store, call)

    client, store, buyer, _timer = _client_with_orchestrator(
        tmp_path,
        cases=[
            _case('case-a', auth_profile='auth-a', metadata={'budget': 500}),
            _case('case-b', auth_profile='auth-b', metadata={'budget': 900}),
            _case('case-c', auth_profile='auth-c'),
        ],
        auth_results={
            'auth-a': AuthProfileLoadResult(storage_state={'cookies': [{'name': 'a', 'value': '1'}], 'origins': []}),
            'auth-b': AuthProfileLoadResult(storage_state={'cookies': [{'name': 'b', 'value': '2'}], 'origins': []}),
        },
        on_create=finish_case_and_assert_sequence,
    )

    response = client.post('/runs', json={'case_ids': ['case-a', 'case-b']})

    assert response.status_code == 200
    body = response.json()
    assert body['eval_run_id'] == 'eval-run-001'
    assert body['status'] == 'finished'
    manifest = store.read_manifest('eval-run-001')
    assert manifest.status == EvalRunStatus.FINISHED
    assert [case.eval_case_id for case in manifest.cases] == ['case-a', 'case-b']
    assert [case.state for case in manifest.cases] == [CaseRunState.FINISHED, CaseRunState.FINISHED]
    assert [call['metadata']['eval_case_id'] for call in buyer.calls] == ['case-a', 'case-b']
    assert buyer.calls[1]['metadata']['eval_case_id'] == 'case-b'
    assert store.read_manifest('eval-run-001').cases[0].state == CaseRunState.FINISHED
    assert buyer.calls[0]['metadata'] == {
        'budget': 500,
        'eval_run_id': 'eval-run-001',
        'eval_case_id': 'case-a',
        'case_version': '1',
        'host': 'example.test',
        'case_title': 'Case case-a',
        'variant_id': 'variant-case-a',
    }
    assert buyer.calls[0]['task'] == 'Задача для case-a'
    assert buyer.calls[0]['start_url'] == 'https://example.test/case-a'
    assert buyer.calls[0]['callback_url'] == 'http://testserver/callbacks/buyer'
    assert buyer.calls[0]['storage_state'] == {'cookies': [{'name': 'a', 'value': '1'}], 'origins': []}


def test_missing_auth_profile_skips_case_and_continues_with_later_case(tmp_path: Path) -> None:
    client, store, buyer, _timer = _client_with_orchestrator(
        tmp_path,
        cases=[
            _case('case-missing-auth', auth_profile='missing-auth'),
            _case('case-later', auth_profile=None),
        ],
        auth_results={
            'missing-auth': AuthProfileLoadResult(
                skip_reason=AuthProfileSkipReason(
                    reason='auth_profile_missing',
                    auth_profile='missing-auth',
                    message='Auth-профиль не найден.',
                )
            )
        },
        on_create=lambda call: _append_payment_ready(store, call),
    )

    response = client.post('/runs')

    assert response.status_code == 200
    manifest = store.read_manifest('eval-run-001')
    assert manifest.status == EvalRunStatus.FINISHED
    assert [call['metadata']['eval_case_id'] for call in buyer.calls] == ['case-later']
    skipped_case, later_case = manifest.cases
    assert skipped_case.state == CaseRunState.SKIPPED_AUTH_MISSING
    assert skipped_case.finished_at is not None
    assert json.loads(skipped_case.error or '{}') == {
        'state': 'skipped_auth_missing',
        'reason': 'auth_profile_missing',
        'auth_profile': 'missing-auth',
        'message': 'Auth-профиль не найден.',
    }
    assert later_case.state == CaseRunState.FINISHED


def test_timeout_marks_case_timeout_and_preserves_session_id(tmp_path: Path) -> None:
    client, store, buyer, timer = _client_with_orchestrator(
        tmp_path,
        cases=[_case('case-timeout')],
        timeout_seconds=1.0,
        poll_interval_seconds=0.25,
    )

    response = client.post('/runs', json={})

    assert response.status_code == 200
    assert len(buyer.calls) == 1
    case = store.read_manifest('eval-run-001').cases[0]
    assert case.state == CaseRunState.TIMEOUT
    assert case.session_id == 'session-1'
    assert case.error == 'timeout after 1.0s'
    assert case.callback_events == []
    assert timer.sleeps == [0.25, 0.25, 0.25, 0.25]


def test_payment_ready_waits_grace_period_before_finishing_case(tmp_path: Path) -> None:
    timer = FakeTimer()

    async def sleep(seconds: float) -> None:
        if seconds == 5.0:
            case = store.read_manifest('eval-run-001').cases[0]
            assert case.state == CaseRunState.PAYMENT_READY
            assert case.finished_at is None
        await timer.sleep(seconds)

    client, store, _buyer, _timer = _client_with_orchestrator(
        tmp_path,
        cases=[_case('case-payment')],
        on_create=lambda call: _append_payment_ready(store, call),
        sleep=sleep,
        timer=timer,
    )

    response = client.post('/runs', json={})

    assert response.status_code == 200
    case = store.read_manifest('eval-run-001').cases[0]
    assert case.state == CaseRunState.FINISHED
    assert case.finished_at is not None
    assert timer.sleeps == [5.0]


def test_waiting_user_state_is_preserved_and_run_stays_running(tmp_path: Path) -> None:
    client, store, buyer, _timer = _client_with_orchestrator(
        tmp_path,
        cases=[
            _case('case-needs-user'),
            _case('case-after-waiting'),
        ],
        on_create=lambda call: _append_ask_user(store, call),
    )

    response = client.post('/runs', json={})

    assert response.status_code == 200
    manifest = store.read_manifest('eval-run-001')
    assert manifest.status == EvalRunStatus.RUNNING
    assert [call['metadata']['eval_case_id'] for call in buyer.calls] == ['case-needs-user']
    waiting_case, pending_case = manifest.cases
    assert waiting_case.state == CaseRunState.WAITING_USER
    assert waiting_case.waiting_reply_id == 'reply-session-1'
    assert waiting_case.finished_at is None
    assert pending_case.state == CaseRunState.PENDING


def _client_with_orchestrator(
    tmp_path: Path,
    *,
    cases: list[EvalCase],
    auth_results: dict[str | None, AuthProfileLoadResult] | None = None,
    on_create: Callable[[dict[str, Any]], None] | None = None,
    timeout_seconds: float = 600.0,
    poll_interval_seconds: float = 0.1,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    timer: FakeTimer | None = None,
) -> tuple[TestClient, RunStore, FakeBuyerClient, FakeTimer]:
    settings = Settings(_env_file=None, eval_runs_dir=tmp_path, buyer_api_base_url='http://buyer.test')
    app = create_app(settings)
    store = RunStore(tmp_path, clock=lambda: datetime(2026, 4, 28, 12, 0, tzinfo=UTC))
    fake_timer = timer or FakeTimer(store)
    buyer = FakeBuyerClient(store, on_create=on_create)
    app.state.run_store = store
    app.state.case_registry = FakeCaseRegistry(cases)
    app.state.auth_profile_loader = FakeAuthProfileLoader(auth_results or {})
    app.state.buyer_client = buyer
    app.state.eval_run_id_generator = lambda: 'eval-run-001'
    app.state.orchestrator_monotonic = fake_timer.monotonic
    app.state.orchestrator_sleep = sleep or fake_timer.sleep
    app.state.orchestrator_timeout_seconds = timeout_seconds
    app.state.orchestrator_poll_interval_seconds = poll_interval_seconds
    return TestClient(app), store, buyer, fake_timer


def _case(
    eval_case_id: str,
    *,
    auth_profile: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EvalCase:
    return EvalCase(
        eval_case_id=eval_case_id,
        case_version='1',
        variant_id=f'variant-{eval_case_id}',
        title=f'Case {eval_case_id}',
        host='example.test',
        task=f'Задача для {eval_case_id}',
        start_url=f'https://example.test/{eval_case_id}',
        metadata=metadata or {},
        auth_profile=auth_profile,
        expected_outcome=ExpectedOutcome(target='target', stop_condition='SberPay открыт'),
    )


def _append_payment_ready(store: RunStore, call: dict[str, Any]) -> None:
    metadata = call['metadata']
    envelope = BuyerCallbackEnvelope(
        event_id=f'event-payment-{call["session_id"]}',
        session_id=call['session_id'],
        event_type=CallbackEventType.PAYMENT_READY,
        occurred_at=datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC),
        idempotency_key=f'idem-payment-{call["session_id"]}',
        payload={'order_id': f'order-{call["session_id"]}'},
        eval_run_id=metadata['eval_run_id'],
        eval_case_id=metadata['eval_case_id'],
    )
    store.append_callback_event(
        metadata['eval_run_id'],
        metadata['eval_case_id'],
        envelope,
        state=CaseRunState.PAYMENT_READY,
        waiting_reply_id=None,
    )


def _append_ask_user(store: RunStore, call: dict[str, Any]) -> None:
    metadata = call['metadata']
    reply_id = f'reply-{call["session_id"]}'
    envelope = BuyerCallbackEnvelope(
        event_id=f'event-ask-{call["session_id"]}',
        session_id=call['session_id'],
        event_type=CallbackEventType.ASK_USER,
        occurred_at=datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC),
        idempotency_key=f'idem-ask-{call["session_id"]}',
        payload={'reply_id': reply_id, 'question': 'Продолжить?'},
        eval_run_id=metadata['eval_run_id'],
        eval_case_id=metadata['eval_case_id'],
    )
    store.append_callback_event(
        metadata['eval_run_id'],
        metadata['eval_case_id'],
        envelope,
        state=CaseRunState.WAITING_USER,
        waiting_reply_id=reply_id,
    )
