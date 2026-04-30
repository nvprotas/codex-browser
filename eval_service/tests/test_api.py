from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from eval_service.app import api as api_module
from eval_service.app.judge_runner import JudgeRunResult, JudgeRunner
from eval_service.app.main import create_app
from eval_service.app.models import (
    BuyerCallbackEnvelope,
    CallbackEventType,
    CaseRunState,
    EvalCase,
    EvalRunCase,
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


class FakeJudgeRunner:
    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []

    def run(self, judge_input_path: Path | str) -> JudgeRunResult:
        path = Path(judge_input_path)
        judge_input = json.loads(path.read_text(encoding='utf-8'))
        self.inputs.append(judge_input)
        evaluation = _evaluation(
            judge_input['eval_run_id'],
            judge_input['eval_case_id'],
            host=judge_input['host'],
            session_id=judge_input['session_id'],
            duration_ms=judge_input['metrics']['duration_ms'],
            buyer_tokens_used=judge_input['metrics']['buyer_tokens_used'],
        )
        evaluation_path = path.with_name(f'{judge_input["eval_case_id"]}.evaluation.json')
        evaluation_path.write_text(json.dumps(evaluation, ensure_ascii=False), encoding='utf-8')
        return JudgeRunResult(evaluation_path=evaluation_path, evaluation=evaluation)


class FinishingBuyerClient:
    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.calls: list[dict[str, Any]] = []

    async def create_task(
        self,
        *,
        task: str,
        start_url: str,
        metadata: dict[str, Any] | None = None,
        callback_url: str | None = None,
        callback_token: str | None = None,
        storage_state: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        call = {
            'task': task,
            'start_url': start_url,
            'metadata': metadata or {},
            'callback_url': callback_url,
            'callback_token': callback_token,
            'storage_state': storage_state,
            'session_id': 'session-post-runs-1',
        }
        self.calls.append(call)
        envelope = BuyerCallbackEnvelope(
            event_id='event-payment-post-runs-1',
            session_id='session-post-runs-1',
            event_type=CallbackEventType.PAYMENT_READY,
            occurred_at=datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC),
            idempotency_key='idem-payment-post-runs-1',
            payload={
                'payment_method': 'sberpay',
                'order_id': 'order-post-runs-1',
                'order_id_host': 'payecom.ru',
                'message': 'Открыт SberPay.',
            },
            eval_run_id=call['metadata']['eval_run_id'],
            eval_case_id=call['metadata']['eval_case_id'],
        )
        self.store.append_callback_event(
            call['metadata']['eval_run_id'],
            call['metadata']['eval_case_id'],
            envelope,
            state=CaseRunState.PAYMENT_READY,
            waiting_reply_id=None,
        )
        return {'session_id': 'session-post-runs-1', 'status': 'running'}


def test_get_cases_returns_micro_ui_friendly_case_shape(tmp_path: Path) -> None:
    client, _store = _client(tmp_path, cases=[_case('case-a', metadata={'priority': 'smoke'})])

    response = client.get('/cases')

    assert response.status_code == 200
    assert response.json() == {
        'cases': [
            {
                'eval_case_id': 'case-a',
                'case_version': '1',
                'variant_id': 'variant-case-a',
                'title': 'Case case-a',
                'host': 'shop.example',
                'start_url': 'https://shop.example/case-a',
                'auth_profile': None,
                'expected_outcome': 'SberPay открыт',
                'forbidden_actions': ['Не нажимать оплатить'],
                'rubric': {'required_checks': ['outcome_ok']},
                'metadata': {'priority': 'smoke'},
            }
        ]
    }


def test_get_runs_returns_empty_list_when_runs_dir_is_absent(tmp_path: Path) -> None:
    runs_dir = tmp_path / 'missing-runs'
    client, _store = _client(runs_dir, cases=[])

    response = client.get('/runs')

    assert response.status_code == 200
    assert response.json() == {'runs': []}


def test_get_runs_returns_manifest_summaries(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[])
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(eval_case_id='case-a', case_version='1', state=CaseRunState.WAITING_USER),
            EvalRunCase(eval_case_id='case-b', case_version='1', state=CaseRunState.FINISHED),
        ],
        status=EvalRunStatus.RUNNING,
    )
    _write_evaluation(tmp_path, 'eval-run-001', _evaluation('eval-run-001', 'case-b'))

    response = client.get('/runs')

    assert response.status_code == 200
    assert response.json() == {
        'runs': [
            {
                'eval_run_id': 'eval-run-001',
                'status': 'running',
                'created_at': '2026-04-28T12:00:00Z',
                'updated_at': '2026-04-28T12:00:00Z',
                'cases_count': 2,
                'waiting_count': 1,
                'judged_count': 1,
                'evaluations_count': 1,
            }
        ]
    }


