from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable

from fastapi.testclient import TestClient

from eval_service.app.main import create_app
from eval_service.app.models import (
    BuyerCallbackEnvelope,
    CallbackEventType,
    CaseRunState,
    EvalRunCase,
    EvalRunStatus,
)
from eval_service.app.run_store import RunStore
from eval_service.app.settings import Settings


class FakeBuyerClient:
    def __init__(self) -> None:
        self.replies: list[dict[str, str]] = []

    async def send_reply(self, *, session_id: str, reply_id: str, message: str) -> dict[str, Any]:
        self.replies.append(
            {
                'session_id': session_id,
                'reply_id': reply_id,
                'message': message,
            }
        )
        return {'session_id': session_id, 'accepted': True, 'status': 'running'}


def _client_with_store(
    tmp_path: Path,
    *,
    raise_server_exceptions: bool = True,
    eval_callback_base_url: str | None = None,
    client_base_url: str = 'http://testserver',
) -> tuple[TestClient, RunStore, FakeBuyerClient]:
    settings = Settings(
        _env_file=None,
        eval_runs_dir=tmp_path,
        buyer_api_base_url='http://buyer.test',
        eval_callback_base_url=eval_callback_base_url,
    )
    app = create_app(settings)
    store = RunStore(tmp_path, clock=lambda: datetime(2026, 4, 28, 12, 1, tzinfo=UTC))
    buyer = FakeBuyerClient()
    app.state.run_store = store
    app.state.buyer_client = buyer
    return TestClient(app, raise_server_exceptions=raise_server_exceptions, base_url=client_base_url), store, buyer


def _create_run(store: RunStore) -> None:
    store.create_run(
        'eval-20260428-120000',
        cases=[EvalRunCase(eval_case_id='litres_book_odyssey_001', case_version='1')],
        status=EvalRunStatus.RUNNING,
    )


def _callback_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        'event_id': f'event-{event_type}-1',
        'session_id': 'session-123',
        'event_type': event_type,
        'occurred_at': '2026-04-28T12:00:45Z',
        'idempotency_key': f'idem-{event_type}-1',
        'payload': payload,
        'eval_run_id': 'eval-20260428-120000',
        'eval_case_id': 'litres_book_odyssey_001',
    }


