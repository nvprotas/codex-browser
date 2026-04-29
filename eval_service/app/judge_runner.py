from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .judge_prompt import build_judge_prompt_from_payload
from .settings import Settings


DEFAULT_EVALUATION_SCHEMA_PATH = Path('eval_service/app/evaluation_schema.json')
DEFAULT_JUDGE_TIMEOUT_SEC = 600
EVALUATION_CHECKS = (
    'outcome_ok',
    'safety_ok',
    'payment_boundary_ok',
    'evidence_ok',
    'recommendations_ok',
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class JudgeRunResult:
    evaluation_path: Path
    evaluation: dict[str, Any]


def write_fallback_evaluation(
    evaluation_path: Path | str,
    judge_input: dict[str, Any],
    *,
    status: str,
    reason: str,
    model: str,
    schema_validator: Draft202012Validator | None = None,
) -> JudgeRunResult:
    output_path = Path(evaluation_path)
    evaluation = _fallback_evaluation(
        judge_input,
        status=status,
        reason=reason,
        model=model,
    )
    validator = schema_validator or Draft202012Validator(_read_schema(DEFAULT_EVALUATION_SCHEMA_PATH))
    validator.validate(evaluation)
    _write_json_atomic(output_path, evaluation)
    return JudgeRunResult(evaluation_path=output_path, evaluation=evaluation)


class JudgeRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: Runner | None = None,
        codex_bin: str = 'codex',
        schema_path: Path | str = DEFAULT_EVALUATION_SCHEMA_PATH,
        timeout_sec: int = DEFAULT_JUDGE_TIMEOUT_SEC,
    ) -> None:
        self._settings = settings
        self._runner = runner or subprocess.run
        self._codex_bin = codex_bin
        self._schema_path = Path(schema_path)
        self._timeout_sec = timeout_sec
        self._schema_validator = Draft202012Validator(_read_schema(self._schema_path))

    def run(self, judge_input_path: Path | str) -> JudgeRunResult:
        input_path = Path(judge_input_path)
        judge_input = _read_json(input_path)
        evaluation_path = _evaluation_path(input_path, judge_input)

        auth_skip_reason = _auth_skip_reason(judge_input)
        if auth_skip_reason is not None:
            return self._write_fallback(
                evaluation_path,
                judge_input,
                status='judge_skipped',
                reason=auth_skip_reason,
            )

        prompt = build_judge_prompt_from_payload(judge_input)
        cmd = self._build_command(evaluation_path=evaluation_path, prompt=prompt)
        try:
            completed = self._runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return self._write_fallback(
                evaluation_path,
                judge_input,
                status='judge_failed',
                reason=f'codex exec timeout after {self._timeout_sec}s.',
            )

        if completed.returncode != 0:
            output = _combined_process_output(completed)
            if _looks_like_missing_credentials(output):
                return self._write_fallback(
                    evaluation_path,
                    judge_input,
                    status='judge_skipped',
                    reason=(
                        'codex exec skipped: missing credentials/auth for judge. '
                        f'{_tail(output)}'
                    ),
                )
            return self._write_fallback(
                evaluation_path,
                judge_input,
                status='judge_failed',
                reason=f'codex exec failed with exit code {completed.returncode}. {_tail(output)}',
            )

        try:
            evaluation = _read_json(evaluation_path)
        except (FileNotFoundError, JSONDecodeError) as exc:
            return self._write_fallback(
                evaluation_path,
                judge_input,
                status='judge_failed',
                reason=f'codex exec returned invalid JSON: {exc}',
            )

        try:
            self._schema_validator.validate(evaluation)
        except ValidationError as exc:
            return self._write_fallback(
                evaluation_path,
                judge_input,
                status='judge_failed',
                reason=f'codex exec schema validation failed: {exc.message}',
            )

        identity_mismatch = _identity_mismatch_reason(evaluation, judge_input)
        if identity_mismatch is not None:
            return self._write_fallback(
                evaluation_path,
                judge_input,
                status='judge_failed',
                reason=identity_mismatch,
            )

        return JudgeRunResult(evaluation_path=evaluation_path, evaluation=evaluation)

    def _build_command(self, *, evaluation_path: Path, prompt: str) -> list[str]:
        return [
            self._codex_bin,
            'exec',
            '-m',
            self._settings.eval_judge_model,
            '--output-schema',
            str(self._schema_path),
            '-o',
            str(evaluation_path),
            prompt,
        ]

    def _write_fallback(
        self,
        evaluation_path: Path,
        judge_input: dict[str, Any],
        *,
        status: str,
        reason: str,
    ) -> JudgeRunResult:
        return write_fallback_evaluation(
            evaluation_path,
            judge_input,
            status=status,
            reason=reason,
            model=self._settings.eval_judge_model,
            schema_validator=self._schema_validator,
        )


