from __future__ import annotations

from pathlib import Path
import json

import pytest
from pydantic import ValidationError

from eval_service.app.judge_input import write_judge_input
from eval_service.app.models import EvalCase, ExpectedOutcome


def _case(eval_case_id: str = 'case-a') -> EvalCase:
    return EvalCase(
        eval_case_id=eval_case_id,
        case_version='1',
        variant_id='variant-a',
        title='Case A',
        host='example.test',
        task='Подготовить покупку до платежной границы.',
        start_url='https://example.test/',
        expected_outcome=ExpectedOutcome(target='Товар', stop_condition='SberPay ready'),
    )


def test_write_judge_input_rejects_eval_run_id_path_segment_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='eval_run_id'):
        write_judge_input(
            run_dir=tmp_path,
            eval_run_id='../evil',
            case=_case(),
            session_id='session-123',
            task_payload={},
            events=[],
            metrics={},
            trace_summary={},
        )

    assert not (tmp_path.parent / 'evil').exists()


def test_write_judge_input_rejects_eval_case_id_path_segment_traversal() -> None:
    with pytest.raises(ValidationError):
        _case('../evil')


def test_write_judge_input_keeps_output_inside_evaluations(tmp_path: Path) -> None:
    output_path = write_judge_input(
        run_dir=tmp_path,
        eval_run_id='eval-20260428-120000',
        case=_case('case.with-safe_chars-1'),
        session_id='session-123',
        task_payload={},
        events=[],
        metrics={},
        trace_summary={},
    )

    evaluations_dir = (tmp_path / 'evaluations').resolve()
    assert output_path.resolve().parent == evaluations_dir
    assert output_path.name == 'case.with-safe_chars-1.judge-input.json'


def test_write_judge_input_preserves_redacted_payload_and_adds_trace_file_refs(tmp_path: Path) -> None:
    trace_dir = tmp_path / 'trace' / '2026-04-29' / '12-00-00' / 'session-123'
    trace_dir.mkdir(parents=True)
    (trace_dir / 'step-001-trace.json').write_text('{"large":"trace"}', encoding='utf-8')
    (trace_dir / 'step-001-browser-actions.jsonl').write_text('{"large":"actions"}\n', encoding='utf-8')
    huge_html = '<html>' + ('x' * 300_000) + '</html>'

    output_path = write_judge_input(
        run_dir=tmp_path,
        eval_run_id='eval-20260428-120000',
        case=_case(),
        session_id='session-123',
        task_payload={'task': 'Купить книгу'},
        events=[
            {
                'event_id': 'event-stream-1',
                'event_type': 'agent_stream_event',
                'occurred_at': '2026-04-29T12:00:01Z',
                'session_id': 'session-123',
                'payload': {
                    'source': 'browser',
                    'stream': 'browser_actions',
                    'step': 1,
                    'sequence': 17,
                    'message': 'browser_command_finished',
                    'items': [{'result': {'html': huge_html}}],
                },
            },
            {
                'event_id': 'event-payment',
                'event_type': 'payment_ready',
                'occurred_at': '2026-04-29T12:00:02Z',
                'session_id': 'session-123',
                'payload': {
                    'message': 'SberPay открыт',
                    'order_id': 'order-secret',
                    'order_id_host': 'payecom.ru',
                },
            },
        ],
        metrics={'duration_ms': 1234},
        trace_summary={
            'session_id': 'session-123',
            'trace_dir': str(trace_dir),
            'steps': [
                {
                    'step': 1,
                    'trace_file': 'step-001-trace.json',
                    'browser_actions_file': 'step-001-browser-actions.jsonl',
                    'browser_actions_total': 42,
                    'browser_actions_summary': {'command_errors': 0, 'html_bytes': 300000},
                    'browser_actions_tail': [{'result': {'html': huge_html}}],
                    'stdout_tail': huge_html,
                    'stderr_tail': '',
                    'screenshots': ['step-001-checkout.png'],
                }
            ],
        },
        artifacts={'trace': 'artifacts/session.trace.json'},
        case_state='finished',
        case_run={
            'eval_case_id': 'case-a',
            'state': 'finished',
            'session_id': 'session-123',
            'callback_events': [{'payload': {'items': [{'html': huge_html}]}}],
        },
    )

    payload = json.loads(output_path.read_text(encoding='utf-8'))
    text = json.dumps(payload, ensure_ascii=False)

    assert huge_html in text
    assert payload['case_run']['callback_events'][0]['payload']['items'][0]['html'] == huge_html
    assert payload['events'][0]['payload']['items'][0]['result']['html'] == huge_html
    assert payload['trace']['steps'][0]['browser_actions_tail'][0]['result']['html'] == huge_html
    assert payload['trace']['steps'][0]['stdout_tail'] == huge_html
    assert str(trace_dir / 'step-001-trace.json') in payload['evidence_files']['trace_files']
    assert str(trace_dir / 'step-001-browser-actions.jsonl') in payload['evidence_files']['browser_actions_files']
