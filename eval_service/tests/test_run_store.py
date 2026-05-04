from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eval_service.app.models import (
    BuyerCallbackEnvelope,
    CallbackEventType,
    CaseRunState,
    EvalRunCase,
    EvalRunStatus,
)
from eval_service.app.run_store import RunStore


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self.values = list(values)

    def __call__(self) -> datetime:
        return self.values.pop(0)


def _case(eval_case_id: str = 'litres_book_odyssey_001') -> EvalRunCase:
    return EvalRunCase(eval_case_id=eval_case_id, case_version='1')


def test_create_run_creates_directory_and_manifest(tmp_path: Path) -> None:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    store = RunStore(tmp_path, clock=SequenceClock(now))

    manifest = store.create_run(
        'eval-20260428-120000',
        cases=[_case(), _case('brandshop_sneakers_001')],
        status=EvalRunStatus.RUNNING,
    )

    run_dir = tmp_path / 'eval-20260428-120000'
    assert run_dir.is_dir()
    assert (run_dir / 'manifest.json').is_file()
    assert manifest.created_at == now
    assert manifest.updated_at == now
    assert manifest.status == EvalRunStatus.RUNNING
    assert [case.eval_case_id for case in manifest.cases] == [
        'litres_book_odyssey_001',
        'brandshop_sneakers_001',
    ]
    assert store.read_manifest('eval-20260428-120000') == manifest


def test_run_store_rejects_eval_run_id_path_traversal(tmp_path: Path) -> None:
    store = RunStore(tmp_path)

    with pytest.raises(ValueError, match='eval_run_id'):
        store.create_run('../evil', cases=[_case()])

    assert not (tmp_path.parent / 'evil').exists()


def test_write_manifest_round_trips_existing_manifest(tmp_path: Path) -> None:
    created_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    updated_at = datetime(2026, 4, 28, 12, 1, tzinfo=UTC)
    store = RunStore(tmp_path, clock=SequenceClock(created_at))
    manifest = store.create_run('eval-20260428-120000', cases=[_case()])

    changed = manifest.model_copy(update={'status': EvalRunStatus.FINISHED, 'updated_at': updated_at})
    store.write_manifest(changed)

    loaded = store.read_manifest('eval-20260428-120000')
    assert loaded.status == EvalRunStatus.FINISHED
    assert loaded.updated_at == updated_at


def test_update_case_tracks_state_session_timings_errors_and_artifacts(tmp_path: Path) -> None:
    created_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    first_update = datetime(2026, 4, 28, 12, 1, tzinfo=UTC)
    second_update = datetime(2026, 4, 28, 12, 2, tzinfo=UTC)
    started_at = datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC)
    finished_at = datetime(2026, 4, 28, 12, 1, 50, tzinfo=UTC)
    store = RunStore(tmp_path, clock=SequenceClock(created_at, first_update, second_update))
    store.create_run('eval-20260428-120000', cases=[_case()])

    store.update_case(
        'eval-20260428-120000',
        'litres_book_odyssey_001',
        state=CaseRunState.RUNNING,
        session_id='session-123',
        started_at=started_at,
        artifact_paths={'trace_dir': 'traces/session-123'},
    )
    manifest = store.update_case(
        'eval-20260428-120000',
        'litres_book_odyssey_001',
        state=CaseRunState.TIMEOUT,
        finished_at=finished_at,
        error='timeout after 600s',
        artifact_paths={'judge_input': 'evaluations/litres_book_odyssey_001.judge-input.json'},
    )

    case = manifest.cases[0]
    assert manifest.updated_at == second_update
    assert case.state == CaseRunState.TIMEOUT
    assert case.session_id == 'session-123'
    assert case.started_at == started_at
    assert case.finished_at == finished_at
    assert case.error == 'timeout after 600s'
    assert case.artifact_paths == {
        'trace_dir': 'traces/session-123',
        'judge_input': 'evaluations/litres_book_odyssey_001.judge-input.json',
    }


