from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .redaction import sanitize_for_judge_input


TRACE_DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TRACE_TIME_DIR_RE = re.compile(r'^\d{2}-\d{2}-\d{2}$')
STEP_TRACE_RE = re.compile(r'^step-(\d{3})-trace\.json$')
STEP_ACTIONS_RE = re.compile(r'^step-(\d{3})-browser-actions\.jsonl$')
SCREENSHOT_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp'}

TRACE_SUMMARY_KEYS = (
    'duration_ms',
    'codex_returncode',
    'codex_model',
    'codex_tokens_used',
    'model_strategy',
    'model_fallback_reason',
    'preflight_summary',
    'prompt_preview',
    'stdout_tail',
    'stderr_tail',
    'post_browser_idle_ms',
)
ACTION_SUMMARY_KEYS = (
    'command_duration_ms',
    'inter_command_idle_ms',
    'browser_busy_union_ms',
    'top_idle_gaps',
    'last_command_finished_epoch_ms',
    'command_errors',
    'html_commands',
    'html_bytes',
    'command_breakdown',
)


def find_trace_session_dir(trace_root: Path | str, session_id: str) -> Path | None:
    root = Path(trace_root).expanduser()
    if not root.is_dir():
        return None

    matches: list[Path] = []
    for date_dir in _iter_dirs(root):
        if TRACE_DATE_DIR_RE.fullmatch(date_dir.name) is None:
            continue
        for time_dir in _iter_dirs(date_dir):
            if TRACE_TIME_DIR_RE.fullmatch(time_dir.name) is None:
                continue
            candidate = time_dir / session_id
            if candidate.is_dir():
                matches.append(candidate)

    if not matches:
        return None
    return sorted(matches, key=lambda path: (path.parent.parent.name, path.parent.name, str(path)))[-1]


def collect_trace_session(
    trace_root: Path | str,
    session_id: str,
    *,
    browser_actions_tail_limit: int = 20,
) -> dict[str, Any]:
    trace_dir = find_trace_session_dir(trace_root, session_id)
    if trace_dir is None:
        return {'session_id': session_id, 'trace_dir': None, 'steps': []}

    return collect_trace_session_dir(trace_dir, browser_actions_tail_limit=browser_actions_tail_limit)


def iter_trace_session_dirs(trace_root: Path | str) -> list[Path]:
    root = Path(trace_root).expanduser()
    if not root.is_dir():
        return []

    session_dirs: list[Path] = []
    for date_dir in _iter_dirs(root):
        if TRACE_DATE_DIR_RE.fullmatch(date_dir.name) is None:
            continue
        for time_dir in _iter_dirs(date_dir):
            if TRACE_TIME_DIR_RE.fullmatch(time_dir.name) is None:
                continue
            session_dirs.extend(_iter_dirs(time_dir))
    return sorted(session_dirs, key=lambda path: (path.parent.parent.name, path.parent.name, path.name))


def collect_trace_session_dir(
    trace_dir: Path | str,
    *,
    browser_actions_tail_limit: int = 20,
) -> dict[str, Any]:
    trace_dir = Path(trace_dir).expanduser()
    session_id = trace_dir.name
    steps = [
        _build_step_summary(trace_file, trace_dir=trace_dir, browser_actions_tail_limit=browser_actions_tail_limit)
        for trace_file in sorted(trace_dir.glob('step-*-trace.json'), key=_step_sort_key)
        if STEP_TRACE_RE.fullmatch(trace_file.name) is not None
    ]
    return sanitize_for_judge_input(
        {
            'session_id': session_id,
            'trace_dir': str(trace_dir),
            'steps': steps,
        }
    )


def _build_step_summary(trace_file: Path, *, trace_dir: Path, browser_actions_tail_limit: int) -> dict[str, Any]:
    trace_payload = _read_json_object(trace_file)
    step_index = _extract_step_index(trace_file, trace_payload)
    step_tag = f'step-{step_index:03d}'
    actions_file = _resolve_actions_file(
        trace_payload.get('browser_actions_log_path'),
        trace_dir=trace_dir,
        step_tag=step_tag,
    )
    actions_total, actions_tail, actions_summary = summarize_browser_actions(
        actions_file,
        tail_limit=browser_actions_tail_limit,
    )

    item: dict[str, Any] = {
        'step': step_index,
        'trace_file': trace_file.name,
        'browser_actions_file': actions_file.name if actions_file is not None else None,
        'browser_actions_total': actions_total if actions_file is not None else _int_or_zero(trace_payload.get('browser_actions_total')),
        'browser_actions_summary': _merge_action_summary(trace_payload, actions_summary),
        'browser_actions_tail': actions_tail,
        'screenshots': _find_step_screenshots(trace_dir, step_tag),
    }
    for key in TRACE_SUMMARY_KEYS:
        if key in trace_payload:
            item[key] = trace_payload[key]
    return item