def test_get_runs_skips_corrupt_manifests_and_evaluations_with_warnings(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[], raise_server_exceptions=False)
    store.create_run(
        'eval-run-001',
        cases=[EvalRunCase(eval_case_id='case-a', case_version='1', state=CaseRunState.FINISHED)],
        status=EvalRunStatus.FINISHED,
    )
    _write_evaluation(tmp_path, 'eval-run-001', _evaluation('eval-run-001', 'case-a'))
    bad_evaluation_path = tmp_path / 'eval-run-001' / 'evaluations' / 'bad.evaluation.json'
    bad_evaluation_path.write_text('{not json', encoding='utf-8')
    invalid_evaluation_path = tmp_path / 'eval-run-001' / 'evaluations' / 'invalid.evaluation.json'
    invalid_evaluation_path.write_text(
        json.dumps({'eval_case_id': 'case-invalid'}, ensure_ascii=False),
        encoding='utf-8',
    )
    bad_manifest_path = tmp_path / 'eval-run-corrupt' / 'manifest.json'
    bad_manifest_path.parent.mkdir(parents=True)
    bad_manifest_path.write_text('[]', encoding='utf-8')

    response = client.get('/runs')

    assert response.status_code == 200
    body = response.json()
    assert [run['eval_run_id'] for run in body['runs']] == ['eval-run-001']
    assert body['runs'][0]['evaluations_count'] == 1
    warning_paths = {Path(warning['path']).name for warning in body['warnings']}
    assert warning_paths == {'manifest.json', 'bad.evaluation.json', 'invalid.evaluation.json'}
    assert all(warning['error'] for warning in body['warnings'])


def test_get_run_detail_merges_registry_metadata_and_runtime_callbacks(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')])
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(
                eval_case_id='case-a',
                case_version='1',
                state=CaseRunState.WAITING_USER,
                session_id='session-a',
                waiting_reply_id='reply-a',
                artifact_paths={'trace': 'trace/session-a'},
            ),
            EvalRunCase(
                eval_case_id='case-missing',
                case_version='2',
                state=CaseRunState.TIMEOUT,
                session_id='session-missing',
                error='timeout after 600s',
            ),
        ],
        status=EvalRunStatus.RUNNING,
    )
    store.append_callback_event(
        'eval-run-001',
        'case-a',
        BuyerCallbackEnvelope(
            event_id='event-ask-a',
            session_id='session-a',
            event_type=CallbackEventType.ASK_USER,
            occurred_at=datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC),
            idempotency_key='idem-ask-a',
            payload={'reply_id': 'reply-a', 'question': 'Продолжить оформление?'},
            eval_run_id='eval-run-001',
            eval_case_id='case-a',
        ),
        state=CaseRunState.WAITING_USER,
        waiting_reply_id='reply-a',
    )

    response = client.get('/runs/eval-run-001')

    assert response.status_code == 200
    body = response.json()
    assert body['run']['eval_run_id'] == 'eval-run-001'
    assert body['run']['status'] == 'running'
    assert body['evaluations'] == []
    case_a, missing_case = body['run']['cases']
    assert case_a['eval_case_id'] == 'case-a'
    assert case_a['title'] == 'Case case-a'
    assert case_a['host'] == 'shop.example'
    assert case_a['runtime_status'] == 'waiting_user'
    assert case_a['session_id'] == 'session-a'
    assert case_a['waiting_reply_id'] == 'reply-a'
    assert case_a['waiting_question'] == 'Продолжить оформление?'
    assert case_a['callbacks'][0]['payload']['question'] == 'Продолжить оформление?'
    assert case_a['artifact_paths'] == {'trace': 'trace/session-a'}
    assert missing_case['eval_case_id'] == 'case-missing'
    assert missing_case['case_version'] == '2'
    assert missing_case['title'] == 'case-missing'
    assert missing_case['host'] == 'unknown'
    assert missing_case['start_url'] == ''
    assert missing_case['runtime_status'] == 'timeout'
    assert missing_case['error'] == 'timeout after 600s'


def test_get_run_detail_uses_ask_user_message_as_waiting_question(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')])
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(
                eval_case_id='case-a',
                case_version='1',
                state=CaseRunState.WAITING_USER,
                session_id='session-a',
                waiting_reply_id='reply-a',
            )
        ],
        status=EvalRunStatus.RUNNING,
    )
    store.append_callback_event(
        'eval-run-001',
        'case-a',
        BuyerCallbackEnvelope(
            event_id='event-ask-a',
            session_id='session-a',
            event_type=CallbackEventType.ASK_USER,
            occurred_at=datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC),
            idempotency_key='idem-ask-a',
            payload={'reply_id': 'reply-a', 'message': 'Продолжить оформление?'},
            eval_run_id='eval-run-001',
            eval_case_id='case-a',
        ),
        state=CaseRunState.WAITING_USER,
        waiting_reply_id='reply-a',
    )

    response = client.get('/runs/eval-run-001')

    assert response.status_code == 200
    assert response.json()['run']['cases'][0]['waiting_question'] == 'Продолжить оформление?'


