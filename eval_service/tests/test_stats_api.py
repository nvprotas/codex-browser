from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from eval_service.app.main import create_app
from eval_service.app.models import CaseRunState, EvalRunCase, EvalRunStatus
from eval_service.app.run_store import RunStore
from eval_service.app.settings import Settings


FIXTURE_TRACE_ROOT = Path(__file__).parent / 'fixtures' / 'trace_session'


def test_stats_sessions_summarizes_trace_sessions_and_eval_metadata(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        eval_runs_dir=tmp_path,
        eval_cases_dir=tmp_path / 'cases',
        buyer_trace_dir=FIXTURE_TRACE_ROOT,
        buyer_api_base_url='http://buyer.test',
    )
    app = create_app(settings)
    store = RunStore(tmp_path, clock=lambda: datetime(2026, 4, 28, 12, 0, tzinfo=UTC))
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(
                eval_case_id='case-a',
                case_version='1',
                state=CaseRunState.PAYMENT_READY,
                session_id='session-judge-123',
                started_at=datetime(2026, 4, 28, 10, 20, 30, tzinfo=UTC),
                finished_at=datetime(2026, 4, 28, 10, 20, 40, tzinfo=UTC),
                artifact_paths={'trace_dir': 'traces/session-judge-123'},
            )
        ],
        status=EvalRunStatus.FINISHED,
    )
    app.state.run_store = store
    client = TestClient(app)

    response = client.get('/stats/sessions')

    assert response.status_code == 200
    payload = response.json()
    assert payload['warnings'] == []
    assert len(payload['sessions']) == 1

    session = payload['sessions'][0]
    assert session['session_id'] == 'session-judge-123'
    assert session['source'] == 'eval'
    assert session['eval_run_id'] == 'eval-run-001'
    assert session['eval_case_id'] == 'case-a'
    assert session['status'] == 'payment_ready'
    assert session['host'] == 'shop.example'
    assert session['step_count'] == 2
    assert session['duration_ms'] == 5221
    assert session['tokens_total'] == 432
    assert session['cdp_count'] == 4
    assert session['errors'] == 1
    assert session['screenshot_count'] == 1
    assert session['steps'][0]['command_breakdown']['goto']['count'] == 1
    assert session['steps'][0]['command_breakdown']['fill']['errors'] == 1
    assert session['trace_dir'].endswith('session-judge-123')