def summarize_browser_actions(path: Path | None, *, tail_limit: int) -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
    if path is None or not path.is_file():
        return 0, [], _empty_actions_summary()

    records: list[dict[str, Any]] = []
    total = 0
    try:
        raw_lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return 0, [], _empty_actions_summary()

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        total += 1
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = {'event': 'json_parse_error', 'line_tail': line[-500:]}
        if not isinstance(parsed, dict):
            parsed = {'event': 'json_non_object', 'value': parsed}
        records.append(sanitize_for_judge_input(parsed))

    summary = _build_actions_summary(records)
    return total, records[-max(tail_limit, 1) :], summary


def _merge_action_summary(trace_payload: dict[str, Any], actions_summary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(actions_summary)
    for key in ACTION_SUMMARY_KEYS:
        if key in trace_payload and not merged.get(key):
            merged[key] = trace_payload[key]
    return merged


def _build_actions_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _empty_actions_summary()
    breakdown: dict[str, dict[str, int]] = {}
    for record in records:
        command = record.get('command')
        if not isinstance(command, str) or not command:
            continue
        event = record.get('event')
        if event not in {'browser_command_finished', 'browser_command_failed'}:
            continue

        duration_ms = _int_or_zero(record.get('duration_ms'))
        failed = event == 'browser_command_failed' or record.get('ok') is False
        command_stats = breakdown.setdefault(command, {'count': 0, 'duration_ms': 0, 'errors': 0})
        command_stats['count'] += 1
        command_stats['duration_ms'] += duration_ms
        if failed:
            command_stats['errors'] += 1
        summary['command_duration_ms'] += duration_ms

    summary['command_breakdown'] = breakdown
    summary['command_errors'] = sum(item['errors'] for item in breakdown.values())
    return summary


def _empty_actions_summary() -> dict[str, Any]:
    return {
        'command_duration_ms': 0,
        'inter_command_idle_ms': 0,
        'browser_busy_union_ms': 0,
        'top_idle_gaps': [],
        'last_command_finished_epoch_ms': None,
        'command_errors': 0,
        'html_commands': 0,
        'html_bytes': 0,
        'command_breakdown': {},
    }


def _resolve_actions_file(value: Any, *, trace_dir: Path, step_tag: str) -> Path | None:
    candidates: list[Path] = []
    if isinstance(value, str) and value.strip():
        raw_path = Path(value.strip())
        candidates.append(raw_path if raw_path.is_absolute() else trace_dir / raw_path)
    candidates.append(trace_dir / f'{step_tag}-browser-actions.jsonl')

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            trace_root = trace_dir.resolve(strict=False)
        except OSError:
            continue
        if _is_relative_to(resolved, trace_root) and resolved.is_file() and STEP_ACTIONS_RE.fullmatch(resolved.name):
            return resolved
    return None


def _find_step_screenshots(trace_dir: Path, step_tag: str) -> list[str]:
    screenshots = [
        path.name
        for path in trace_dir.glob(f'{step_tag}*')
        if path.is_file() and path.suffix.lower() in SCREENSHOT_SUFFIXES
    ]
    return sorted(screenshots)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _extract_step_index(path: Path, payload: dict[str, Any]) -> int:
    raw_step = payload.get('step')
    if isinstance(raw_step, int) and raw_step >= 0:
        return raw_step
    match = STEP_TRACE_RE.fullmatch(path.name)
    if match is None:
        return 0
    return int(match.group(1))


def _step_sort_key(path: Path) -> tuple[int, str]:
    match = STEP_TRACE_RE.fullmatch(path.name)
    if match is None:
        return (0, path.name)
    return (int(match.group(1)), path.name)


def _iter_dirs(path: Path) -> list[Path]:
    try:
        return [item for item in path.iterdir() if item.is_dir()]
    except OSError:
        return []


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _int_or_zero(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0