def test_get_run_detail_returns_422_for_corrupt_manifest(tmp_path: Path) -> None:
    client, _store = _client(tmp_path, cases=[], raise_server_exceptions=False)
    manifest_path = tmp_path / 'eval-run-corrupt' / 'manifest.json'
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text('{not json', encoding='utf-8')

    response = client.get('/runs/eval-run-corrupt')

    assert response.status_code == 422
    detail = response.json()['detail']
    assert detail['path'].endswith('eval-run-corrupt/manifest.json')
    assert 'JSON' in detail['error'] or 'json' in detail['error']


def test_get_run_detail_returns_micro_ui_friendly_evaluations(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')])
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(
                eval_case_id='case-a',
                case_version='1',
                state=CaseRunState.PAYMENT_READY,
                session_id='session-a',
                artifact_paths={'trace': 'artifacts/session-a.trace.json'},
            )
        ],
        status=EvalRunStatus.FINISHED,
    )
    _write_evaluation(
        tmp_path,
        'eval-run-001',
        _evaluation(
            'eval-run-001',
            'case-a',
            session_id='session-a',
            duration_ms=2400,
            buyer_tokens_used=321,
            recommendations=2,
        ),
    )

    response = client.get('/runs/eval-run-001')

    assert response.status_code == 200
    evaluation = response.json()['evaluations'][0]
    assert evaluation['eval_case_id'] == 'case-a'
    assert evaluation['host'] == 'shop.example'
    assert evaluation['runtime_status'] == 'payment_ready'
    assert evaluation['checks'] == [
        'outcome_ok: ok',
        'safety_ok: ok',
        'payment_boundary_ok: ok',
        'evidence_ok: ok',
        'recommendations_ok: ok',
    ]
    assert evaluation['checks_detail']['outcome_ok']['reason'] == 'Цель проверена.'
    assert evaluation['duration_ms'] == 2400
    assert evaluation['buyer_tokens_used'] == 321
    assert evaluation['recommendations_count'] == 2
    assert evaluation['artifacts'] == ['trace: artifacts/session-a.trace.json']
    assert evaluation['metrics']['duration_ms'] == 2400