def _evaluation_path(judge_input_path: Path, judge_input: dict[str, Any]) -> Path:
    eval_case_id = _string_value(
        judge_input.get('eval_case_id'),
        fallback=judge_input_path.stem.removesuffix('.judge-input'),
    )
    return judge_input_path.with_name(f'{eval_case_id}.evaluation.json')


def _fallback_evaluation(
    judge_input: dict[str, Any],
    *,
    status: str,
    reason: str,
    model: str,
) -> dict[str, Any]:
    return {
        'eval_run_id': _string_value(judge_input.get('eval_run_id'), fallback='unknown-eval-run'),
        'eval_case_id': _string_value(
            judge_input.get('eval_case_id'),
            fallback='unknown-eval-case',
        ),
        'case_version': _string_value(judge_input.get('case_version'), fallback='unknown-version'),
        'session_id': _string_value(judge_input.get('session_id'), fallback='unknown-session'),
        'host': _string_value(judge_input.get('host'), fallback='unknown-host'),
        'status': status,
        'metrics': {
            'duration_ms': _non_negative_int_or_none(
                (judge_input.get('metrics') or {}).get('duration_ms')
            ),
            'buyer_tokens_used': _non_negative_int_or_none(
                (judge_input.get('metrics') or {}).get('buyer_tokens_used')
            ),
            'judge_tokens_used': None,
        },
        'checks': {
            check_name: {'status': 'skipped', 'reason': reason, 'evidence_refs': []}
            for check_name in EVALUATION_CHECKS
        },
        'evidence_refs': [],
        'recommendations': [],
        'judge_metadata': {
            'backend': 'codex_exec',
            'model': _string_value(model, fallback='unknown-model'),
        },
    }


def _identity_mismatch_reason(evaluation: dict[str, Any], judge_input: dict[str, Any]) -> str | None:
    mismatches = []
    for key in ('eval_run_id', 'eval_case_id', 'case_version', 'session_id', 'host'):
        expected = _string_value(judge_input.get(key), fallback='')
        actual = _string_value(evaluation.get(key), fallback='')
        if actual != expected:
            mismatches.append(f'{key}: expected {expected!r}, got {actual!r}')
    if not mismatches:
        return None
    return 'codex exec identity mismatch after schema validation: ' + '; '.join(mismatches)


def _auth_skip_reason(judge_input: dict[str, Any]) -> str | None:
    for key in ('case_state', 'runtime_state', 'live_state', 'state'):
        if judge_input.get(key) == 'skipped_auth_missing':
            return (
                'live case state is skipped_auth_missing; '
                'judge is skipped without changing live outcome.'
            )

    case_run = judge_input.get('case_run')
    if isinstance(case_run, dict) and case_run.get('state') == 'skipped_auth_missing':
        return (
            'live case state is skipped_auth_missing; '
            'judge is skipped without changing live outcome.'
        )

    skip_reason = judge_input.get('skip_reason')
    if isinstance(skip_reason, dict) and skip_reason.get('reason') in {
        'auth_profile_missing',
        'auth_profile_invalid',
    }:
        return f'auth profile is unavailable: {skip_reason.get("reason")}.'
    if skip_reason in {'auth_profile_missing', 'auth_profile_invalid', 'skipped_auth_missing'}:
        return f'auth profile is unavailable: {skip_reason}.'

    return None


def _looks_like_missing_credentials(output: str) -> bool:
    normalized = output.lower()
    markers = (
        'no credentials',
        'missing credentials',
        'not logged in',
        'codex login',
        'api key',
        'openai_api_key',
        'auth.json',
        'authentication',
        'unauthorized',
    )
    return any(marker in normalized for marker in markers)


def _combined_process_output(completed: subprocess.CompletedProcess[str]) -> str:
    return '\n'.join(part for part in (completed.stdout, completed.stderr) if part)


def _tail(value: str, *, limit: int = 500) -> str:
    value = value.strip()
    if not value:
        return ''
    if len(value) <= limit:
        return value
    return value[-limit:]


def _read_schema(schema_path: Path) -> dict[str, Any]:
    if schema_path.exists():
        return _read_json(schema_path)
    local_schema_path = Path(__file__).with_name(schema_path.name)
    return _read_json(local_schema_path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    tmp_path = path.with_name(f'.{path.name}.tmp')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp_path.write_text(f'{text}\n', encoding='utf-8')
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _string_value(value: Any, *, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value)
    return text if text else fallback


def _non_negative_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
