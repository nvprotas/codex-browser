from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ._utils import (
    duration_ms_since as _duration_ms_since,
    remove_file_quietly as _remove_file_quietly,
    tail_text as _tail_text,
    trace_date_dir_name as _trace_date_dir_name,
    trace_time_dir_name as _trace_time_dir_name,
)
from .models import AgentOutput, TaskAuthPayload
from .agent_context_files import write_agent_context_files
from .agent_instruction_manifest import build_agent_instruction_manifest
from .prompt_builder import build_agent_prompt
from .settings import Settings
from .user_profile import load_user_profile

logger = logging.getLogger('uvicorn.error')

MUTATING_BROWSER_COMMANDS = {'click', 'fill', 'press'}
STREAM_BATCH_INTERVAL_SEC = 0.5
STREAM_BATCH_SIZE = 20
PROCESS_STREAM_READ_CHUNK_SIZE = 32768
STREAM_TEXT_MAX_CHARS = 4000
STREAM_TEXT_HEAD_CHARS = 2500
STREAM_TEXT_TAIL_CHARS = 1000

AgentStreamCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class _CodexAttemptSpec:
    role: str
    model: str | None


@dataclass
class _CodexAttemptResult:
    spec: _CodexAttemptSpec
    command_for_log: list[str]
    output_path: str
    stdout_text: str = ''
    stderr_text: str = ''
    codex_returncode: int | None = None
    duration_ms: int | None = None
    result: AgentOutput | None = None
    failure_message: str | None = None
    failure_reason: str | None = None
    codex_started_at: datetime | None = None