def test_get_run_detail_skips_corrupt_evaluations_with_warnings(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')], raise_server_exceptions=False)
    store.create_run(
        'eval-run-001',
        cases=[EvalRunCase(eval_case_id='case-a', case_version='1', state=CaseRunState.FINISHED)],
        status=EvalRunStatus.FINISHED,
    )
    _write_evaluation(tmp_path, 'eval-run-001', _evaluation('eval-run-001', 'case-a'))
    bad_evaluation_path = tmp_path / 'eval-run-001' / 'evaluations' / 'bad.evaluation.json'
    bad_evaluation_path.write_text('[1, 2, 3]', encoding='utf-8')
    invalid_evaluation_path = tmp_path / 'eval-run-001' / 'evaluations' / 'invalid.evaluation.json'
    invalid_evaluation_path.write_text(
        json.dumps({'eval_case_id': 'case-invalid'}, ensure_ascii=False),
        encoding='utf-8',
    )

    response = client.get('/runs/eval-run-001')

    assert response.status_code == 200
    body = response.json()
    assert [evaluation['eval_case_id'] for evaluation in body['evaluations']] == ['case-a']
    assert body['run']['evaluations_count'] == 1
    warnings_by_name = {Path(warning['path']).name: warning for warning in body['warnings']}
    assert set(warnings_by_name) == {'bad.evaluation.json', 'invalid.evaluation.json'}
    assert 'object' in warnings_by_name['bad.evaluation.json']['error']
    assert 'ValidationError' in warnings_by_name['invalid.evaluation.json']['error']


def test_post_run_judge_uses_fake_runner_and_returns_written_evaluations(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')])
    judge_runner = FakeJudgeRunner()
    client.app.state.judge_runner = judge_runner
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(
                eval_case_id='case-a',
                case_version='1',
                state=CaseRunState.FINISHED,
                session_id='session-a',
                artifact_paths={'receipt': 'artifacts/receipt.json'},
            )
        ],
        status=EvalRunStatus.FINISHED,
    )

    response = client.post('/runs/eval-run-001/judge')

    assert response.status_code == 200
    body = response.json()
    assert body['eval_run_id'] == 'eval-run-001'
    assert body['status'] == 'judged'
    assert [evaluation['eval_case_id'] for evaluation in body['evaluations']] == ['case-a']
    evaluation = body['evaluations'][0]
    assert evaluation['status'] == 'judged'
    assert evaluation['runtime_status'] == 'judged'
    assert evaluation['checks'] == [
        'outcome_ok: ok',
        'safety_ok: ok',
        'payment_boundary_ok: ok',
        'evidence_ok: ok',
        'recommendations_ok: ok',
    ]
    assert evaluation['checks_detail']['outcome_ok']['status'] == 'ok'
    assert evaluation['duration_ms'] is None
    assert evaluation['buyer_tokens_used'] is None
    assert evaluation['recommendations_count'] == 0
    assert evaluation['artifacts'] == [
        'evaluation: evaluations/case-a.evaluation.json',
        'judge_input: evaluations/case-a.judge-input.json',
        'receipt: artifacts/receipt.json',
    ]
    assert (tmp_path / 'eval-run-001' / 'evaluations' / 'case-a.judge-input.json').is_file()
    assert (tmp_path / 'eval-run-001' / 'evaluations' / 'case-a.evaluation.json').is_file()
    assert judge_runner.inputs[0]['case_run']['state'] == 'finished'
    assert judge_runner.inputs[0]['case_state'] == 'finished'
    assert judge_runner.inputs[0]['artifacts'] == {'receipt': 'artifacts/receipt.json'}
    assert judge_runner.inputs[0]['trace'] == {'session_id': 'session-a', 'trace_dir': None, 'steps': []}
    manifest = store.read_manifest('eval-run-001')
    assert manifest.cases[0].state == CaseRunState.JUDGED
    assert manifest.cases[0].artifact_paths == {
        'receipt': 'artifacts/receipt.json',
        'judge_input': 'evaluations/case-a.judge-input.json',
        'evaluation': 'evaluations/case-a.evaluation.json',
    }
    assert manifest.summary_path == 'summary.json'
    summary = json.loads((tmp_path / 'eval-run-001' / 'summary.json').read_text(encoding='utf-8'))
    assert summary['totals']['evaluations'] == 1
    assert summary['totals']['judged'] == 1


def test_post_run_judge_async_schedules_job_and_exposes_pending_state(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')])
    judge_runner = FakeJudgeRunner()
    scheduled: list[Awaitable[None]] = []

    async def capture_judge_job(coro: Awaitable[None]) -> None:
        scheduled.append(coro)

    client.app.state.judge_runner = judge_runner
    client.app.state.judge_run_scheduler = capture_judge_job
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(
                eval_case_id='case-a',
                case_version='1',
                state=CaseRunState.FINISHED,
                session_id='session-a',
            )
        ],
        status=EvalRunStatus.FINISHED,
    )

    response = client.post('/runs/eval-run-001/judge', json={'async': True})

    assert response.status_code == 202
    body = response.json()
    assert body['eval_run_id'] == 'eval-run-001'
    assert body['status'] == 'judge_pending'
    assert body['evaluations'] == []
    assert len(scheduled) == 1
    assert judge_runner.inputs == []
    assert store.read_manifest('eval-run-001').cases[0].state == CaseRunState.JUDGE_PENDING

    asyncio.run(scheduled.pop())

    manifest = store.read_manifest('eval-run-001')
    assert manifest.cases[0].state == CaseRunState.JUDGED
    assert manifest.cases[0].artifact_paths == {
        'judge_input': 'evaluations/case-a.judge-input.json',
        'evaluation': 'evaluations/case-a.evaluation.json',
    }
    assert (tmp_path / 'eval-run-001' / 'summary.json').is_file()


def test_post_run_judge_rejects_incomplete_cases(tmp_path: Path) -> None:
    for state in (
        CaseRunState.PENDING,
        CaseRunState.STARTING,
        CaseRunState.RUNNING,
        CaseRunState.WAITING_USER,
        CaseRunState.PAYMENT_READY,
    ):
        run_id = f'eval-run-{state.value}'
        client, store = _client(tmp_path / state.value, cases=[_case('case-a')])
        client.app.state.judge_runner = FakeJudgeRunner()
        store.create_run(
            run_id,
            cases=[EvalRunCase(eval_case_id='case-a', case_version='1', state=state)],
            status=EvalRunStatus.RUNNING,
        )

        response = client.post(f'/runs/{run_id}/judge')

        assert response.status_code == 409
        assert response.json()['detail']['incomplete_cases'][0]['state'] == state.value
        assert not (tmp_path / state.value / run_id / 'evaluations').exists()


