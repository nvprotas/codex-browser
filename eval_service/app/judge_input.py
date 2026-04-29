from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import EvalCase, validate_path_segment_id
from .redaction import sanitize_for_judge_input


def write_judge_input(
    *,
    run_dir: Path | str,
    eval_run_id: str,
    case: EvalCase,
    session_id: str,
    task_payload: dict[str, Any],
    events: list[dict[str, Any]],
    metrics: dict[str, Any],
    trace_summary: dict[str, Any],
    artifacts: dict[str, Any] | None = None,
    case_state: str | None = None,
    case_run: dict[str, Any] | None = None,
) -> Path:
    validate_path_segment_id(eval_run_id, 'eval_run_id')
    validate_path_segment_id(case.eval_case_id, 'eval_case_id')
    evaluations_dir = Path(run_dir) / 'evaluations'
    evaluations_dir.mkdir(parents=True, exist_ok=True)
    output_path = evaluations_dir / f'{case.eval_case_id}.judge-input.json'
    if output_path.resolve().parent != evaluations_dir.resolve():
        raise ValueError('judge-input должен записываться внутри evaluations')

    evidence_files = _evidence_files(
        run_dir=Path(run_dir),
        output_path=output_path,
        trace_summary=trace_summary,
    )
    payload = {
        'eval_run_id': eval_run_id,
        'eval_case_id': case.eval_case_id,
        'case_version': case.case_version,
        'host': case.host,
        'session_id': session_id,
        'case': case.model_dump(mode='json'),
        'task_payload': task_payload,
        'events': events,
        'trace': trace_summary,
        'artifacts': artifacts or {},
        'metrics': metrics,
        'evidence_files': evidence_files,
    }
    if case_state is not None:
        payload['case_state'] = case_state
    if case_run is not None:
        payload['case_run'] = case_run
    _write_json_atomic(output_path, sanitize_for_judge_input(payload))
    return output_path


def _evidence_files(
    *,
    run_dir: Path,
    output_path: Path,
    trace_summary: dict[str, Any],
) -> dict[str, Any]:
    trace_dir_value = trace_summary.get('trace_dir')
    trace_dir = Path(trace_dir_value) if isinstance(trace_dir_value, str) and trace_dir_value else None
    trace_files: list[str] = []
    browser_actions_files: list[str] = []
    screenshots: list[str] = []
    for step in trace_summary.get('steps') or []:
        if not isinstance(step, dict) or trace_dir is None:
            continue
        trace_file = step.get('trace_file')
        if isinstance(trace_file, str) and trace_file:
            trace_files.append(str(trace_dir / trace_file))
        actions_file = step.get('browser_actions_file')
        if isinstance(actions_file, str) and actions_file:
            browser_actions_files.append(str(trace_dir / actions_file))
        for screenshot in step.get('screenshots') or []:
            if isinstance(screenshot, str) and screenshot:
                screenshots.append(str(trace_dir / screenshot))

    return {
        'run_dir': str(run_dir),
        'manifest': str(run_dir / 'manifest.json'),
        'judge_input': str(output_path),
        'evaluation_output': str(output_path.with_name(output_path.name.replace('.judge-input.json', '.evaluation.json'))),
        'trace_dir': str(trace_dir) if trace_dir is not None else None,
        'trace_files': trace_files,
        'browser_actions_files': browser_actions_files,
        'screenshots': screenshots,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    tmp_path.replace(path)
