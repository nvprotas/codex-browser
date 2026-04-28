from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from eval_service.app.main import create_app
from eval_service.app.models import CaseRunState, EvalRunCase, EvalRunStatus
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


def _client_with_store(tmp_path: Path) -> tuple[TestClient, RunStore, FakeBuyerClient]:
    settings = Settings(_env_file=None, eval_runs_dir=tmp_path, buyer_api_base_url='http://buyer.test')
    app = create_app(settings)
    store = RunStore(tmp_path, clock=lambda: datetime(2026, 4, 28, 12, 1, tzinfo=UTC))
    buyer = FakeBuyerClient()
    app.state.run_store = store
    app.state.buyer_client = buyer
    return TestClient(app), store, buyer


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