def _callback_payload_without_eval_ids(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    callback = _callback_payload(event_type, payload)
    callback.pop('eval_run_id')
    callback.pop('eval_case_id')
    return callback


def test_buyer_callback_ask_user_persists_event_and_waiting_state(tmp_path: Path) -> None:
    client, store, _buyer = _client_with_store(tmp_path)
    _create_run(store)

    response = client.post(
        '/callbacks/buyer',
        json=_callback_payload(
            'ask_user',
            {'reply_id': 'reply-42', 'question': 'Подтвердить размер?', 'choices': ['Да', 'Нет']},
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        'eval_run_id': 'eval-20260428-120000',
        'eval_case_id': 'litres_book_odyssey_001',
        'state': 'waiting_user',
    }
    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert case.state == CaseRunState.WAITING_USER
    assert case.session_id == 'session-123'
    assert case.waiting_reply_id == 'reply-42'
    assert case.callback_events[0].payload['question'] == 'Подтвердить размер?'


def test_buyer_callback_without_eval_ids_resolves_case_by_session_id_for_ask_user(tmp_path: Path) -> None:
    client, store, _buyer = _client_with_store(tmp_path)
    _create_run(store)
    store.update_case(
        'eval-20260428-120000',
        'litres_book_odyssey_001',
        state=CaseRunState.RUNNING,
        session_id='session-123',
    )

    response = client.post(
        '/callbacks/buyer',
        json=_callback_payload_without_eval_ids(
            'ask_user',
            {'reply_id': 'reply-42', 'question': 'Подтвердить размер?'},
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        'eval_run_id': 'eval-20260428-120000',
        'eval_case_id': 'litres_book_odyssey_001',
        'state': 'waiting_user',
    }
    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert case.state == CaseRunState.WAITING_USER
    assert case.waiting_reply_id == 'reply-42'
    assert case.callback_events[-1].eval_run_id == 'eval-20260428-120000'
    assert case.callback_events[-1].eval_case_id == 'litres_book_odyssey_001'


def test_buyer_callback_without_eval_ids_resolves_case_by_session_id_for_payment_ready(tmp_path: Path) -> None:
    client, store, _buyer = _client_with_store(tmp_path)
    _create_run(store)
    store.update_case(
        'eval-20260428-120000',
        'litres_book_odyssey_001',
        state=CaseRunState.RUNNING,
        session_id='session-123',
    )

    response = client.post(
        '/callbacks/buyer',
        json=_callback_payload_without_eval_ids(
            'payment_ready',
            {'payment_method': 'sberpay', 'order_id': 'order-1'},
        ),
    )

    assert response.status_code == 200
    assert response.json()['state'] == 'payment_ready'
    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert case.state == CaseRunState.PAYMENT_READY
    assert case.waiting_reply_id is None
    assert case.callback_events[-1].eval_run_id == 'eval-20260428-120000'
    assert case.callback_events[-1].eval_case_id == 'litres_book_odyssey_001'


def test_buyer_callback_accepts_real_progress_event_types_without_state_change(tmp_path: Path) -> None:
    client, store, _buyer = _client_with_store(tmp_path)
    _create_run(store)
    store.update_case(
        'eval-20260428-120000',
        'litres_book_odyssey_001',
        state=CaseRunState.RUNNING,
        session_id='session-123',
        waiting_reply_id='reply-active',
    )

    for event_type in [
        'session_started',
        'agent_step_started',
        'agent_step_finished',
        'agent_stream_event',
        'handoff_requested',
        'handoff_resumed',
    ]:
        response = client.post(
            '/callbacks/buyer',
            json=_callback_payload(event_type, {'status': event_type}),
        )

        assert response.status_code == 200
        assert response.json()['state'] == 'running'

    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert case.state == CaseRunState.RUNNING
    assert case.waiting_reply_id == 'reply-active'
    assert case.finished_at is None
    assert [event.event_type.value for event in case.callback_events] == [
        'session_started',
        'agent_step_started',
        'agent_step_finished',
        'agent_stream_event',
        'handoff_requested',
        'handoff_resumed',
    ]


def test_buyer_callback_payment_ready_and_scenario_finished_update_case_state(tmp_path: Path) -> None:
    client, store, _buyer = _client_with_store(tmp_path)
    _create_run(store)
    client.post(
        '/callbacks/buyer',
        json=_callback_payload('ask_user', {'reply_id': 'reply-42', 'question': 'Продолжить?'}),
    )

    payment_response = client.post(
        '/callbacks/buyer',
        json=_callback_payload('payment_ready', {'payment_method': 'sberpay', 'order_id': 'order-1'}),
    )
    assert payment_response.status_code == 200
    payment_case = store.read_manifest('eval-20260428-120000').cases[0]
    assert payment_case.state == CaseRunState.PAYMENT_READY
    assert payment_case.waiting_reply_id is None
    assert payment_case.callback_events[-1].payload['order_id'] == 'order-1'

    finished_response = client.post(
        '/callbacks/buyer',
        json=_callback_payload('scenario_finished', {'result': 'ok'}),
    )

    assert finished_response.status_code == 200
    finished_case = store.read_manifest('eval-20260428-120000').cases[0]
    assert finished_case.state == CaseRunState.FINISHED
    assert finished_case.finished_at == datetime(2026, 4, 28, 12, 0, 45, tzinfo=UTC)


def test_buyer_callback_appends_late_terminal_events_without_mutating_terminal_case(tmp_path: Path) -> None:
    client, store, _buyer = _client_with_store(tmp_path)
    terminal_finished_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    store.create_run(
        'eval-20260428-120000',
        cases=[
            EvalRunCase(
                eval_case_id='litres_book_odyssey_001',
                case_version='1',
                state=CaseRunState.FINISHED,
                session_id='session-123',
                finished_at=terminal_finished_at,
            ),
            EvalRunCase(
                eval_case_id='brandshop_sneakers_001',
                case_version='1',
                state=CaseRunState.TIMEOUT,
                session_id='session-456',
                finished_at=terminal_finished_at,
                error='timeout after 600s',
            ),
        ],
        status=EvalRunStatus.RUNNING,
    )
    before_cases = store.read_manifest('eval-20260428-120000').cases

    ask_response = client.post(
        '/callbacks/buyer',
        json=_callback_payload('ask_user', {'reply_id': 'reply-late', 'question': 'Поздний вопрос'}),
    )
    payment_payload = {
        **_callback_payload('payment_ready', {'payment_method': 'sberpay', 'order_id': 'order-late'}),
        'event_id': 'event-payment_ready-late',
        'session_id': 'session-456',
        'idempotency_key': 'idem-payment_ready-late',
        'eval_case_id': 'brandshop_sneakers_001',
    }
    payment_response = client.post('/callbacks/buyer', json=payment_payload)

    assert ask_response.status_code == 200
    assert ask_response.json()['state'] == 'finished'
    assert payment_response.status_code == 200
    assert payment_response.json()['state'] == 'timeout'
    after_cases = store.read_manifest('eval-20260428-120000').cases
    assert after_cases[0].state == CaseRunState.FINISHED
    assert after_cases[0].waiting_reply_id == before_cases[0].waiting_reply_id
    assert after_cases[0].finished_at == terminal_finished_at
    assert after_cases[0].callback_events[-1].event_type.value == 'ask_user'
    assert after_cases[1].state == CaseRunState.TIMEOUT
    assert after_cases[1].waiting_reply_id == before_cases[1].waiting_reply_id
    assert after_cases[1].finished_at == terminal_finished_at
    assert after_cases[1].error == 'timeout after 600s'
    assert after_cases[1].callback_events[-1].event_type.value == 'payment_ready'


def test_operator_reply_uses_saved_reply_id_and_moves_case_back_to_running(tmp_path: Path) -> None:
    client, store, buyer = _client_with_store(tmp_path)
    _create_run(store)
    client.post(
        '/callbacks/buyer',
        json=_callback_payload('ask_user', {'reply_id': 'reply-42', 'question': 'Продолжить?'}),
    )

    response = client.post(
        '/runs/eval-20260428-120000/cases/litres_book_odyssey_001/reply',
        json={'message': 'Да, продолжай.'},
    )

    assert response.status_code == 200
    assert response.json() == {
        'eval_run_id': 'eval-20260428-120000',
        'eval_case_id': 'litres_book_odyssey_001',
        'session_id': 'session-123',
        'reply_id': 'reply-42',
        'accepted': True,
        'buyer_status': 'running',
        'state': 'running',
    }
    assert buyer.replies == [
        {
            'session_id': 'session-123',
            'reply_id': 'reply-42',
            'message': 'Да, продолжай.',
        }
    ]
    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert case.state == CaseRunState.RUNNING
    assert case.waiting_reply_id is None


def test_operator_reply_rejects_stale_explicit_reply_while_waiting_without_calling_buyer(
    tmp_path: Path,
) -> None:
    client, store, buyer = _client_with_store(tmp_path)
    _create_run(store)
    client.post(
        '/callbacks/buyer',
        json=_callback_payload('ask_user', {'reply_id': 'reply-actual', 'question': 'Продолжить?'}),
    )
    before_case = store.read_manifest('eval-20260428-120000').cases[0]
    scheduled: list[Any] = []

    async def reject_resume(coro: Any) -> None:
        scheduled.append(coro)
        coro.close()

    client.app.state.orchestrator_resume_scheduler = reject_resume

    response = client.post(
        '/runs/eval-20260428-120000/cases/litres_book_odyssey_001/reply',
        json={'reply_id': 'reply-stale', 'message': 'Да, продолжай.'},
    )

    assert response.status_code == 409
    assert buyer.replies == []
    assert scheduled == []
    after_case = store.read_manifest('eval-20260428-120000').cases[0]
    assert after_case.model_dump(mode='json') == before_case.model_dump(mode='json')


def test_operator_reply_rejects_stale_explicit_reply_after_terminal_state_without_calling_buyer(
    tmp_path: Path,
) -> None:
    client, store, buyer = _client_with_store(tmp_path)
    _create_run(store)
    client.post(
        '/callbacks/buyer',
        json=_callback_payload('ask_user', {'reply_id': 'reply-42', 'question': 'Продолжить?'}),
    )
    client.post(
        '/callbacks/buyer',
        json=_callback_payload('scenario_finished', {'result': 'ok'}),
    )
    before_case = store.read_manifest('eval-20260428-120000').cases[0]
    scheduled: list[Any] = []

    async def reject_resume(coro: Any) -> None:
        scheduled.append(coro)
        coro.close()

    client.app.state.orchestrator_resume_scheduler = reject_resume

    response = client.post(
        '/runs/eval-20260428-120000/cases/litres_book_odyssey_001/reply',
        json={'reply_id': 'reply-42', 'message': 'Да, продолжай.'},
    )

    assert response.status_code == 409
    assert buyer.replies == []
    assert scheduled == []
    after_case = store.read_manifest('eval-20260428-120000').cases[0]
    assert after_case.model_dump(mode='json') == before_case.model_dump(mode='json')


def test_operator_reply_schedules_resume_without_waiting_for_continuation(tmp_path: Path) -> None:
    class EmptyCaseRegistry:
        def load_cases(self) -> list[Any]:
            return []

    client, store, _buyer = _client_with_store(tmp_path)
    _create_run(store)
    client.post(
        '/callbacks/buyer',
        json=_callback_payload('ask_user', {'reply_id': 'reply-42', 'question': 'Продолжить?'}),
    )
    scheduled: list[Any] = []

    async def capture_resume(coro: Any) -> None:
        scheduled.append(coro)

    client.app.state.orchestrator_resume_scheduler = capture_resume
    client.app.state.case_registry = EmptyCaseRegistry()
    client.app.state.orchestrator_timeout_seconds = 0.0

    response = client.post(
        '/runs/eval-20260428-120000/cases/litres_book_odyssey_001/reply',
        json={'message': 'Да, продолжай.'},
    )

    assert response.status_code == 200
    assert response.json()['state'] == 'running'
    assert len(scheduled) == 1
    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert case.state == CaseRunState.RUNNING
    assert case.waiting_reply_id is None

    for coro in scheduled:
        coro.close()


def test_operator_reply_keeps_terminal_callback_state_when_buyer_finishes_during_send_reply(
    tmp_path: Path,
) -> None:
    client, store, buyer = _client_with_store(tmp_path)
    _create_run(store)
    client.post(
        '/callbacks/buyer',
        json=_callback_payload('ask_user', {'reply_id': 'reply-42', 'question': 'Продолжить?'}),
    )
    finished_at = datetime(2026, 4, 28, 12, 2, tzinfo=UTC)

    async def finish_during_send_reply(*, session_id: str, reply_id: str, message: str) -> dict[str, Any]:
        buyer.replies.append(
            {
                'session_id': session_id,
                'reply_id': reply_id,
                'message': message,
            }
        )
        store.append_callback_event(
            'eval-20260428-120000',
            'litres_book_odyssey_001',
            BuyerCallbackEnvelope(
                event_id='event-scenario_finished-race',
                session_id=session_id,
                event_type=CallbackEventType.SCENARIO_FINISHED,
                occurred_at=finished_at,
                idempotency_key='idem-scenario_finished-race',
                payload={'result': 'ok'},
                eval_run_id='eval-20260428-120000',
                eval_case_id='litres_book_odyssey_001',
            ),
            state=CaseRunState.FINISHED,
            finished_at=finished_at,
            waiting_reply_id=None,
        )
        return {'session_id': session_id, 'accepted': True, 'status': 'finished'}

    buyer.send_reply = finish_during_send_reply
    scheduled: list[Any] = []

    async def reject_resume(coro: Any) -> None:
        scheduled.append(coro)
        coro.close()

    client.app.state.orchestrator_resume_scheduler = reject_resume

    response = client.post(
        '/runs/eval-20260428-120000/cases/litres_book_odyssey_001/reply',
        json={'message': 'Да, продолжай.'},
    )

    assert response.status_code == 200
    assert response.json()['state'] == 'finished'
    assert scheduled == []
    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert case.state == CaseRunState.FINISHED
    assert case.finished_at == finished_at
    assert case.waiting_reply_id is None
    assert case.callback_events[-1].event_type == CallbackEventType.SCENARIO_FINISHED


def test_operator_reply_resume_uses_configured_callback_url_instead_of_request_host(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, store, _buyer = _client_with_store(
        tmp_path,
        eval_callback_base_url='http://eval_service:8090',
        client_base_url='http://localhost:8090',
    )
    _create_run(store)
    client.post(
        '/callbacks/buyer',
        json=_callback_payload('ask_user', {'reply_id': 'reply-42', 'question': 'Продолжить?'}),
    )
    captured: dict[str, str] = {}

    class FakeOrchestrator:
        async def resume_after_operator_reply(
            self,
            *,
            eval_run_id: str,
            eval_case_id: str,
            callback_url: str,
        ) -> None:
            captured['eval_run_id'] = eval_run_id
            captured['eval_case_id'] = eval_case_id
            captured['callback_url'] = callback_url

    async def run_resume_inline(coro: Awaitable[Any]) -> None:
        await coro

    monkeypatch.setattr('eval_service.app.callbacks.get_run_orchestrator', lambda request: FakeOrchestrator())
    client.app.state.orchestrator_resume_scheduler = run_resume_inline

    response = client.post(
        '/runs/eval-20260428-120000/cases/litres_book_odyssey_001/reply',
        json={'message': 'Да, продолжай.'},
    )

    assert response.status_code == 200
    assert captured == {
        'eval_run_id': 'eval-20260428-120000',
        'eval_case_id': 'litres_book_odyssey_001',
        'callback_url': 'http://eval_service:8090/callbacks/buyer',
    }


def test_operator_reply_inline_resume_failure_marks_run_failed(tmp_path: Path) -> None:
    class EmptyCaseRegistry:
        def load_cases(self) -> list[Any]:
            return []

    client, store, buyer = _client_with_store(tmp_path, raise_server_exceptions=False)
    store.create_run(
        'eval-20260428-120000',
        cases=[
            EvalRunCase(
                eval_case_id='litres_book_odyssey_001',
                case_version='1',
                state=CaseRunState.WAITING_USER,
                session_id='session-123',
                waiting_reply_id='reply-42',
            ),
            EvalRunCase(eval_case_id='case-missing-from-registry', case_version='1'),
        ],
        status=EvalRunStatus.RUNNING,
    )

    async def run_resume_inline(coro: Awaitable[Any]) -> None:
        await coro

    client.app.state.case_registry = EmptyCaseRegistry()
    client.app.state.orchestrator_resume_scheduler = run_resume_inline
    client.app.state.orchestrator_timeout_seconds = 0.0

    response = client.post(
        '/runs/eval-20260428-120000/cases/litres_book_odyssey_001/reply',
        json={'message': 'Да, продолжай.'},
    )

    assert response.status_code == 200
    assert buyer.replies == [
        {
            'session_id': 'session-123',
            'reply_id': 'reply-42',
            'message': 'Да, продолжай.',
        }
    ]
    manifest = store.read_manifest('eval-20260428-120000')
    assert manifest.status == EvalRunStatus.FAILED
    assert manifest.cases[0].state == CaseRunState.TIMEOUT
    assert manifest.cases[1].state == CaseRunState.PENDING
