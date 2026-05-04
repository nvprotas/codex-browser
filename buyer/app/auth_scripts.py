from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cdp_endpoint import resolve_cdp_endpoint
from .script_runtime import (
    ScriptSpec,
    read_script_result_payload,
    registry_snapshot,
    remove_script_output,
    script_stdio_artifacts,
    unique_script_output_path,
)
from ._utils import remove_file_quietly, tail_text
from .trace_session import resolve_trace_session_dir

logger = logging.getLogger(__name__)


AUTH_OK = 'auth_ok'
AUTH_FAILED_PAYLOAD = 'auth_failed_payload'
AUTH_FAILED_REDIRECT_LOOP = 'auth_failed_redirect_loop'
AUTH_FAILED_INVALID_SESSION = 'auth_failed_invalid_session'
AUTH_REFRESH_REQUESTED = 'auth_refresh_requested'


def normalize_domain(value: str) -> str:
    text = (value or '').strip().lower()
    if text.startswith('www.'):
        return text[4:]
    return text


def domain_from_url(raw_url: str) -> str:
    try:
        host = urlparse(raw_url).hostname or ''
    except Exception:
        return ''
    return normalize_domain(host)


def is_domain_in_allowlist(domain: str, allowlist: set[str]) -> bool:
    target = normalize_domain(domain)
    if not target:
        return False
    if target in allowlist:
        return True
    return any(target.endswith(f'.{item}') for item in allowlist)


def parse_allowlist(raw: str) -> set[str]:
    return {normalize_domain(item) for item in raw.split(',') if normalize_domain(item)}


@dataclass
class AuthScriptResult:
    status: str
    reason_code: str
    message: str
    artifacts: dict[str, Any]


