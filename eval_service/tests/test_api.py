from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

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
        storage_state: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        call = {
            'task': task,
            'start_url': start_url,
            'metadata': metadata or {},
            'callback_url': callback_url,
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
            payload={'payment_method': 'sberpay'},
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
    assert body['evaluations'][0]['status'] == 'judged'
    assert (tmp_path / 'eval-run-001' / 'evaluations' / 'case-a.judge-input.json').is_file()
    assert (tmp_path / 'eval-run-001' / 'evaluations' / 'case-a.evaluation.json').is_file()
    assert judge_runner.inputs[0]['case_run']['state'] == 'finished'
    assert judge_runner.inputs[0]['case_state'] == 'finished'
    assert judge_runner.inputs[0]['artifacts'] == {'receipt': 'artifacts/receipt.json'}
    assert judge_runner.inputs[0]['trace'] == {'session_id': 'session-a', 'trace_dir': None, 'steps': []}


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
    assert 'skipped_auth_missing' in body['evaluations'][0]['checks']['outcome_ok']['reason']
    assert store.read_manifest('eval-run-001').cases[0].state == CaseRunState.SKIPPED_AUTH_MISSING


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
    assert case_rows[0]['checks']['outcome_ok'] == {'ok': 1, 'not_ok': 0, 'skipped': 1}
    assert case_rows[0]['metrics']['duration_ms']['median'] == 2000
    assert host_rows[0]['host'] == 'shop.example'
    assert host_rows[0]['total'] == 2
    assert host_rows[0]['cases'] == ['case-a']


def test_existing_post_runs_route_still_works_after_api_router_include(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, eval_runs_dir=tmp_path, buyer_api_base_url='http://buyer.test')
    app = create_app(settings)
    store = RunStore(tmp_path, clock=lambda: datetime(2026, 4, 28, 12, 0, tzinfo=UTC))
    app.state.run_store = store
    app.state.case_registry = FakeCaseRegistry([_case('case-a')])
    app.state.buyer_client = FinishingBuyerClient(store)
    app.state.eval_run_id_generator = lambda: 'eval-run-001'
    app.state.orchestrator_sleep = _no_sleep
    client = TestClient(app)

    response = client.post('/runs', json={'case_ids': ['case-a']})

    assert response.status_code == 200
    assert response.json()['eval_run_id'] == 'eval-run-001'
    assert response.json()['status'] == 'finished'


async def _no_sleep(_seconds: float) -> None:
    return None


def _client(tmp_path: Path, *, cases: list[EvalCase]) -> tuple[TestClient, RunStore]:
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
    return TestClient(app), store


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
        'recommendations': [],
        'judge_metadata': {'backend': 'codex_exec', 'model': 'gpt-5.5'},
    }
