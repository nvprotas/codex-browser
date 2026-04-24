from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AgentOutput, TaskAuthPayload
from .prompt_builder import build_agent_prompt
from .settings import Settings

logger = logging.getLogger('uvicorn.error')


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

        prompt = build_agent_prompt(
            task=task,
            start_url=start_url,
            browser_cdp_endpoint=self._settings.browser_cdp_endpoint,
            cdp_preflight_summary=probe_summary,
            metadata=metadata,
            auth_payload=_build_redacted_auth_payload(auth),
            auth_context=auth_context,
            memory=memory,
            latest_user_reply=latest_user_reply,
        )
        trace['prompt_path'].write_text(prompt, encoding='utf-8')
        prompt_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        prompt_preview = _preview_text(prompt, limit=self._settings.buyer_prompt_preview_chars)

        with tempfile.NamedTemporaryFile(
            prefix=f'codex-result-step-{step_index:03d}-',
            suffix='.json',
            dir=trace['session_dir'],
            delete=False,
        ) as output_file:
            output_path = output_file.name

        cmd = [
            self._settings.codex_bin,
            'exec',
            '-s',
            self._settings.codex_sandbox_mode,
        ]
        if self._settings.codex_skip_git_repo_check:
            cmd.append('--skip-git-repo-check')
        if self._settings.codex_model:
            cmd.extend(['-m', self._settings.codex_model])
        cmd.extend([
            '--output-schema',
            str(self._schema_path),
            '-o',
            output_path,
            prompt,
        ])
        command_for_log = [*cmd[:-1], f'@{trace["prompt_path"]}']
        logger.info(
            'codex_step_exec session_id=%s step=%s prompt_path=%s model=%s sandbox=%s',
            session_id,
            step_index,
            trace['prompt_path'],
            self._settings.codex_model or 'default',
            self._settings.codex_sandbox_mode,
        )

        env = os.environ.copy()
        env['BROWSER_CDP_ENDPOINT'] = self._settings.browser_cdp_endpoint
        env['CDP_RECOVERY_WINDOW_SEC'] = str(self._settings.cdp_recovery_window_sec)
        env['CDP_RECOVERY_INTERVAL_MS'] = str(self._settings.cdp_recovery_interval_ms)
        env['BUYER_CDP_ACTIONS_LOG_PATH'] = str(trace['browser_actions_log_path'])

        stdout_text = ''
        stderr_text = ''
        codex_returncode: int | None = None
        duration_ms: int | None = None
        started_at = datetime.now(timezone.utc)

        try:
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
                        command_for_log=command_for_log,
                        output_path=output_path,
                        stdout_text='',
                        stderr_text='',
                        codex_returncode=None,
                        duration_ms=None,
                    ),
                )

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
                    'codex_step_failed_binary_missing session_id=%s step=%s codex_bin=%s',
                    session_id,
                    step_index,
                    self._settings.codex_bin,
                )
                return AgentOutput(
                    status='failed',
                    message='Команда codex не найдена в контейнере buyer. Проверьте CODEX_BIN.',
                    order_id=None,
                    artifacts=self._build_trace_artifacts(
                        trace=trace,
                        preflight_summary=probe_summary,
                        prompt_hash=prompt_hash,
                        prompt_preview=prompt_preview,
                        command_for_log=command_for_log,
                        output_path=output_path,
                        stdout_text='',
                        stderr_text='',
                        codex_returncode=None,
                        duration_ms=None,
                    ),
                )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self._settings.codex_timeout_sec)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                duration_ms = _duration_ms_since(started_at)
                logger.error(
                    'codex_step_timeout session_id=%s step=%s timeout_sec=%s duration_ms=%s',
                    session_id,
                    step_index,
                    self._settings.codex_timeout_sec,
                    duration_ms,
                )
                return AgentOutput(
                    status='failed',
                    message=f'Команда codex превысила таймаут {self._settings.codex_timeout_sec} секунд.',
                    order_id=None,
                    artifacts=self._build_trace_artifacts(
                        trace=trace,
                        preflight_summary=probe_summary,
                        prompt_hash=prompt_hash,
                        prompt_preview=prompt_preview,
                        command_for_log=command_for_log,
                        output_path=output_path,
                        stdout_text='',
                        stderr_text='',
                        codex_returncode=None,
                        duration_ms=duration_ms,
                    ),
                )
            finally:
                stdout_text = stdout.decode('utf-8', errors='ignore') if 'stdout' in locals() else ''
                stderr_text = stderr.decode('utf-8', errors='ignore') if 'stderr' in locals() else ''

            codex_returncode = process.returncode
            duration_ms = _duration_ms_since(started_at)
            logger.info(
                'codex_step_process_finished session_id=%s step=%s returncode=%s duration_ms=%s stdout_len=%s stderr_len=%s',
                session_id,
                step_index,
                codex_returncode,
                duration_ms,
                len(stdout_text),
                len(stderr_text),
            )

            if process.returncode != 0:
                message = _format_codex_failure_message(
                    returncode=process.returncode,
                    stderr_text=stderr_text,
                    stdout_text=stdout_text,
                )
                if stderr_text.strip():
                    logger.warning(
                        'codex_step_stderr_tail session_id=%s step=%s tail=%s',
                        session_id,
                        step_index,
                        _tail_text(stderr_text, limit=1200),
                    )
                return AgentOutput(
                    status='failed',
                    message=message,
                    order_id=None,
                    artifacts=self._build_trace_artifacts(
                        trace=trace,
                        preflight_summary=probe_summary,
                        prompt_hash=prompt_hash,
                        prompt_preview=prompt_preview,
                        command_for_log=command_for_log,
                        output_path=output_path,
                        stdout_text=stdout_text,
                        stderr_text=stderr_text,
                        codex_returncode=codex_returncode,
                        duration_ms=duration_ms,
                    ),
                )

            try:
                raw = Path(output_path).read_text(encoding='utf-8')
                parsed = json.loads(raw)
                result = AgentOutput.model_validate(parsed)
            except Exception as exc:  # noqa: BLE001 - нужно вернуть понятную причину в сессию
                logger.error(
                    'codex_step_failed_parse_output session_id=%s step=%s error=%s',
                    session_id,
                    step_index,
                    _tail_text(str(exc), limit=500),
                )
                return AgentOutput(
                    status='failed',
                    message=f'Не удалось распарсить структурированный ответ codex: {exc}',
                    order_id=None,
                    artifacts=self._build_trace_artifacts(
                        trace=trace,
                        preflight_summary=probe_summary,
                        prompt_hash=prompt_hash,
                        prompt_preview=prompt_preview,
                        command_for_log=command_for_log,
                        output_path=output_path,
                        stdout_text=stdout_text,
                        stderr_text=stderr_text,
                        codex_returncode=codex_returncode,
                        duration_ms=duration_ms,
                    ),
                )

            normalized = result.status.strip().lower()
            if normalized not in {'needs_user_input', 'completed', 'failed'}:
                logger.error(
                    'codex_step_invalid_status session_id=%s step=%s status=%s',
                    session_id,
                    step_index,
                    result.status,
                )
                return AgentOutput(
                    status='failed',
                    message=f'codex вернул неподдерживаемый статус: {result.status}',
                    order_id=None,
                    artifacts=_merge_artifacts(
                        result.artifacts,
                        self._build_trace_artifacts(
                            trace=trace,
                            preflight_summary=probe_summary,
                            prompt_hash=prompt_hash,
                            prompt_preview=prompt_preview,
                            command_for_log=command_for_log,
                            output_path=output_path,
                            stdout_text=stdout_text,
                            stderr_text=stderr_text,
                            codex_returncode=codex_returncode,
                            duration_ms=duration_ms,
                        ),
                    ),
                )

            result.status = normalized
            result.artifacts = _merge_artifacts(
                result.artifacts,
                self._build_trace_artifacts(
                    trace=trace,
                    preflight_summary=probe_summary,
                    prompt_hash=prompt_hash,
                    prompt_preview=prompt_preview,
                    command_for_log=command_for_log,
                    output_path=output_path,
                    stdout_text=stdout_text,
                    stderr_text=stderr_text,
                    codex_returncode=codex_returncode,
                    duration_ms=duration_ms,
                ),
            )
            logger.info(
                'codex_step_result session_id=%s step=%s status=%s order_id=%s trace_file=%s',
                session_id,
                step_index,
                result.status,
                result.order_id,
                (
                    result.artifacts.get('trace', {}).get('trace_file')
                    if isinstance(result.artifacts.get('trace'), dict)
                    else None
                ),
            )
            return result
        finally:
            _remove_file_quietly(output_path)

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
    ) -> dict[str, Any]:
        actions_total, actions_tail = _read_jsonl_records(
            trace['browser_actions_log_path'],
            limit=self._settings.buyer_browser_actions_tail,
        )
        actions_metrics = _build_browser_actions_metrics(trace['browser_actions_log_path'])
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
            'duration_ms': duration_ms,
            'stdout_tail': _tail_text(stdout_text, limit=self._settings.buyer_stream_tail_chars),
            'stderr_tail': _tail_text(stderr_text, limit=self._settings.buyer_stream_tail_chars),
            'browser_actions_log_path': str(trace['browser_actions_log_path']),
            'browser_actions_total': actions_total,
            'browser_actions_tail': actions_tail,
            **actions_metrics,
        }
        _write_json_safely(trace['step_trace_path'], payload)
        payload['trace_file'] = str(trace['step_trace_path'])
        return {'trace': payload}


