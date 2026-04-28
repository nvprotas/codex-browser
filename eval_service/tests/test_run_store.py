from __future__ import annotations

import json
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
    assert case.callback_events == [event]
    stored = json.loads(
        (tmp_path / 'eval-20260428-120000' / 'manifest.json').read_text(encoding='utf-8')
    )
    assert stored['cases'][0]['callback_events'][0]['event_type'] == 'ask_user'


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