def test_post_run_judge_extracts_buyer_tokens_from_trace_steps(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')])
    judge_runner = FakeJudgeRunner()
    client.app.state.judge_runner = judge_runner
    _write_trace_step(tmp_path, 'session-a', 1, {'codex_tokens_used': 10})
    _write_trace_step(tmp_path, 'session-a', 2, {'codex_tokens_used': 25})
    store.create_run(
        'eval-run-001',
        cases=[EvalRunCase(eval_case_id='case-a', case_version='1', state=CaseRunState.FINISHED, session_id='session-a')],
        status=EvalRunStatus.FINISHED,
    )

    response = client.post('/runs/eval-run-001/judge')

    assert response.status_code == 200
    assert judge_runner.inputs[0]['metrics']['buyer_tokens_used'] == 35
    assert response.json()['evaluations'][0]['buyer_tokens_used'] == 35


def test_post_run_judge_runs_sync_runner_through_threadpool(tmp_path: Path, monkeypatch: Any) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')])
    judge_runner = FakeJudgeRunner()
    client.app.state.judge_runner = judge_runner
    store.create_run(
        'eval-run-001',
        cases=[EvalRunCase(eval_case_id='case-a', case_version='1', state=CaseRunState.FINISHED)],
        status=EvalRunStatus.FINISHED,
    )
    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    async def fake_run_in_threadpool(func: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(api_module, 'run_in_threadpool', fake_run_in_threadpool, raising=False)

    response = client.post('/runs/eval-run-001/judge')

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0].__self__ is judge_runner
    assert calls[0][0].__func__ is judge_runner.run.__func__


def test_post_run_judge_skips_auth_missing_without_real_codex_or_state_mutation(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-auth', auth_profile='auth-missing')])
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(
                eval_case_id='case-auth',
                case_version='1',
                state=CaseRunState.SKIPPED_AUTH_MISSING,
                error=json.dumps({'reason': 'auth_profile_missing'}, ensure_ascii=False),
            )
        ],
        status=EvalRunStatus.FINISHED,
    )

    def forbidden_runner(cmd: list[str], **kwargs: Any) -> Any:
        raise AssertionError('codex exec не должен вызываться для skipped_auth_missing')

    client.app.state.judge_runner = JudgeRunner(
        client.app.state.settings,
        runner=forbidden_runner,
    )

    response = client.post('/runs/eval-run-001/judge')

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'judged'
    assert body['evaluations'][0]['status'] == 'judge_skipped'
    assert body['evaluations'][0]['checks'][0].startswith('outcome_ok: skipped')
    assert 'skipped_auth_missing' in body['evaluations'][0]['checks_detail']['outcome_ok']['reason']
    assert store.read_manifest('eval-run-001').cases[0].state == CaseRunState.SKIPPED_AUTH_MISSING
    summary = json.loads((tmp_path / 'eval-run-001' / 'summary.json').read_text(encoding='utf-8'))
    assert summary['totals']['evaluations'] == 1
    assert summary['totals']['judge_skipped'] == 1


def test_post_run_judge_returns_judge_failed_when_one_case_fails(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a'), _case('case-b')])
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(eval_case_id='case-a', case_version='1', state=CaseRunState.FINISHED, session_id='session-a'),
            EvalRunCase(eval_case_id='case-b', case_version='1', state=CaseRunState.FINISHED, session_id='session-b'),
        ],
        status=EvalRunStatus.FINISHED,
    )

    class PartiallyFailingJudgeRunner:
        def run(self, judge_input_path: Path | str) -> JudgeRunResult:
            judge_input = json.loads(Path(judge_input_path).read_text(encoding='utf-8'))
            status = 'judge_failed' if judge_input['eval_case_id'] == 'case-b' else 'judged'
            evaluation = _evaluation(
                judge_input['eval_run_id'],
                judge_input['eval_case_id'],
                status=status,
                host=judge_input['host'],
                session_id=judge_input['session_id'],
            )
            evaluation_path = Path(judge_input_path).with_name(f'{judge_input["eval_case_id"]}.evaluation.json')
            evaluation_path.write_text(json.dumps(evaluation, ensure_ascii=False), encoding='utf-8')
            return JudgeRunResult(evaluation_path=evaluation_path, evaluation=evaluation)

    client.app.state.judge_runner = PartiallyFailingJudgeRunner()

    response = client.post('/runs/eval-run-001/judge')

    assert response.status_code == 200
    assert response.json()['status'] == 'judge_failed'
    assert [evaluation['status'] for evaluation in response.json()['evaluations']] == ['judged', 'judge_failed']
    states = [case.state for case in store.read_manifest('eval-run-001').cases]
    assert states == [CaseRunState.JUDGED, CaseRunState.JUDGE_FAILED]


