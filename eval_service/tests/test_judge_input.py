from __future__ import annotations

from pathlib import Path

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