def test_append_callback_event_tracks_event_and_optional_case_fields(tmp_path: Path) -> None:
    created_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    updated_at = datetime(2026, 4, 28, 12, 1, tzinfo=UTC)
    occurred_at = datetime(2026, 4, 28, 12, 0, 45, tzinfo=UTC)
    store = RunStore(tmp_path, clock=SequenceClock(created_at, updated_at))
    store.create_run('eval-20260428-120000', cases=[_case()])
    event = BuyerCallbackEnvelope(
        event_id='event-ask-user-1',
        session_id='session-123',
        event_type=CallbackEventType.ASK_USER,
        occurred_at=occurred_at,
        idempotency_key='idem-1',
        payload={'reply_id': 'reply-42', 'question': 'Подтвердить размер?'},
        eval_run_id='eval-20260428-120000',
        eval_case_id='litres_book_odyssey_001',
    )

    manifest = store.append_callback_event(
        'eval-20260428-120000',
        'litres_book_odyssey_001',
        event,
        state=CaseRunState.WAITING_USER,
        waiting_reply_id='reply-42',
    )

    case = manifest.cases[0]
    assert manifest.updated_at == updated_at
    assert case.state == CaseRunState.WAITING_USER
    assert case.session_id == 'session-123'
    assert case.waiting_reply_id == 'reply-42'
    assert len(case.callback_events) == 1
    assert case.callback_events[0].event_id == event.event_id
    assert case.callback_events[0].idempotency_key.startswith('sha256:')
    assert case.callback_events[0].idempotency_key != event.idempotency_key
    assert case.callback_events[0].payload == {'reply_id': 'reply-42', 'question': 'Подтвердить размер?'}
    stored = json.loads(
        (tmp_path / 'eval-20260428-120000' / 'manifest.json').read_text(encoding='utf-8')
    )
    assert stored['cases'][0]['callback_events'][0]['event_type'] == 'ask_user'
    assert stored['cases'][0]['callback_events'][0]['idempotency_key'].startswith('sha256:')