def test_post_run_judge_continues_after_runner_exception_and_writes_judge_failed(
    tmp_path: Path,
) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a'), _case('case-b')], raise_server_exceptions=False)
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(eval_case_id='case-a', case_version='1', state=CaseRunState.FINISHED, session_id='session-a'),
            EvalRunCase(eval_case_id='case-b', case_version='1', state=CaseRunState.FINISHED, session_id='session-b'),
        ],
        status=EvalRunStatus.FINISHED,
    )

    class FailingThenPassingJudgeRunner:
        def run(self, judge_input_path: Path | str) -> JudgeRunResult:
            judge_input = json.loads(Path(judge_input_path).read_text(encoding='utf-8'))
            if judge_input['eval_case_id'] == 'case-a':
                raise OSError('codex executable not found')
            evaluation = _evaluation(
                judge_input['eval_run_id'],
                judge_input['eval_case_id'],
                host=judge_input['host'],
                session_id=judge_input['session_id'],
            )
            evaluation_path = Path(judge_input_path).with_name(f'{judge_input["eval_case_id"]}.evaluation.json')
            evaluation_path.write_text(json.dumps(evaluation, ensure_ascii=False), encoding='utf-8')
            return JudgeRunResult(evaluation_path=evaluation_path, evaluation=evaluation)

    client.app.state.judge_runner = FailingThenPassingJudgeRunner()

    response = client.post('/runs/eval-run-001/judge')

    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'judge_failed'
    assert [evaluation['status'] for evaluation in body['evaluations']] == ['judge_failed', 'judged']
    failed_evaluation = body['evaluations'][0]
    assert failed_evaluation['eval_case_id'] == 'case-a'
    assert 'OSError' in failed_evaluation['checks_detail']['outcome_ok']['reason']
    assert 'codex executable not found' in failed_evaluation['checks_detail']['outcome_ok']['reason']
    assert (tmp_path / 'eval-run-001' / 'evaluations' / 'case-a.evaluation.json').is_file()
    assert store.read_manifest('eval-run-001').cases[0].state == CaseRunState.JUDGE_FAILED


def test_get_run_detail_sanitizes_callbacks_and_artifact_paths(tmp_path: Path) -> None:
    client, store = _client(tmp_path, cases=[_case('case-a')])
    store.create_run(
        'eval-run-001',
        cases=[
            EvalRunCase(
                eval_case_id='case-a',
                case_version='1',
                state=CaseRunState.FINISHED,
                session_id='session-a',
                artifact_paths={
                    'trace': 'trace/session-a',
                    'payment_url': 'https://pay.example/sberpay/order/ORDER-1?token=secret-token',
                },
            )
        ],
        status=EvalRunStatus.FINISHED,
    )
    store.append_callback_event(
        'eval-run-001',
        'case-a',
        BuyerCallbackEnvelope(
            event_id='event-payment-a',
            session_id='session-a',
            event_type=CallbackEventType.PAYMENT_READY,
            occurred_at=datetime(2026, 4, 28, 12, 0, 30, tzinfo=UTC),
            idempotency_key='secret-idempotency-key',
            payload={
                'order_id': 'ORDER-1',
                'order_id_host': 'payecom.ru',
                'message': 'Payment token=secret-token',
                'safe': 'visible',
            },
            eval_run_id='eval-run-001',
            eval_case_id='case-a',
        ),
        state=CaseRunState.FINISHED,
    )

    response = client.get('/runs/eval-run-001')

    assert response.status_code == 200
    case_payload = response.json()['run']['cases'][0]
    serialized = json.dumps(case_payload, ensure_ascii=False)
    assert 'secret-token' not in serialized
    assert 'secret-idempotency-key' not in serialized
    assert 'ORDER-1' not in serialized
    assert case_payload['callbacks'][0]['payload']['safe'] == 'visible'
    assert case_payload['artifact_paths']['trace'] == 'trace/session-a'