class SberIdScriptRunner:
    def __init__(
        self,
        *,
        scripts_dir: str,
        cdp_endpoint: str,
        timeout_sec: int,
        trace_dir: str,
    ) -> None:
        self._scripts_dir = Path(scripts_dir)
        self._tsx_bin = self._scripts_dir / 'node_modules' / '.bin' / 'tsx'
        self._cdp_endpoint = cdp_endpoint
        self._timeout_sec = max(timeout_sec, 5)
        self._trace_dir = Path(trace_dir)
        self._registry: dict[str, ScriptSpec] = {
            'brandshop.ru': ScriptSpec(domain='brandshop.ru', lifecycle='publish', relative_path='sberid/brandshop.ts'),
            'litres.ru': ScriptSpec(domain='litres.ru', lifecycle='publish', relative_path='sberid/litres.ts'),
        }

    def registry_snapshot(self) -> list[dict[str, str]]:
        return registry_snapshot(self._registry)

    async def run(
        self,
        *,
        session_id: str,
        domain: str,
        start_url: str,
        storage_state: dict[str, Any],
        attempt: int,
    ) -> AuthScriptResult:
        normalized_domain = normalize_domain(domain)
        spec = self._registry.get(normalized_domain)
        if spec is None:
            return AuthScriptResult(
                status='failed',
                reason_code=AUTH_FAILED_INVALID_SESSION,
                message=f'Для домена {normalized_domain or "<empty>"} нет зарегистрированного SberId-скрипта.',
                artifacts={'domain': normalized_domain, 'script_registry': self.registry_snapshot()},
            )

        script_path = self._scripts_dir / spec.relative_path
        if spec.lifecycle != 'publish':
            return AuthScriptResult(
                status='failed',
                reason_code=AUTH_REFRESH_REQUESTED,
                message=(
                    f'SberId-скрипт для {normalized_domain} пока в lifecycle={spec.lifecycle}. '
                    'Нужен новый auth-пакет или fallback.'
                ),
                artifacts={
                    'domain': normalized_domain,
                    'script': str(script_path),
                    'lifecycle': spec.lifecycle,
                },
            )
        if not script_path.is_file():
            return AuthScriptResult(
                status='failed',
                reason_code=AUTH_FAILED_INVALID_SESSION,
                message=f'Файл скрипта {script_path} не найден.',
                artifacts={
                    'domain': normalized_domain,
                    'script': str(script_path),
                    'lifecycle': spec.lifecycle,
                },
            )
        if not self._tsx_bin.is_file():
            return AuthScriptResult(
                status='failed',
                reason_code=AUTH_FAILED_INVALID_SESSION,
                message=f'Локальный TSX рантайм не найден: {self._tsx_bin}',
                artifacts={
                    'domain': normalized_domain,
                    'script': str(script_path),
                    'tsx_bin': str(self._tsx_bin),
                },
            )

        trace_root = self._trace_dir.expanduser()
        session_dir = _auth_trace_session_dir(trace_root=trace_root, session_id=session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        remove_script_output(session_dir / 'auth-script-result.json')
        remove_script_output(session_dir / f'auth-script-result-attempt-{attempt:02d}.json')
        output_path = unique_script_output_path(session_dir, f'auth-script-result-attempt-{attempt:02d}')
        remove_script_output(output_path)
        storage_path = _write_storage_state_tempfile(storage_state, session_id=session_id, attempt=attempt)

        try:
            try:
                resolved_endpoint = await resolve_cdp_endpoint(self._cdp_endpoint)
            except Exception as exc:
                logger.error(
                    'auth_script_endpoint_resolve_failed session_id=%s domain=%s attempt=%s endpoint=%s error=%s',
                    session_id,
                    normalized_domain,
                    attempt,
                    self._cdp_endpoint,
                    tail_text(str(exc), limit=700),
                )
                return AuthScriptResult(
                    status='failed',
                    reason_code=AUTH_FAILED_INVALID_SESSION,
                    message='Не удалось резолвить CDP endpoint для auth-скрипта.',
                    artifacts={
                        'domain': normalized_domain,
                        'script': str(script_path),
                        'cdp_endpoint': self._cdp_endpoint,
                        'cdp_resolve_error': tail_text(str(exc), limit=900),
                    },
                )

            cmd = [
                str(self._tsx_bin),
                str(script_path),
                '--endpoint',
                resolved_endpoint,
                '--start-url',
                start_url,
                '--storage-state-path',
                str(storage_path),
                '--output-path',
                str(output_path),
            ]
            logger.info(
                'auth_script_endpoint_resolved session_id=%s domain=%s attempt=%s endpoint=%s resolved=%s',
                session_id,
                normalized_domain,
                attempt,
                self._cdp_endpoint,
                resolved_endpoint,
            )
            logger.info(
                'auth_script_started session_id=%s domain=%s attempt=%s script=%s lifecycle=%s trace_dir=%s',
                session_id,
                normalized_domain,
                attempt,
                script_path,
                spec.lifecycle,
                session_dir,
            )

            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                return AuthScriptResult(
                    status='failed',
                    reason_code=AUTH_FAILED_INVALID_SESSION,
                    message='Node.js не найден в контейнере buyer, запуск auth-скрипта невозможен.',
                    artifacts={'domain': normalized_domain, 'script': str(script_path)},
                )

            try:
                stdout_raw, stderr_raw = await asyncio.wait_for(process.communicate(), timeout=self._timeout_sec)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                return AuthScriptResult(
                    status='failed',
                    reason_code=AUTH_FAILED_INVALID_SESSION,
                    message=f'SberId-скрипт превысил таймаут {self._timeout_sec}с.',
                    artifacts={
                        'domain': normalized_domain,
                        'script': str(script_path),
                        'cdp_endpoint': self._cdp_endpoint,
                        'resolved_cdp_endpoint': resolved_endpoint,
                        'timeout_sec': self._timeout_sec,
                    },
                )

            stdout_text = stdout_raw.decode('utf-8', errors='ignore').strip()
            stderr_text = stderr_raw.decode('utf-8', errors='ignore').strip()
            if stderr_text:
                logger.warning(
                    'auth_script_stderr session_id=%s domain=%s attempt=%s stderr=%s',
                    session_id,
                    normalized_domain,
                    attempt,
                    tail_text(stderr_text, limit=1200),
                )
            parsed_payload = read_script_result_payload(output_path, stdout_text)
            stdio_artifacts = script_stdio_artifacts(stdout_text, stderr_text)

            if process.returncode != 0:
                artifacts: dict[str, Any] = {
                    'domain': normalized_domain,
                    'script': str(script_path),
                    'cdp_endpoint': self._cdp_endpoint,
                    'resolved_cdp_endpoint': resolved_endpoint,
                    'returncode': process.returncode,
                    **stdio_artifacts,
                }
                if parsed_payload is not None:
                    artifacts['script_result_payload'] = parsed_payload
                return AuthScriptResult(
                    status='failed',
                    reason_code=AUTH_FAILED_INVALID_SESSION,
                    message=(
                        f'SberId-скрипт завершился с кодом {process.returncode}. '
                        f'stderr: {tail_text(stderr_text)}'
                    ),
                    artifacts=artifacts,
                )

            if not isinstance(parsed_payload, dict):
                return AuthScriptResult(
                    status='failed',
                    reason_code=AUTH_FAILED_INVALID_SESSION,
                    message='SberId-скрипт не вернул валидный JSON-результат.',
                    artifacts={
                        'domain': normalized_domain,
                        'script': str(script_path),
                        'cdp_endpoint': self._cdp_endpoint,
                        'resolved_cdp_endpoint': resolved_endpoint,
                        **stdio_artifacts,
                    },
                )

            status = str(parsed_payload.get('status') or '').strip().lower()
            reason_code = str(parsed_payload.get('reason_code') or AUTH_FAILED_INVALID_SESSION).strip()
            message = str(parsed_payload.get('message') or 'SberId-скрипт завершился без сообщения.')
            artifacts = parsed_payload.get('artifacts')
            if not isinstance(artifacts, dict):
                artifacts = {}
            artifacts.update(
                {
                    'domain': normalized_domain,
                    'script': str(script_path),
                    'lifecycle': spec.lifecycle,
                    'cdp_endpoint': self._cdp_endpoint,
                    'resolved_cdp_endpoint': resolved_endpoint,
                    **stdio_artifacts,
                }
            )
            return AuthScriptResult(
                status=status or 'failed',
                reason_code=reason_code,
                message=message,
                artifacts=artifacts,
            )
        finally:
            remove_file_quietly(str(storage_path))


def _auth_trace_session_dir(*, trace_root: Path, session_id: str) -> Path:
    _, _, session_dir = resolve_trace_session_dir(trace_root=trace_root, session_id=session_id)
    return session_dir


def _write_storage_state_tempfile(storage_state: dict[str, Any], *, session_id: str, attempt: int) -> Path:
    safe_session = ''.join(ch if ch.isalnum() or ch in {'-', '_'} else '-' for ch in session_id)[:80] or 'session'
    with tempfile.NamedTemporaryFile(
        mode='w',
        encoding='utf-8',
        prefix=f'buyer-auth-storage-{safe_session}-{attempt:02d}-',
        suffix='.json',
        delete=False,
    ) as handle:
        storage_path = Path(handle.name)
        os.chmod(storage_path, 0o600)
        json.dump(storage_state, handle, ensure_ascii=False)
    return storage_path
