from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import EvalCase
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
) -> Path:
    evaluations_dir = Path(run_dir) / 'evaluations'
    evaluations_dir.mkdir(parents=True, exist_ok=True)
    output_path = evaluations_dir / f'{case.eval_case_id}.judge-input.json'

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
    }
    _write_json_atomic(output_path, sanitize_for_judge_input(payload))
    return output_path


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + '.tmp')
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    tmp_path.replace(path)