def test_append_callback_event_redacts_payment_and_callback_secrets_on_disk(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create_run('eval-20260428-120000', cases=[_case()])
    event = BuyerCallbackEnvelope(
        event_id='event-payment-ready',
        session_id='session-123',
        event_type=CallbackEventType.PAYMENT_READY,
        occurred_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
        idempotency_key='sha256:ORDER-999-secret',
        payload={
            'order_id': 'ORDER-999',
            'order_id_host': 'payecom.ru',
            'message': (
                'Payment token=payment-token-secret order_id=ORDER-999 '
                'url=https://pay.example/sberpay/order/ORDER-999?token=payment-token-secret'
            ),
            'payment_url': 'https://pay.example/sberpay/order/ORDER-999?token=payment-token-secret',
            'callback_url': 'http://eval.test/callbacks/buyer?token=callback-secret',
        },
    )

    store.append_callback_event('eval-20260428-120000', 'litres_book_odyssey_001', event)

    manifest_text = store.manifest_path('eval-20260428-120000').read_text(encoding='utf-8')
    stored_event = json.loads(manifest_text)['cases'][0]['callback_events'][0]
    assert stored_event['idempotency_key'].startswith('sha256:')
    assert stored_event['payload'] == {
        'order_id_host': 'payecom.ru',
        'message': 'Payment token=[redacted] order_id=[redacted] url=[redacted-payment-url]',
        'callback_url': 'http://eval.test/callbacks/buyer?token=[redacted]',
    }
    assert 'sha256:ORDER-999-secret' not in manifest_text
    assert 'ORDER-999' not in manifest_text
    assert 'payment-token-secret' not in manifest_text
    assert 'callback-secret' not in manifest_text


def test_concurrent_case_updates_and_callback_events_do_not_lose_updates(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create_run('eval-20260428-120000', cases=[_case()])
    occurred_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)

    def append_event(index: int) -> None:
        event = BuyerCallbackEnvelope(
            event_id=f'event-{index}',
            session_id='session-123',
            event_type=CallbackEventType.AGENT_STEP_FINISHED,
            occurred_at=occurred_at,
            idempotency_key=f'idem-{index}',
            payload={'index': index},
        )
        store.append_callback_event(
            'eval-20260428-120000',
            'litres_book_odyssey_001',
            event,
            artifact_paths={f'event_{index}': f'artifacts/event-{index}.json'},
        )

    def update_case(index: int) -> None:
        store.update_case(
            'eval-20260428-120000',
            'litres_book_odyssey_001',
            session_id='session-123',
            artifact_paths={f'update_{index}': f'artifacts/update-{index}.json'},
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [
            *(executor.submit(append_event, index) for index in range(30)),
            *(executor.submit(update_case, index) for index in range(30)),
        ]
        for future in futures:
            future.result()

    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert {event.event_id for event in case.callback_events} == {f'event-{index}' for index in range(30)}
    assert case.artifact_paths == {
        **{f'event_{index}': f'artifacts/event-{index}.json' for index in range(30)},
        **{f'update_{index}': f'artifacts/update-{index}.json' for index in range(30)},
    }


def test_append_callback_event_is_idempotent_under_concurrency(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create_run('eval-20260428-120000', cases=[_case()])
    event = BuyerCallbackEnvelope(
        event_id='event-duplicate',
        session_id='session-123',
        event_type=CallbackEventType.ASK_USER,
        occurred_at=datetime(2026, 4, 28, 12, 0, tzinfo=UTC),
        idempotency_key='idem-duplicate',
        payload={},
    )

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [
            executor.submit(
                store.append_callback_event,
                'eval-20260428-120000',
                'litres_book_odyssey_001',
                event,
            )
            for _ in range(30)
        ]
        for future in futures:
            future.result()

    case = store.read_manifest('eval-20260428-120000').cases[0]
    assert len(case.callback_events) == 1
    assert case.callback_events[0].event_id == event.event_id
    assert case.callback_events[0].idempotency_key.startswith('sha256:')


def test_write_summary_persists_supplied_aggregate_and_updates_manifest(tmp_path: Path) -> None:
    created_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    updated_at = datetime(2026, 4, 28, 12, 1, tzinfo=UTC)
    store = RunStore(tmp_path, clock=SequenceClock(created_at, updated_at))
    store.create_run('eval-20260428-120000', cases=[_case()])
    summary = {
        'totals': {'cases': 1, 'judged': 1, 'failed': 0},
        'checks': {'outcome_ok': {'ok': 1, 'not_ok': 0, 'skipped': 0}},
    }

    summary_path = store.write_summary('eval-20260428-120000', summary)

    assert summary_path == tmp_path / 'eval-20260428-120000' / 'summary.json'
    assert json.loads(summary_path.read_text(encoding='utf-8')) == summary
    manifest = store.read_manifest('eval-20260428-120000')
    assert manifest.summary_path == 'summary.json'
    assert manifest.updated_at == updated_at


def test_atomic_json_write_keeps_previous_summary_when_new_data_is_invalid(tmp_path: Path) -> None:
    created_at = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    first_update = datetime(2026, 4, 28, 12, 1, tzinfo=UTC)
    store = RunStore(tmp_path, clock=SequenceClock(created_at, first_update))
    store.create_run('eval-20260428-120000', cases=[_case()])
    summary_path = store.write_summary('eval-20260428-120000', {'status': 'original'})
    original_text = summary_path.read_text(encoding='utf-8')

    with pytest.raises(TypeError):
        store.write_summary('eval-20260428-120000', {'bad': object()})

    assert summary_path.read_text(encoding='utf-8') == original_text