def _duration_ms_since(started_at: datetime) -> int:
    delta = datetime.now(timezone.utc) - started_at
    return max(int(delta.total_seconds() * 1000), 0)


def _trace_date_dir_name(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime('%Y-%m-%d')


def _trace_time_dir_name(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.strftime('%H-%M-%S')


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
    merged: dict[str, Any] = {}
    merged.update(base)
    merged.update(extra)
    return merged


def _read_jsonl_records(path: Path, *, limit: int) -> tuple[int, list[dict[str, Any]]]:
    if not path.is_file():
        return 0, []

    total = 0
    items: list[dict[str, Any]] = []
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
                items.append(parsed)
            else:
                items.append({'event': 'json_non_object', 'value': parsed})
    except OSError:
        return 0, []

    return total, items[-max(limit, 1) :]


def _build_browser_actions_metrics(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            'command_duration_ms': 0,
            'inter_command_idle_ms': 0,
            'html_commands': 0,
            'html_bytes': 0,
            'command_breakdown': {},
        }

    starts_by_command: dict[str, list[dict[str, Any]]] = {}
    finished_commands: list[dict[str, Any]] = []
    breakdown: dict[str, dict[str, int]] = {}
    total_command_duration_ms = 0
    html_commands = 0
    html_bytes = 0

    try:
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        records = []

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

        if event == 'browser_command_finished':
            started_record = None
            queue = starts_by_command.get(command)
            if queue:
                started_record = queue.pop(0)

            duration_ms = _int_or_zero(record.get('duration_ms'))
            total_command_duration_ms += duration_ms
            command_stats = breakdown.setdefault(command, {'count': 0, 'duration_ms': 0, 'errors': 0})
            command_stats['count'] += 1
            command_stats['duration_ms'] += duration_ms
            if not bool(record.get('ok')):
                command_stats['errors'] += 1

            result = record.get('result') if isinstance(record.get('result'), dict) else {}
            if command == 'html':
                html_commands += 1
                size = _int_or_zero(result.get('html_size') or result.get('size'))
                html_bytes += size
                command_stats['html_bytes'] = command_stats.get('html_bytes', 0) + size

            started_ts = _parse_ts_ms(started_record.get('ts')) if isinstance(started_record, dict) else None
            finished_ts = _parse_ts_ms(record.get('ts'))
            if started_ts is not None and finished_ts is not None:
                finished_commands.append(
                    {
                        'started_ms': started_ts,
                        'finished_ms': finished_ts,
                        'duration_ms': duration_ms,
                    }
                )

    finished_commands.sort(key=lambda item: item['started_ms'])
    inter_command_idle_ms = 0
    previous_finish_ms: int | None = None
    for command in finished_commands:
        started_ms = command['started_ms']
        finished_ms = command['finished_ms']
        if previous_finish_ms is not None and started_ms > previous_finish_ms:
            inter_command_idle_ms += started_ms - previous_finish_ms
        previous_finish_ms = finished_ms if previous_finish_ms is None else max(previous_finish_ms, finished_ms)

    return {
        'command_duration_ms': total_command_duration_ms,
        'inter_command_idle_ms': inter_command_idle_ms,
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


def _write_json_safely(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        return


def _remove_file_quietly(path: str) -> None:
    try:
        os.remove(path)
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


def _tail_text(text: str, limit: int = 500) -> str:
    compact = ' '.join(text.replace('\n', ' ').split())
    if len(compact) <= limit:
        return compact
    return compact[-limit:]


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