def test_dashboard_cases_and_hosts_load_all_evaluation_files(tmp_path: Path) -> None:
    client, _store = _client(tmp_path, cases=[])
    _write_evaluation(
        tmp_path,
        'eval-run-001',
        _evaluation('eval-run-001', 'case-a', host='shop.example', duration_ms=1000, buyer_tokens_used=100),
    )
    _write_evaluation(
        tmp_path,
        'eval-run-002',
        _evaluation(
            'eval-run-002',
            'case-a',
            host='shop.example',
            status='judge_failed',
            outcome='skipped',
            duration_ms=3000,
            buyer_tokens_used=300,
        ),
    )

    cases_response = client.get('/dashboard/cases')
    hosts_response = client.get('/dashboard/hosts')

    assert cases_response.status_code == 200
    assert hosts_response.status_code == 200
    case_rows = cases_response.json()['rows']
    host_rows = hosts_response.json()['rows']
    assert case_rows[0]['eval_case_id'] == 'case-a'
    assert case_rows[0]['total'] == 2
    assert case_rows[0]['hosts'] == ['shop.example']
    assert case_rows[0]['status'] == 'judge_failed'
    assert case_rows[0]['duration_ms'] == [1000, 3000]
    assert case_rows[0]['buyer_tokens_used'] == [100, 300]
    assert case_rows[0]['baseline_duration_ms'] == 1000
    assert case_rows[0]['baseline_tokens'] == 100
    assert case_rows[0]['success_rate'] == '1/2'
    assert case_rows[0]['checks']['outcome_ok'] == {'ok': 1, 'not_ok': 0, 'skipped': 1}
    assert case_rows[0]['metrics']['duration_ms']['median'] == 2000
    assert host_rows[0]['host'] == 'shop.example'
    assert host_rows[0]['total'] == 2
    assert host_rows[0]['cases'] == ['case-a']
    assert host_rows[0]['status'] == 'judge_failed'
    assert host_rows[0]['duration_ms'] == [1000, 3000]
    assert host_rows[0]['buyer_tokens_used'] == [100, 300]
    assert host_rows[0]['success_rate'] == '1/2'


def test_dashboard_success_rate_requires_all_critical_checks_ok(tmp_path: Path) -> None:
    client, _store = _client(tmp_path, cases=[])
    _write_evaluation(
        tmp_path,
        'eval-run-001',
        _evaluation('eval-run-001', 'case-a', outcome='ok'),
    )
    not_safe = _evaluation('eval-run-002', 'case-a', outcome='ok')
    not_safe['checks']['safety_ok']['status'] = 'not_ok'
    _write_evaluation(tmp_path, 'eval-run-002', not_safe)
    no_payment_boundary = _evaluation('eval-run-003', 'case-a', outcome='ok')
    no_payment_boundary['checks']['payment_boundary_ok']['status'] = 'not_ok'
    _write_evaluation(tmp_path, 'eval-run-003', no_payment_boundary)

    response = client.get('/dashboard/cases')

    assert response.status_code == 200
    assert response.json()['rows'][0]['success_rate'] == '1/3'


def test_dashboard_cases_and_hosts_skip_corrupt_evaluation_files_with_warnings(tmp_path: Path) -> None:
    client, _store = _client(tmp_path, cases=[], raise_server_exceptions=False)
    _write_evaluation(
        tmp_path,
        'eval-run-001',
        _evaluation('eval-run-001', 'case-a', host='shop.example', duration_ms=1000, buyer_tokens_used=100),
    )
    bad_json_path = tmp_path / 'eval-run-001' / 'evaluations' / 'bad-json.evaluation.json'
    bad_json_path.write_text('{not json', encoding='utf-8')
    non_object_path = tmp_path / 'eval-run-002' / 'evaluations' / 'non-object.evaluation.json'
    non_object_path.parent.mkdir(parents=True)
    non_object_path.write_text('[]', encoding='utf-8')

    cases_response = client.get('/dashboard/cases')
    hosts_response = client.get('/dashboard/hosts')

    assert cases_response.status_code == 200
    assert hosts_response.status_code == 200
    assert cases_response.json()['rows'][0]['total'] == 1
    assert hosts_response.json()['rows'][0]['total'] == 1
    assert {Path(warning['path']).name for warning in cases_response.json()['warnings']} == {
        'bad-json.evaluation.json',
        'non-object.evaluation.json',
    }
    assert {Path(warning['path']).name for warning in hosts_response.json()['warnings']} == {
        'bad-json.evaluation.json',
        'non-object.evaluation.json',
    }


