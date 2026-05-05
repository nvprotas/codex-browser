from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from eval_service.app.models import EvalRunManifest
from eval_service.app.trace_collector import collect_trace_session_dir, iter_trace_session_dirs


_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')


@dataclass(frozen=True)
class EvalSessionMeta:
    eval_run_id: str
    eval_case_id: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None


def build_stats_sessions_payload(trace_root: Path | str, runs_dir: Path | str) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    eval_index = _build_eval_session_index(Path(runs_dir), warnings=warnings)
    sessions = []
    for trace_dir in iter_trace_session_dirs(trace_root):
        session = _stats_session_from_trace(trace_dir, eval_index.get(trace_dir.name))
        if session['step_count'] > 0:
            sessions.append(session)
    sessions.sort(key=lambda item: (item.get('start_ts') or 0, item.get('session_id') or ''), reverse=True)
    return {'sessions': sessions, 'warnings': warnings}


def _build_eval_session_index(runs_dir: Path, *, warnings: list[dict[str, str]]) -> dict[str, EvalSessionMeta]:
    index: dict[str, EvalSessionMeta] = {}
    if not runs_dir.is_dir():
        return index

    for manifest_path in sorted(runs_dir.glob('*/manifest.json')):
        try:
            manifest = EvalRunManifest.model_validate_json(manifest_path.read_text(encoding='utf-8'))
        except Exception as exc:  # noqa: BLE001 - stats UI должен переживать битые artifacts
            warnings.append({'path': str(manifest_path), 'error': str(exc)})
            continue

        for case in manifest.cases:
            if not case.session_id:
                continue
            index[case.session_id] = EvalSessionMeta(
                eval_run_id=manifest.eval_run_id,
                eval_case_id=case.eval_case_id,
                status=case.state.value,
                started_at=case.started_at,
                finished_at=case.finished_at,
            )
    return index


def _stats_session_from_trace(trace_dir: Path, eval_meta: EvalSessionMeta | None) -> dict[str, Any]:
    trace = collect_trace_session_dir(trace_dir, browser_actions_tail_limit=20)
    steps = [_stats_step(step) for step in trace.get('steps') or []]
    duration_ms = sum(_int(step.get('duration_ms')) for step in steps)
    tokens_total = sum(_int(step.get('codex_tokens_used')) for step in steps)
    cdp_count = sum(_int(step.get('total_cmds')) for step in steps)
    errors = sum(_int(step.get('command_errors')) for step in steps)
    screenshot_count = sum(len(step.get('screenshots') or []) for step in steps)

    if duration_ms == 0 and eval_meta and eval_meta.started_at and eval_meta.finished_at:
        duration_ms = int((eval_meta.finished_at - eval_meta.started_at).total_seconds() * 1000)

    status = eval_meta.status if eval_meta is not None else _direct_status(steps)
    return {
        'session_id': trace_dir.name,
        'source': 'eval' if eval_meta is not None else 'direct',
        'eval_run_id': eval_meta.eval_run_id if eval_meta is not None else None,
        'eval_case_id': eval_meta.eval_case_id if eval_meta is not None else None,
        'host': _host_from_trace(trace) or 'unknown',
        'status': status,
        'start_ts': _trace_start_ts(trace_dir),
        'duration_ms': duration_ms,
        'tokens_total': tokens_total,
        'steps': steps,
        'step_count': len(steps),
        'cdp_count': cdp_count,
        'errors': errors,
        'screenshot_count': screenshot_count,
        'trace_dir': trace.get('trace_dir') or str(trace_dir),
    }


def _stats_step(step: dict[str, Any]) -> dict[str, Any]:
    action_summary = _dict(step.get('browser_actions_summary'))
    breakdown = _normalize_command_breakdown(action_summary.get('command_breakdown'))
    total_cmds = sum(_int(item.get('count')) for item in breakdown.values())
    if total_cmds == 0:
        total_cmds = _int(step.get('browser_actions_total'))

    return {
        'step': _int(step.get('step')),
        'trace_file': step.get('trace_file'),
        'browser_actions_file': step.get('browser_actions_file'),
        'browser_actions_total': _int(step.get('browser_actions_total')),
        'codex_model': step.get('codex_model') or '',
        'codex_returncode': _int(step.get('codex_returncode')),
        'duration_ms': _int(step.get('duration_ms')),
        'codex_tokens_used': _int(step.get('codex_tokens_used')),
        'post_browser_idle_ms': _int(step.get('post_browser_idle_ms')),
        'command_duration_ms': _int(action_summary.get('command_duration_ms')),
        'inter_command_idle_ms': _int(action_summary.get('inter_command_idle_ms')),
        'browser_busy_union_ms': _int(action_summary.get('browser_busy_union_ms')),
        'command_errors': _int(action_summary.get('command_errors')),
        'html_commands': _int(action_summary.get('html_commands')),
        'html_bytes': _int(action_summary.get('html_bytes')),
        'command_breakdown': breakdown,
        'llm_duration_ms': max(_int(step.get('duration_ms')) - _int(action_summary.get('command_duration_ms')), 0),
        'total_cmds': total_cmds,
        'stdout_tail': step.get('stdout_tail') or '',
        'stderr_tail': step.get('stderr_tail') or '',
        'screenshots': step.get('screenshots') or [],
    }


def _normalize_command_breakdown(value: Any) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    if not isinstance(value, dict):
        return result
    for command, raw_stats in value.items():
        if not isinstance(command, str) or not command:
            continue
        if isinstance(raw_stats, dict):
            result[command] = {
                'count': _int(raw_stats.get('count')),
                'duration_ms': _int(raw_stats.get('duration_ms')),
                'errors': _int(raw_stats.get('errors')),
            }
        else:
            result[command] = {'count': _int(raw_stats), 'duration_ms': 0, 'errors': 0}
    return result


def _direct_status(steps: list[dict[str, Any]]) -> str:
    if any(_int(step.get('codex_returncode')) != 0 for step in steps):
        return 'failed'
    return 'completed' if steps else 'unknown'


def _host_from_trace(trace: dict[str, Any]) -> str | None:
    for step in trace.get('steps') or []:
        for key in ('preflight_summary', 'stdout_tail', 'stderr_tail'):
            host = _host_from_text(step.get(key))
            if host:
                return host
        for record in step.get('browser_actions_tail') or []:
            host = _host_from_text(record)
            if host:
                return host
    return None


def _host_from_text(value: Any) -> str | None:
    text = value if isinstance(value, str) else str(value)
    match = _URL_RE.search(text)
    if match is None:
        return None
    parsed = urlparse(match.group(0))
    return parsed.hostname


def _trace_start_ts(trace_dir: Path) -> int | None:
    try:
        value = datetime.strptime(
            f'{trace_dir.parent.parent.name} {trace_dir.parent.name}',
            '%Y-%m-%d %H-%M-%S',
        ).replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(value.timestamp() * 1000)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0