class AgentRunner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._schema_path = Path(__file__).with_name('codex_output_schema.json')

    async def run_step(
        self,
        *,
        session_id: str,
        step_index: int,
        task: str,
        start_url: str,
        metadata: dict[str, Any],
        auth: TaskAuthPayload | None,
        auth_context: dict[str, Any] | None,
        memory: list[dict[str, str]],
        latest_user_reply: str | None,
        stream_callback: AgentStreamCallback | None = None,
    ) -> AgentOutput:
        trace = self._prepare_trace_context(session_id=session_id, step_index=step_index)
        logger.info(
            'codex_step_started session_id=%s step=%s endpoint=%s trace_dir=%s',
            session_id,
            step_index,
            self._settings.browser_cdp_endpoint,
            trace['session_dir'],
        )
        probe_ok, probe_summary = await self._probe_browser_sidecar(
            self._settings.browser_cdp_endpoint,
            actions_log_path=trace['browser_actions_log_path'],
        )
        if not probe_ok:
            logger.error(
                'codex_step_preflight_failed session_id=%s step=%s reason=%s',
                session_id,
                step_index,
                _tail_text(probe_summary, limit=700),
            )
            return AgentOutput(
                status='failed',
                message=probe_summary,
                order_id=None,
                artifacts=self._build_trace_artifacts(
                    trace=trace,
                    preflight_summary=probe_summary,
                    prompt_hash=None,
                    prompt_preview=None,
                    command_for_log=None,
                    output_path=None,
                    stdout_text='',
                    stderr_text='',
                    codex_returncode=None,
                    duration_ms=None,
                ),
            )

        user_profile = load_user_profile(
            self._settings.buyer_user_info_path,
            max_chars=self._settings.buyer_user_info_max_chars,
        )
        context_file_manifest = write_agent_context_files(
            step_dir=trace['step_dir'],
            task=task,
            start_url=start_url,
            metadata=metadata,
            memory=memory,
            latest_user_reply=latest_user_reply,
            user_profile_text=user_profile.text,
            auth_state=_build_agent_auth_state(auth=auth, auth_context=auth_context),
        )

        prompt = build_agent_prompt(
            task=task,
            start_url=start_url,
            browser_cdp_endpoint=self._settings.browser_cdp_endpoint,
            instruction_manifest=build_agent_instruction_manifest(start_url=start_url),
            context_file_manifest=context_file_manifest,
            latest_user_reply=latest_user_reply,
        )
        trace['prompt_path'].write_text(prompt, encoding='utf-8')
        prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        prompt_preview = _preview_text(prompt, limit=self._settings.buyer_prompt_preview_chars)

        env = os.environ.copy()
        env['BROWSER_CDP_ENDPOINT'] = self._settings.browser_cdp_endpoint
        env['CDP_RECOVERY_WINDOW_SEC'] = str(self._settings.cdp_recovery_window_sec)
        env['CDP_RECOVERY_INTERVAL_MS'] = str(self._settings.cdp_recovery_interval_ms)
        env['BUYER_CDP_ACTIONS_LOG_PATH'] = str(trace['browser_actions_log_path'])

        has_api_key = bool(env.get('OPENAI_API_KEY'))
        has_oauth_file = Path('/root/.codex/auth.json').is_file()
        if not has_api_key and not has_oauth_file:
            logger.error(
                'codex_step_failed_auth session_id=%s step=%s reason=no_api_key_or_oauth',
                session_id,
                step_index,
            )
            return AgentOutput(
                status='failed',
                message=(
                    'Не найдена авторизация для codex: задайте OPENAI_API_KEY '
                    'или подключите CODEX_AUTH_JSON_PATH с OAuth auth.json, затем перезапустите buyer.'
                ),
                order_id=None,
                artifacts=self._build_trace_artifacts(
                    trace=trace,
                    preflight_summary=probe_summary,
                    prompt_hash=prompt_hash,
                    prompt_preview=prompt_preview,
                    command_for_log=None,
                    output_path=None,
                    stdout_text='',
                    stderr_text='',
                    codex_returncode=None,
                    duration_ms=None,
                    codex_started_at=None,
                    codex_model=None,
                    codex_attempts=[],
                    model_strategy=self._settings.buyer_model_strategy,
                    fallback_reason='no_api_key_or_oauth',
                ),
            )

        attempts = _build_model_attempt_specs(self._settings)
        initial_attempt_count = len(attempts)
        same_model_retry_added = False
        attempt_summaries: list[dict[str, Any]] = []
        attempt_results: list[_CodexAttemptResult] = []
        latest_attempt: _CodexAttemptResult | None = None
        fallback_reason: str | None = None
        codex_phase_started_at = datetime.now(timezone.utc)

        for attempt_index, attempt_spec in enumerate(attempts, start=1):
            attempt = await self._run_codex_attempt(
                trace=trace,
                step_index=step_index,
                prompt=prompt,
                env=env,
                attempt_spec=attempt_spec,
                stream_callback=stream_callback,
            )
            latest_attempt = attempt
            attempt_results.append(attempt)
            attempt_summaries.append(_summarize_codex_attempt(attempt))

            normalized = attempt.result.status.strip().lower() if attempt.result is not None else None
            if normalized is not None and normalized not in {'needs_user_input', 'completed', 'failed'}:
                attempt.failure_reason = 'invalid_status'
                attempt.failure_message = f'codex вернул неподдерживаемый статус: {attempt.result.status}'
                attempt_summaries[-1] = _summarize_codex_attempt(attempt)
                logger.error(
                    'codex_step_invalid_status session_id=%s step=%s status=%s model=%s',
                    session_id,
                    step_index,
                    attempt.result.status,
                    attempt.spec.model or 'default',
                )

            retryable_failed_status = normalized == 'failed'
            retryable_attempt_failure = attempt.result is None or attempt.failure_reason == 'invalid_status'
            if (
                attempt_index < len(attempts)
                and (retryable_failed_status or retryable_attempt_failure)
            ):
                if _browser_actions_have_mutating_commands(trace['browser_actions_log_path']):
                    fallback_reason = 'strong_retry_skipped_dirty_state'
                    logger.info(
                        'codex_step_strong_retry_skipped session_id=%s step=%s reason=%s',
                        session_id,
                        step_index,
                        fallback_reason,
                    )
                    break

                reset_ok, reset_summary = await self._reset_browser_to_start_url(
                    start_url=start_url,
                    actions_log_path=trace['browser_actions_log_path'],
                )
                if not reset_ok:
                    fallback_reason = 'strong_retry_skipped_reset_failed'
                    attempt_summaries.append(
                        {
                            'role': 'reset_before_strong',
                            'ok': False,
                            'reason': fallback_reason,
                            'message': reset_summary,
                        }
                    )
                    logger.info(
                        'codex_step_strong_retry_skipped session_id=%s step=%s reason=%s reset_summary=%s',
                        session_id,
                        step_index,
                        fallback_reason,
                        _tail_text(reset_summary, limit=500),
                    )
                    break

                attempt_summaries.append(
                    {
                        'role': 'reset_before_strong',
                        'ok': True,
                        'message': reset_summary,
                    }
                )
                continue

            if (
                initial_attempt_count == 1
                and not same_model_retry_added
                and retryable_failed_status
            ):
                if _browser_actions_have_mutating_commands(trace['browser_actions_log_path']):
                    fallback_reason = 'same_model_retry_skipped_dirty_state'
                    logger.info(
                        'codex_step_same_model_retry_skipped session_id=%s step=%s reason=%s',
                        session_id,
                        step_index,
                        fallback_reason,
                    )
                    break

                reset_ok, reset_summary = await self._reset_browser_to_start_url(
                    start_url=start_url,
                    actions_log_path=trace['browser_actions_log_path'],
                )
                if not reset_ok:
                    fallback_reason = 'same_model_retry_skipped_reset_failed'
                    attempt_summaries.append(
                        {
                            'role': 'reset_before_same_model',
                            'ok': False,
                            'reason': fallback_reason,
                            'message': reset_summary,
                        }
                    )
                    logger.info(
                        'codex_step_same_model_retry_skipped session_id=%s step=%s reason=%s reset_summary=%s',
                        session_id,
                        step_index,
                        fallback_reason,
                        _tail_text(reset_summary, limit=500),
                    )
                    break

                attempt_summaries.append(
                    {
                        'role': 'reset_before_same_model',
                        'ok': True,
                        'message': reset_summary,
                    }
                )
                attempts.append(_CodexAttemptSpec(role='same_model_retry', model=attempt.spec.model))
                same_model_retry_added = True
                continue

            break

        if latest_attempt is None:
            raise RuntimeError('codex step finished without attempts')

        aggregate_stdout_text = '\n'.join(item.stdout_text for item in attempt_results if item.stdout_text)
        aggregate_stderr_text = '\n'.join(item.stderr_text for item in attempt_results if item.stderr_text)
        trace_artifacts = self._build_trace_artifacts(
            trace=trace,
            preflight_summary=probe_summary,
            prompt_hash=prompt_hash,
            prompt_preview=prompt_preview,
            command_for_log=latest_attempt.command_for_log,
            output_path=latest_attempt.output_path,
            stdout_text=aggregate_stdout_text,
            stderr_text=aggregate_stderr_text,
            codex_returncode=latest_attempt.codex_returncode,
            duration_ms=_duration_ms_since(codex_phase_started_at),
            codex_started_at=codex_phase_started_at,
            codex_model=latest_attempt.spec.model,
            codex_attempts=attempt_summaries,
            model_strategy=self._settings.buyer_model_strategy,
            fallback_reason=fallback_reason or latest_attempt.failure_reason,
        )

        if latest_attempt.result is None or latest_attempt.failure_message is not None:
            message = latest_attempt.failure_message or 'codex не вернул структурированный результат.'
            return AgentOutput(
                status='failed',
                message=message,
                order_id=None,
                artifacts=trace_artifacts,
            )

        result = latest_attempt.result
        result.status = result.status.strip().lower()
        result.artifacts = _merge_artifacts(result.artifacts, trace_artifacts)
        logger.info(
            'codex_step_result session_id=%s step=%s status=%s order_id=%s trace_file=%s model=%s strategy=%s',
            session_id,
            step_index,
            result.status,
            result.order_id,
            (
                result.artifacts.get('trace', {}).get('trace_file')
                if isinstance(result.artifacts.get('trace'), dict)
                else None
            ),
            latest_attempt.spec.model or 'default',
            self._settings.buyer_model_strategy,
        )
        return result

    async def _run_codex_attempt(
        self,
        *,
        trace: dict[str, Any],
        step_index: int,
        prompt: str,
        env: dict[str, str],
        attempt_spec: _CodexAttemptSpec,
        stream_callback: AgentStreamCallback | None,
    ) -> _CodexAttemptResult:
        with tempfile.NamedTemporaryFile(
            prefix=f'codex-result-step-{step_index:03d}-{attempt_spec.role}-',
            suffix='.json',
            dir=trace['session_dir'],
            delete=False,
        ) as output_file:
            output_path = output_file.name

        cmd = _build_codex_command(
            settings=self._settings,
            schema_path=self._schema_path,
            output_path=output_path,
            prompt=prompt,
            model=attempt_spec.model,
        )
        command_for_log = [*cmd[:-1], f'@{trace["prompt_path"]}']
        attempt = _CodexAttemptResult(
            spec=attempt_spec,
            command_for_log=command_for_log,
            output_path=output_path,
            codex_started_at=datetime.now(timezone.utc),
        )
        stream_publisher = _AgentStreamPublisher(
            session_id=trace['session_id'],
            step_index=step_index,
            callback=stream_callback,
        )

        logger.info(
            'codex_step_exec step=%s prompt_path=%s model=%s role=%s sandbox=%s',
            step_index,
            trace['prompt_path'],
            attempt_spec.model or 'default',
            attempt_spec.role,
            self._settings.codex_sandbox_mode,
        )

        try:
            browser_actions_offset = _file_size(trace['browser_actions_log_path'])
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=self._settings.codex_workdir,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except FileNotFoundError:
                logger.error(
                    'codex_step_failed_binary_missing step=%s codex_bin=%s',
                    step_index,
                    self._settings.codex_bin,
                )
                attempt.failure_reason = 'binary_missing'
                attempt.failure_message = 'Команда codex не найдена в контейнере buyer. Проверьте CODEX_BIN.'
                return attempt

            try:
                attempt.stdout_text, attempt.stderr_text = await asyncio.wait_for(
                    _collect_process_streams(
                        process,
                        publisher=stream_publisher,
                        browser_actions_log_path=trace['browser_actions_log_path'],
                        browser_actions_offset=browser_actions_offset,
                    ),
                    timeout=self._settings.codex_timeout_sec,
                )
            except asyncio.TimeoutError:
                process.kill()
                await _communicate_quietly(process)
                await stream_publisher.aclose()
                attempt.duration_ms = _duration_ms_since(attempt.codex_started_at)
                attempt.failure_reason = 'timeout'
                attempt.failure_message = f'Команда codex превысила таймаут {self._settings.codex_timeout_sec} секунд.'
                logger.error(
                    'codex_step_timeout step=%s role=%s model=%s timeout_sec=%s duration_ms=%s',
                    step_index,
                    attempt_spec.role,
                    attempt_spec.model or 'default',
                    self._settings.codex_timeout_sec,
                    attempt.duration_ms,
                )
                return attempt

            attempt.codex_returncode = process.returncode
            attempt.duration_ms = _duration_ms_since(attempt.codex_started_at)
            logger.info(
                'codex_step_process_finished step=%s role=%s model=%s returncode=%s duration_ms=%s stdout_len=%s stderr_len=%s',
                step_index,
                attempt_spec.role,
                attempt_spec.model or 'default',
                attempt.codex_returncode,
                attempt.duration_ms,
                len(attempt.stdout_text),
                len(attempt.stderr_text),
            )

            if process.returncode != 0:
                attempt.failure_reason = 'process_failed'
                attempt.failure_message = _format_codex_failure_message(
                    returncode=process.returncode,
                    stderr_text=attempt.stderr_text,
                    stdout_text=attempt.stdout_text,
                )
                if attempt.stderr_text.strip():
                    logger.warning(
                        'codex_step_stderr_tail step=%s role=%s tail=%s',
                        step_index,
                        attempt_spec.role,
                        _tail_text(attempt.stderr_text, limit=1200),
                    )
                return attempt

            try:
                raw = Path(output_path).read_text(encoding='utf-8')
                parsed = json.loads(raw)
                attempt.result = AgentOutput.model_validate(parsed)
            except Exception as exc:  # noqa: BLE001 - нужно вернуть понятную причину в сессию
                logger.error(
                    'codex_step_failed_parse_output step=%s role=%s error=%s',
                    step_index,
                    attempt_spec.role,
                    _tail_text(str(exc), limit=500),
                )
                attempt.failure_reason = 'parse_output_failed'
                attempt.failure_message = f'Не удалось распарсить структурированный ответ codex: {exc}'
                return attempt

            return attempt
        finally:
            _remove_file_quietly(output_path)

    async def _reset_browser_to_start_url(self, *, start_url: str, actions_log_path: Path) -> tuple[bool, str]:
        cmd = [
            'python',
            '/app/tools/cdp_tool.py',
            '--endpoint',
            self._settings.browser_cdp_endpoint,
            '--timeout-ms',
            '12000',
            '--recovery-window-sec',
            str(self._settings.cdp_recovery_window_sec),
            '--recovery-interval-ms',
            str(self._settings.cdp_recovery_interval_ms),
            'goto',
            '--url',
            start_url,
        ]
        env = os.environ.copy()
        env['BUYER_CDP_ACTIONS_LOG_PATH'] = str(actions_log_path)
        env['BROWSER_CDP_ENDPOINT'] = self._settings.browser_cdp_endpoint
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._settings.codex_workdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            return False, _tail_text(f'Не удалось запустить reset CDP (`cdp_tool.py`): {exc}')

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return False, 'Reset browser to start_url превысил таймаут 30с.'

        stdout_text = stdout.decode('utf-8', errors='ignore').strip()
        stderr_text = stderr.decode('utf-8', errors='ignore').strip()
        if process.returncode != 0:
            return False, _extract_cdp_error_tail(stdout_text=stdout_text, stderr_text=stderr_text)
        return True, stdout_text or 'reset_ok'

    async def _probe_browser_sidecar(self, endpoint: str, *, actions_log_path: Path | None = None) -> tuple[bool, str]:
        window_sec = max(self._settings.cdp_recovery_window_sec, 0.0)
        interval_sec = max(self._settings.cdp_recovery_interval_ms, 1) / 1000.0
        deadline = asyncio.get_running_loop().time() + window_sec
        attempts = 0
        last_error_tail = 'none'

        while True:
            attempts += 1
            ok, probe_result, error_tail = await self._probe_browser_sidecar_once(endpoint, actions_log_path=actions_log_path)
            if ok:
                now = datetime.now(timezone.utc).isoformat()
                recovered_after_retry = attempts > 1
                return (
                    True,
                    ' ; '.join([
                        f'OK at {now}',
                        f'endpoint={endpoint}',
                        'command=url',
                        f'recovered_after_retry={str(recovered_after_retry).lower()}',
                        f'attempts={attempts}',
                        f'last_error_tail={last_error_tail}',
                        f'result={probe_result[:500]}',
                    ]),
                )

            last_error_tail = error_tail
            if asyncio.get_running_loop().time() >= deadline:
                return (
                    False,
                    'Preflight CDP не прошел: browser-sidecar недоступен до запуска сценария. '
                    f'Endpoint: `{endpoint}`. recovered_after_retry={str(attempts > 1).lower()}; '
                    f'attempts={attempts}; last_error_tail={last_error_tail}',
                )

            await asyncio.sleep(interval_sec)

    async def _probe_browser_sidecar_once(self, endpoint: str, *, actions_log_path: Path | None = None) -> tuple[bool, str, str]:
        cmd = [
            'python',
            '/app/tools/cdp_tool.py',
            '--endpoint',
            endpoint,
            '--timeout-ms',
            '12000',
            '--recovery-window-sec',
            '0',
            '--recovery-interval-ms',
            str(self._settings.cdp_recovery_interval_ms),
            'url',
        ]
        env = os.environ.copy()
        if actions_log_path is not None:
            env['BUYER_CDP_ACTIONS_LOG_PATH'] = str(actions_log_path)
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self._settings.codex_workdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            return False, '', _tail_text(f'Не удалось запустить preflight CDP (`cdp_tool.py`): {exc}')

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=25)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return (
                False,
                '',
                _tail_text(
                    'Preflight CDP превысил таймаут (25с). '
                    f'Не удалось подтвердить доступность browser-sidecar по endpoint `{endpoint}`.'
                ),
            )

        stdout_text = stdout.decode('utf-8', errors='ignore').strip()
        stderr_text = stderr.decode('utf-8', errors='ignore').strip()

        if process.returncode != 0:
            return False, '', _extract_cdp_error_tail(stdout_text=stdout_text, stderr_text=stderr_text)

        return True, stdout_text, 'none'

    def _prepare_trace_context(self, *, session_id: str, step_index: int) -> dict[str, Any]:
        trace_root = Path(self._settings.buyer_trace_dir).expanduser()
        session_dir = _find_existing_trace_session_dir(trace_root=trace_root, session_id=session_id)
        if session_dir is None:
            trace_date = _trace_date_dir_name()
            trace_time = _trace_time_dir_name()
            session_dir = trace_root / trace_date / trace_time / session_id
        else:
            trace_time = session_dir.parent.name
            trace_date = session_dir.parent.parent.name
        session_dir.mkdir(parents=True, exist_ok=True)
        step_tag = f'step-{step_index:03d}'
        return {
            'session_id': session_id,
            'step_index': step_index,
            'trace_date': trace_date,
            'trace_time': trace_time,
            'session_dir': session_dir,
            'step_dir': session_dir / step_tag,
            'prompt_path': session_dir / f'{step_tag}-prompt.txt',
            'browser_actions_log_path': session_dir / f'{step_tag}-browser-actions.jsonl',
            'step_trace_path': session_dir / f'{step_tag}-trace.json',
        }

    def _build_trace_artifacts(
        self,
        *,
        trace: dict[str, Any],
        preflight_summary: str,
        prompt_hash: str | None,
        prompt_preview: str | None,
        command_for_log: list[str] | None,
        output_path: str | None,
        stdout_text: str,
        stderr_text: str,
        codex_returncode: int | None,
        duration_ms: int | None,
        codex_started_at: datetime | None = None,
        codex_model: str | None = None,
        codex_attempts: list[dict[str, Any]] | None = None,
        model_strategy: str | None = None,
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        actions_total, actions_tail, actions_metrics = _read_browser_actions_log(
            trace['browser_actions_log_path'],
            limit=self._settings.buyer_browser_actions_tail,
        )
        codex_tokens_used = _extract_codex_tokens_used(stdout_text=stdout_text, stderr_text=stderr_text)
        post_browser_idle_ms = _build_post_browser_idle_ms(
            codex_started_at=codex_started_at,
            duration_ms=duration_ms,
            last_command_finished_ms=actions_metrics.get('last_command_finished_epoch_ms'),
        )
        payload: dict[str, Any] = {
            'session_id': trace['session_id'],
            'step': trace['step_index'],
            'trace_date': trace['trace_date'],
            'trace_time': trace['trace_time'],
            'preflight_summary': preflight_summary,
            'prompt_path': str(trace['prompt_path']) if trace['prompt_path'].is_file() else None,
            'prompt_sha256': prompt_hash,
            'prompt_preview': prompt_preview,
            'codex_command': command_for_log,
            'codex_output_path': output_path,
            'codex_returncode': codex_returncode,
            'codex_model': codex_model,
            'codex_tokens_used': codex_tokens_used,
            'model_strategy': model_strategy,
            'model_fallback_reason': fallback_reason,
            'codex_attempts': codex_attempts or [],
            'duration_ms': duration_ms,
            'post_browser_idle_ms': post_browser_idle_ms,
            'stdout_tail': _tail_text(stdout_text, limit=self._settings.buyer_stream_tail_chars),
            'stderr_tail': _tail_text(stderr_text, limit=self._settings.buyer_stream_tail_chars),
            'browser_actions_log_path': str(trace['browser_actions_log_path']),
            'browser_actions_total': actions_total,
            'browser_actions_tail': actions_tail,
            **actions_metrics,
        }
        _write_json_safely(trace['step_trace_path'], payload)
        return {
            'trace': _build_callback_trace_summary(
                trace=trace,
                prompt_hash=prompt_hash,
                codex_returncode=codex_returncode,
                duration_ms=duration_ms,
                codex_model=codex_model,
                codex_tokens_used=codex_tokens_used,
                codex_attempts=codex_attempts or [],
                model_strategy=model_strategy,
                fallback_reason=fallback_reason,
                browser_actions_total=actions_total,
            )
        }


def _find_existing_trace_session_dir(*, trace_root: Path, session_id: str) -> Path | None:
    if not trace_root.is_dir():
        return None

    matches: list[Path] = []
    try:
        date_dirs = [item for item in trace_root.iterdir() if item.is_dir()]
    except OSError:
        return None

    for date_dir in date_dirs:
        try:
            time_dirs = [item for item in date_dir.iterdir() if item.is_dir()]
        except OSError:
            continue
        for time_dir in time_dirs:
            candidate = time_dir / session_id
            if candidate.is_dir():
                matches.append(candidate)

    if not matches:
        return None
    return sorted(matches)[-1]


def _preview_text(text: str, *, limit: int) -> str:
    if limit <= 0:
        return ''
    compact = text.strip()
    if len(compact) <= limit:
        return compact
    return f'{compact[:limit]}...'


def _merge_artifacts(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    return {**base, **extra}


def _build_callback_trace_summary(
    *,
    trace: dict[str, Any],
    prompt_hash: str | None,
    codex_returncode: int | None,
    duration_ms: int | None,
    codex_model: str | None,
    codex_tokens_used: int | None,
    codex_attempts: list[dict[str, Any]],
    model_strategy: str | None,
    fallback_reason: str | None,
    browser_actions_total: int,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        'step': trace['step_index'],
        'trace_date': trace['trace_date'],
        'trace_time': trace['trace_time'],
        'prompt_sha256': prompt_hash,
        'trace_file': str(trace['step_trace_path']),
        'browser_actions_total': browser_actions_total,
        'duration_ms': duration_ms,
        'codex_returncode': codex_returncode,
        'codex_model': codex_model,
        'codex_tokens_used': codex_tokens_used,
        'model_strategy': model_strategy,
        'model_fallback_reason': fallback_reason,
        'codex_attempts': _slim_codex_attempts(codex_attempts),
    }
    return {key: value for key, value in summary.items() if value is not None}


def _slim_codex_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slim_attempts: list[dict[str, Any]] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        slim: dict[str, Any] = {}
        for field in ('role', 'model', 'status', 'failure_reason'):
            value = attempt.get(field)
            if value is not None:
                slim[field] = value
        if slim:
            slim_attempts.append(slim)
    return slim_attempts


def _read_jsonl_records(path: Path, *, limit: int) -> tuple[int, list[dict[str, Any]]]:
    total, items, _ = _read_browser_actions_log(path, limit=limit)
    return total, items


def _read_browser_actions_log(path: Path, *, limit: int) -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
    if not path.is_file():
        return 0, [], _empty_browser_actions_metrics()

    total = 0
    records: list[dict[str, Any]] = []
    try:
        for raw_line in path.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if not line:
                continue
            total += 1
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                parsed = {'event': 'json_parse_error', 'line_tail': _tail_text(line, limit=500)}
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                records.append({'event': 'json_non_object', 'value': parsed})
    except OSError:
        return 0, [], _empty_browser_actions_metrics()

    return total, records[-max(limit, 1) :], _build_browser_actions_metrics_from_records(records)


def _empty_browser_actions_metrics() -> dict[str, Any]:
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


def _build_browser_actions_metrics(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_browser_actions_metrics()

    try:
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        records = []

    return _build_browser_actions_metrics_from_records(records)


def _build_browser_actions_metrics_from_records(records: list[Any]) -> dict[str, Any]:
    starts_by_command: dict[str, list[dict[str, Any]]] = {}
    finished_commands: list[dict[str, Any]] = []
    breakdown: dict[str, dict[str, int]] = {}
    total_command_duration_ms = 0
    html_commands = 0
    html_bytes = 0

    for record in records:
        if not isinstance(record, dict):
            continue
        event = record.get('event')
        command = record.get('command')
        if not isinstance(command, str) or not command:
            continue

        if event == 'browser_command_started':
            starts_by_command.setdefault(command, []).append(record)
            continue

        if event in {'browser_command_finished', 'browser_command_failed'}:
            started_record = None
            queue = starts_by_command.get(command)
            if queue:
                started_record = queue.pop(0)

            duration_ms = _int_or_zero(record.get('duration_ms'))
            total_command_duration_ms += duration_ms
            command_stats = breakdown.setdefault(command, {'count': 0, 'duration_ms': 0, 'errors': 0})
            command_stats['count'] += 1
            command_stats['duration_ms'] += duration_ms
            failed = event == 'browser_command_failed' or not bool(record.get('ok'))
            if failed:
                command_stats['errors'] += 1

            result = record.get('result') if isinstance(record.get('result'), dict) else {}
            if command == 'html' and result:
                html_commands += 1
                size = _int_or_zero(result.get('html_size') or result.get('size'))
                html_bytes += size
                command_stats['html_bytes'] = command_stats.get('html_bytes', 0) + size

            started_ts = _parse_ts_ms(started_record.get('ts')) if isinstance(started_record, dict) else None
            finished_ts = _parse_ts_ms(record.get('ts'))
            if started_ts is None and finished_ts is not None and duration_ms > 0:
                started_ts = max(finished_ts - duration_ms, 0)
            if started_ts is not None and finished_ts is not None:
                finished_commands.append(
                    {
                        'started_ms': started_ts,
                        'finished_ms': finished_ts,
                        'duration_ms': duration_ms,
                        'command': command,
                        'failed': failed,
                    }
                )

    finished_commands.sort(key=lambda item: item['started_ms'])
    inter_command_idle_ms = 0
    browser_busy_union_ms = 0
    top_idle_gaps: list[dict[str, Any]] = []
    previous_finish_ms: int | None = None
    previous_command: str | None = None
    for command in finished_commands:
        started_ms = command['started_ms']
        finished_ms = command['finished_ms']
        if previous_finish_ms is not None and started_ms > previous_finish_ms:
            gap_ms = started_ms - previous_finish_ms
            inter_command_idle_ms += gap_ms
            top_idle_gaps.append(
                {
                    'duration_ms': gap_ms,
                    'from_epoch_ms': previous_finish_ms,
                    'to_epoch_ms': started_ms,
                    'after_command': previous_command,
                    'before_command': command['command'],
                }
            )
            browser_busy_union_ms += finished_ms - started_ms
        elif previous_finish_ms is None:
            browser_busy_union_ms += finished_ms - started_ms
        elif finished_ms > previous_finish_ms:
            browser_busy_union_ms += finished_ms - previous_finish_ms
        if previous_finish_ms is None or finished_ms >= previous_finish_ms:
            previous_command = command['command']
            previous_finish_ms = finished_ms

    return {
        'command_duration_ms': total_command_duration_ms,
        'inter_command_idle_ms': inter_command_idle_ms,
        'browser_busy_union_ms': browser_busy_union_ms,
        'top_idle_gaps': sorted(top_idle_gaps, key=lambda item: item['duration_ms'], reverse=True)[:5],
        'last_command_finished_epoch_ms': previous_finish_ms,
        'command_errors': sum(item.get('errors', 0) for item in breakdown.values()),
        'html_commands': html_commands,
        'html_bytes': html_bytes,
        'command_breakdown': breakdown,
    }


def _parse_ts_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = value.replace('Z', '+00:00')
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except ValueError:
        return None


def _int_or_zero(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _build_model_attempt_specs(settings: Settings) -> list[_CodexAttemptSpec]:
    if settings.buyer_model_strategy == 'fast_then_strong':
        fast_model = _non_empty(settings.buyer_fast_codex_model) or 'gpt-5.4-mini'
        strong_model = _non_empty(settings.buyer_strong_codex_model) or _non_empty(settings.codex_model) or 'gpt-5.5'
        if fast_model == strong_model:
            return [_CodexAttemptSpec(role='fast', model=fast_model)]
        return [
            _CodexAttemptSpec(role='fast', model=fast_model),
            _CodexAttemptSpec(role='strong', model=strong_model),
        ]
    return [_CodexAttemptSpec(role='single', model=_non_empty(settings.codex_model))]


def _build_codex_command(
    *,
    settings: Settings,
    schema_path: Path,
    output_path: str,
    prompt: str,
    model: str | None,
) -> list[str]:
    cmd = [
        settings.codex_bin,
        'exec',
        '--json',
        '-s',
        settings.codex_sandbox_mode,
    ]
    if settings.codex_skip_git_repo_check:
        cmd.append('--skip-git-repo-check')
    if model:
        cmd.extend(['-m', model])
    cmd.extend(_build_codex_config_overrides(settings))
    cmd.extend([
        '--output-schema',
        str(schema_path),
        '-o',
        output_path,
        prompt,
    ])
    return cmd


def _build_codex_config_overrides(settings: Settings) -> list[str]:
    overrides: list[tuple[str, str | bool | None]] = [
        ('model_reasoning_effort', settings.codex_reasoning_effort),
        ('model_reasoning_summary', settings.codex_reasoning_summary),
        ('web_search', settings.codex_web_search),
    ]
    overrides.append(('features.image_generation', settings.codex_image_generation == 'enabled'))
    cmd: list[str] = []
    for key, value in overrides:
        if value is not None:
            cmd.extend(['-c', f'{key}={_format_codex_config_value(value)}'])
    return cmd


def _format_codex_config_value(value: str | bool) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return f'"{value}"'


class _AgentStreamPublisher:
    def __init__(
        self,
        *,
        session_id: str,
        step_index: int,
        callback: AgentStreamCallback | None,
        batch_size: int = STREAM_BATCH_SIZE,
        batch_interval_sec: float = STREAM_BATCH_INTERVAL_SEC,
    ) -> None:
        self._session_id = session_id
        self._step_index = step_index
        self._callback = callback
        self._batch_size = batch_size
        self._batch_interval_sec = batch_interval_sec
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._pending_source: str | None = None
        self._pending_stream: str | None = None
        self._pending_items: list[dict[str, Any]] = []
        self._pending_message: str | None = None
        self._flush_task: asyncio.Task[None] | None = None

    async def publish(self, *, source: str, stream: str, item: dict[str, Any], message: str | None = None) -> None:
        if self._callback is None:
            async with self._lock:
                self._sequence += 1
                payload = {
                    'step': self._step_index,
                    'source': source,
                    'stream': stream,
                    'sequence': self._sequence,
                    'items': [item],
                    'message': message or _stream_item_message(item),
                }
            self._log_payload(payload)
            return

        payloads_to_send: list[dict[str, Any]] = []
        async with self._lock:
            if self._pending_items and (self._pending_source != source or self._pending_stream != stream):
                payloads_to_send.append(self._build_pending_payload_locked())
                self._clear_pending_locked(cancel_timer=True)

            if not self._pending_items:
                self._pending_source = source
                self._pending_stream = stream
                self._schedule_flush_locked()

            self._pending_items.append(item)
            self._pending_message = message or _stream_item_message(item)
            if len(self._pending_items) >= self._batch_size:
                payloads_to_send.append(self._build_pending_payload_locked())
                self._clear_pending_locked(cancel_timer=True)

        for payload in payloads_to_send:
            await self._send_payload(payload)

    async def aclose(self) -> None:
        payload: dict[str, Any] | None = None
        async with self._lock:
            if self._pending_items:
                payload = self._build_pending_payload_locked()
            self._clear_pending_locked(cancel_timer=True)
        if payload is not None:
            await self._send_payload(payload)

    def _build_pending_payload_locked(self) -> dict[str, Any]:
        self._sequence += 1
        return {
            'step': self._step_index,
            'source': self._pending_source or 'unknown',
            'stream': self._pending_stream or 'unknown',
            'sequence': self._sequence,
            'items': list(self._pending_items),
            'message': self._pending_message or f'{self._pending_source}/{self._pending_stream}',
        }

    def _clear_pending_locked(self, *, cancel_timer: bool) -> None:
        self._pending_source = None
        self._pending_stream = None
        self._pending_items = []
        self._pending_message = None
        if cancel_timer and self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

    def _schedule_flush_locked(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(self._batch_interval_sec)
            await self._flush_pending()
        except asyncio.CancelledError:
            return

    async def _flush_pending(self) -> None:
        payload: dict[str, Any] | None = None
        async with self._lock:
            if self._pending_items:
                payload = self._build_pending_payload_locked()
            self._clear_pending_locked(cancel_timer=False)
            self._flush_task = None
        if payload is not None:
            await self._send_payload(payload)

    async def _send_payload(self, payload: dict[str, Any]) -> None:
        self._log_payload(payload)
        if self._callback is not None:
            try:
                await self._callback(payload)
            except Exception as exc:  # noqa: BLE001 - stream callback не должен ломать шаг
                logger.warning(
                    'agent_stream_callback_failed session_id=%s step=%s source=%s stream=%s error=%s',
                    self._session_id,
                    payload.get('step'),
                    payload.get('source'),
                    payload.get('stream'),
                    _tail_text(str(exc), limit=500),
                )

    def _log_payload(self, payload: dict[str, Any]) -> None:
        logger.info(
            'agent_stream_event session_id=%s step=%s source=%s stream=%s sequence=%s items=%s',
            self._session_id,
            payload.get('step'),
            payload.get('source'),
            payload.get('stream'),
            payload.get('sequence'),
            json.dumps(payload.get('items', []), ensure_ascii=False, default=str),
        )


async def _collect_process_streams(
    process: Any,
    *,
    publisher: _AgentStreamPublisher,
    browser_actions_log_path: Path,
    browser_actions_offset: int,
) -> tuple[str, str]:
    if getattr(process, 'stdout', None) is None or getattr(process, 'stderr', None) is None or not hasattr(process, 'wait'):
        stdout, stderr = await process.communicate()
        await publisher.aclose()
        return stdout.decode('utf-8', errors='ignore'), stderr.decode('utf-8', errors='ignore')

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stop_browser_tail = asyncio.Event()
    tasks = [
        asyncio.create_task(_read_process_stream(process.stdout, source='codex', stream='stdout', chunks=stdout_chunks, publisher=publisher)),
        asyncio.create_task(_read_process_stream(process.stderr, source='codex', stream='stderr', chunks=stderr_chunks, publisher=publisher)),
        asyncio.create_task(
            _tail_browser_actions(
                browser_actions_log_path,
                initial_offset=browser_actions_offset,
                publisher=publisher,
                stop_event=stop_browser_tail,
            )
        ),
    ]
    try:
        await process.wait()
        stop_browser_tail.set()
        await asyncio.gather(*tasks)
        await publisher.aclose()
        return ''.join(stdout_chunks), ''.join(stderr_chunks)
    except Exception:
        stop_browser_tail.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await publisher.aclose()
        raise


async def _communicate_quietly(process: Any) -> None:
    try:
        if hasattr(process, 'communicate'):
            await process.communicate()
        elif hasattr(process, 'wait'):
            await process.wait()
    except Exception:  # noqa: BLE001 - cleanup best-effort after timeout
        return


async def _read_process_stream(
    reader: Any,
    *,
    source: str,
    stream: str,
    chunks: list[str],
    publisher: _AgentStreamPublisher,
) -> None:
    read = getattr(reader, 'read', None)
    if callable(read):
        decoder = codecs.getincrementaldecoder('utf-8')('ignore')
        pending_text = ''
        while True:
            raw = await read(PROCESS_STREAM_READ_CHUNK_SIZE)
            if not raw:
                tail = decoder.decode(b'', final=True)
                if tail:
                    chunks.append(tail)
                    pending_text += tail
                if pending_text:
                    await _publish_process_stream_text(
                        source=source,
                        stream=stream,
                        text=pending_text,
                        publisher=publisher,
                    )
                return

            text = decoder.decode(raw, final=False)
            if not text:
                continue
            chunks.append(text)
            pending_text += text
            while True:
                newline_index = pending_text.find('\n')
                if newline_index < 0:
                    break
                line = pending_text[: newline_index + 1]
                pending_text = pending_text[newline_index + 1 :]
                await _publish_process_stream_text(
                    source=source,
                    stream=stream,
                    text=line,
                    publisher=publisher,
                )
        return

    while True:
        raw = await reader.readline()
        if not raw:
            return
        text = raw.decode('utf-8', errors='ignore')
        chunks.append(text)
        await _publish_process_stream_text(
            source=source,
            stream=stream,
            text=text,
            publisher=publisher,
        )


async def _publish_process_stream_text(
    *,
    source: str,
    stream: str,
    text: str,
    publisher: _AgentStreamPublisher,
) -> None:
    for payload_stream, item, message in _normalize_process_stream_line(stream=stream, text=text):
        await publisher.publish(source=source, stream=payload_stream, item=item, message=message)


def _normalize_process_stream_line(*, stream: str, text: str) -> list[tuple[str, dict[str, Any], str]]:
    line = text.rstrip('\r\n')
    if not line:
        return []
    if stream == 'stdout':
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return [('stdout', {'line': _tail_text(line, limit=2000)}, _tail_text(line, limit=140))]
        if isinstance(parsed, dict):
            sanitized = _sanitize_stream_payload(parsed)
            return [('codex_json', sanitized, _stream_item_message(sanitized))]
        return [('stdout', {'value': parsed}, _tail_text(str(parsed), limit=140))]
    return [('stderr', {'line': _tail_text(line, limit=2000)}, _tail_text(line, limit=140))]


def _sanitize_stream_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize_stream_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_stream_payload(item) for item in value]
    if isinstance(value, str):
        return _truncate_stream_text(value)
    return value


def _truncate_stream_text(value: str) -> str:
    if len(value) <= STREAM_TEXT_MAX_CHARS:
        return value
    head = value[:STREAM_TEXT_HEAD_CHARS]
    tail = value[-STREAM_TEXT_TAIL_CHARS:]
    return f'{head}\n[truncated stream text: {len(value)} chars]\n{tail}'


async def _tail_browser_actions(
    path: Path,
    *,
    initial_offset: int,
    publisher: _AgentStreamPublisher,
    stop_event: asyncio.Event,
) -> None:
    offset = initial_offset
    while True:
        offset, records = _read_new_jsonl_records(path, offset=offset)
        for record in records:
            await publisher.publish(
                source='browser',
                stream='browser_actions',
                item=record,
                message=_stream_item_message(record),
            )
        if stop_event.is_set():
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.2)
        except asyncio.TimeoutError:
            continue


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_new_jsonl_records(path: Path, *, offset: int) -> tuple[int, list[dict[str, Any]]]:
    try:
        with path.open('rb') as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return offset, []
    if not data:
        return offset, []

    last_newline_index = data.rfind(b'\n')
    if last_newline_index < 0:
        return offset, []

    complete_data = data[: last_newline_index + 1]
    new_offset = offset + last_newline_index + 1

    records: list[dict[str, Any]] = []
    for raw_line in complete_data.decode('utf-8', errors='ignore').splitlines():
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            records.append({'event': 'json_parse_error', 'line_tail': _tail_text(raw_line, limit=500)})
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            records.append({'event': 'json_non_object', 'value': parsed})
    return new_offset, records


def _stream_item_message(item: dict[str, Any]) -> str:
    for key in ('message', 'event', 'type', 'command'):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return _tail_text(value.strip(), limit=160)
    return 'stream event'


def _browser_actions_have_mutating_commands(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return False
    for raw_line in lines:
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get('event') not in {'browser_command_started', 'browser_command_finished', 'browser_command_failed'}:
            continue
        command = record.get('command')
        if isinstance(command, str) and command in MUTATING_BROWSER_COMMANDS:
            return True
    return False


def _summarize_codex_attempt(attempt: _CodexAttemptResult) -> dict[str, Any]:
    return {
        'role': attempt.spec.role,
        'model': attempt.spec.model,
        'returncode': attempt.codex_returncode,
        'duration_ms': attempt.duration_ms,
        'status': attempt.result.status if attempt.result is not None else None,
        'failure_reason': attempt.failure_reason,
        'output_path': attempt.output_path,
        'tokens_used': _extract_codex_tokens_used(
            stdout_text=attempt.stdout_text,
            stderr_text=attempt.stderr_text,
        ),
    }


def _extract_codex_tokens_used(*, stdout_text: str, stderr_text: str) -> int | None:
    combined = f'{stdout_text}\n{stderr_text}'
    matches = re.findall(r'tokens\s+used\s+([0-9][0-9,\s]*)', combined, flags=re.IGNORECASE)
    if not matches:
        return None
    total = 0
    for raw in matches:
        compact = ''.join(ch for ch in raw if ch.isdigit())
        total += _int_or_zero(compact)
    return total


def _build_post_browser_idle_ms(
    *,
    codex_started_at: datetime | None,
    duration_ms: int | None,
    last_command_finished_ms: Any,
) -> int | None:
    if codex_started_at is None or duration_ms is None:
        return None
    last_finished = _int_or_zero(last_command_finished_ms)
    if last_finished <= 0:
        return None
    codex_started_ms = int(codex_started_at.timestamp() * 1000)
    codex_finished_ms = codex_started_ms + duration_ms
    if last_finished < codex_started_ms:
        return duration_ms
    return max(codex_finished_ms - last_finished, 0)


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _write_json_safely(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        return


def _format_codex_failure_message(*, returncode: int, stderr_text: str, stdout_text: str) -> str:
    combined = f'{stderr_text}\n{stdout_text}'
    compact_tail = (stderr_text or stdout_text).strip()
    if len(compact_tail) > 500:
        compact_tail = compact_tail[-500:]

    if '401 Unauthorized' in combined or 'Missing bearer or basic authentication' in combined:
        return (
            'codex не авторизован в контейнере buyer (401 Unauthorized). '
            'Проверьте OPENAI_API_KEY/OPENAI_BASE_URL в окружении сервиса buyer.'
        )

    if '429' in combined and 'rate' in combined.lower():
        return 'codex вернул rate-limit (429). Повторите запрос позже или уменьшите нагрузку.'

    if compact_tail:
        return f'codex завершился с кодом {returncode}: {compact_tail}'
    return f'codex завершился с кодом {returncode}.'


def _extract_cdp_error_tail(*, stdout_text: str, stderr_text: str) -> str:
    parsed_error: str | None = None
    if stdout_text:
        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            raw = payload.get('error')
            if isinstance(raw, str) and raw.strip():
                parsed_error = raw

    source = parsed_error or stderr_text or stdout_text or 'unknown error'
    return _tail_text(source)


def _build_redacted_auth_payload(auth: TaskAuthPayload | None) -> dict[str, Any] | None:
    if auth is None:
        return None

    storage_state = auth.storage_state if isinstance(auth.storage_state, dict) else None
    cookies = storage_state.get('cookies') if storage_state is not None else None
    origins = storage_state.get('origins') if storage_state is not None else None

    return {
        'provider': (auth.provider or '').strip().lower() or 'sberid',
        'has_storage_state': storage_state is not None,
        'storage_state_stats': {
            'cookies_count': len(cookies) if isinstance(cookies, list) else 0,
            'origins_count': len(origins) if isinstance(origins, list) else 0,
        },
    }


def _build_agent_auth_state(
    *,
    auth: TaskAuthPayload | None,
    auth_context: dict[str, Any] | None,
) -> dict[str, Any]:
    auth_context_dict = auth_context if isinstance(auth_context, dict) else {}
    provider = (auth.provider if auth is not None else '') or str(auth_context_dict.get('provider') or '')
    reason_code = auth_context_dict.get('reason_code')
    safe_summary: dict[str, Any] = {}
    for key in ('source', 'mode', 'path', 'reason_code', 'attempts', 'script_status', 'context_prepared'):
        value = auth_context_dict.get(key)
        if isinstance(value, (str, int, bool)) or value is None:
            safe_summary[key] = value
    domain = auth_context_dict.get('domain')
    if isinstance(domain, str):
        safe_summary['domain'] = domain

    return {
        'provided': auth is not None,
        'provider': provider.strip().lower() or None,
        'authenticated': reason_code == 'auth_ok',
        'summary': safe_summary,
    }