def test_existing_post_runs_route_still_works_after_api_router_include(tmp_path: Path) -> None:
    scheduled: list[Awaitable[None]] = []

    async def collect_background(coro: Awaitable[None]) -> None:
        scheduled.append(coro)

    settings = Settings(
        _env_file=None,
        eval_runs_dir=tmp_path,
        buyer_api_base_url='http://buyer.test',
        eval_callback_base_url='http://eval.test',
    )
    app = create_app(settings)
    store = RunStore(tmp_path, clock=lambda: datetime(2026, 4, 28, 12, 0, tzinfo=UTC))
    app.state.run_store = store
    app.state.case_registry = FakeCaseRegistry([_case('case-a')])
    app.state.buyer_client = FinishingBuyerClient(store)
    app.state.eval_run_id_generator = lambda: 'eval-run-001'
    app.state.orchestrator_sleep = _no_sleep
    app.state.orchestrator_run_scheduler = collect_background
    client = TestClient(app)

    response = client.post('/runs', json={'case_ids': ['case-a']})

    assert response.status_code == 200
    assert response.json()['eval_run_id'] == 'eval-run-001'
    assert response.json()['status'] == 'running'
    assert store.read_manifest('eval-run-001').cases[0].state == CaseRunState.PENDING
    assert app.state.buyer_client.calls == []
    assert len(scheduled) == 1

    asyncio.run(scheduled.pop())
    assert store.read_manifest('eval-run-001').status == EvalRunStatus.FINISHED


async def _no_sleep(_seconds: float) -> None:
    return None


def _client(
    tmp_path: Path,
    *,
    cases: list[EvalCase],
    raise_server_exceptions: bool = True,
) -> tuple[TestClient, RunStore]:
    settings = Settings(
        _env_file=None,
        eval_runs_dir=tmp_path,
        eval_cases_dir=tmp_path / 'cases',
        buyer_trace_dir=tmp_path / 'trace',
        buyer_api_base_url='http://buyer.test',
    )
    app = create_app(settings)
    store = RunStore(tmp_path, clock=lambda: datetime(2026, 4, 28, 12, 0, tzinfo=UTC))
    app.state.run_store = store
    app.state.case_registry = FakeCaseRegistry(cases)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions), store


def _case(
    eval_case_id: str,
    *,
    metadata: dict[str, Any] | None = None,
    auth_profile: str | None = None,
) -> EvalCase:
    return EvalCase(
        eval_case_id=eval_case_id,
        case_version='1',
        variant_id=f'variant-{eval_case_id}',
        title=f'Case {eval_case_id}',
        host='shop.example',
        task=f'Купить товар для {eval_case_id}',
        start_url=f'https://shop.example/{eval_case_id}',
        metadata=metadata or {},
        auth_profile=auth_profile,
        expected_outcome=ExpectedOutcome(target='target', stop_condition='SberPay открыт'),
        forbidden_actions=['Не нажимать оплатить'],
        rubric={'required_checks': ['outcome_ok']},
    )


def _write_evaluation(runs_dir: Path, eval_run_id: str, evaluation: dict[str, Any]) -> Path:
    path = runs_dir / eval_run_id / 'evaluations' / f'{evaluation["eval_case_id"]}.evaluation.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evaluation, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def _write_trace_step(tmp_path: Path, session_id: str, step: int, payload: dict[str, Any]) -> Path:
    trace_dir = tmp_path / 'trace' / '2026-04-28' / '12-00-00' / session_id
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / f'step-{step:03d}-trace.json'
    path.write_text(json.dumps({'step': step, **payload}, ensure_ascii=False), encoding='utf-8')
    return path


def _evaluation(
    eval_run_id: str,
    eval_case_id: str,
    *,
    status: str = 'judged',
    host: str = 'shop.example',
    session_id: str | None = None,
    duration_ms: int | None = 1000,
    buyer_tokens_used: int | None = 100,
    outcome: str = 'ok',
    recommendations: int = 0,
) -> dict[str, Any]:
    return {
        'eval_run_id': eval_run_id,
        'eval_case_id': eval_case_id,
        'case_version': '1',
        'session_id': session_id or f'session-{eval_run_id}-{eval_case_id}',
        'host': host,
        'status': status,
        'metrics': {
            'duration_ms': duration_ms,
            'buyer_tokens_used': buyer_tokens_used,
            'judge_tokens_used': None,
        },
        'checks': {
            'outcome_ok': {'status': outcome, 'reason': 'Цель проверена.', 'evidence_refs': []},
            'safety_ok': {'status': 'ok', 'reason': 'Опасных действий нет.', 'evidence_refs': []},
            'payment_boundary_ok': {'status': 'ok', 'reason': 'Остановлено на SberPay.', 'evidence_refs': []},
            'evidence_ok': {'status': 'ok', 'reason': 'Есть trace evidence.', 'evidence_refs': []},
            'recommendations_ok': {'status': 'ok', 'reason': 'Рекомендации применимы.', 'evidence_refs': []},
        },
        'evidence_refs': [],
        'recommendations': [
            {
                'category': 'prompt',
                'priority': 'medium',
                'rationale': f'Причина {index}.',
                'evidence_refs': [],
                'draft_text': f'Рекомендация {index}.',
            }
            for index in range(recommendations)
        ],
        'judge_metadata': {'backend': 'codex_exec', 'model': 'gpt-5.5'},
    }
