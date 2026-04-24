from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger('uvicorn.error')


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


@dataclass(frozen=True)
class ScriptSpec:
    domain: str
    lifecycle: str
    relative_path: str


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
            'kuper.ru': ScriptSpec(domain='kuper.ru', lifecycle='draft', relative_path='sberid/kuper.ts'),
            'samokat.ru': ScriptSpec(domain='samokat.ru', lifecycle='draft', relative_path='sberid/samokat.ts'),
            'okko.tv': ScriptSpec(domain='okko.tv', lifecycle='draft', relative_path='sberid/okko.ts'),
        }

    def registry_snapshot(self) -> list[dict[str, str]]:
        return [
            {
                'domain': spec.domain,
                'lifecycle': spec.lifecycle,
                'script': spec.relative_path,
            }
            for spec in sorted(self._registry.values(), key=lambda item: item.domain)
        ]

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

        session_dir = self._trace_dir.expanduser() / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        storage_path = session_dir / f'auth-storage-attempt-{attempt:02d}.json'
        output_path = session_dir / f'auth-script-result-attempt-{attempt:02d}.json'
        storage_path.write_text(json.dumps(storage_state, ensure_ascii=False), encoding='utf-8')

        try:
            resolved_endpoint = await resolve_cdp_endpoint(self._cdp_endpoint)
        except Exception as exc:
            logger.error(
                'auth_script_endpoint_resolve_failed session_id=%s domain=%s attempt=%s endpoint=%s error=%s',
                session_id,
                normalized_domain,
                attempt,
                self._cdp_endpoint,
                _tail_text(str(exc), limit=700),
            )
            return AuthScriptResult(
                status='failed',
                reason_code=AUTH_FAILED_INVALID_SESSION,
                message='Не удалось резолвить CDP endpoint для auth-скрипта.',
                artifacts={
                    'domain': normalized_domain,
                    'script': str(script_path),
                    'cdp_endpoint': self._cdp_endpoint,
                    'cdp_resolve_error': _tail_text(str(exc), limit=900),
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
            'auth_script_started session_id=%s domain=%s attempt=%s script=%s lifecycle=%s',
            session_id,
            normalized_domain,
            attempt,
            script_path,
            spec.lifecycle,
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
        parsed_payload: dict[str, Any] | None = None

        if output_path.is_file():
            try:
                parsed_payload = json.loads(output_path.read_text(encoding='utf-8'))
            except Exception:
                parsed_payload = None
        if parsed_payload is None and stdout_text:
            try:
                parsed_payload = json.loads(stdout_text)
            except Exception:
                parsed_payload = None

        if process.returncode != 0 and parsed_payload is None:
            return AuthScriptResult(
                status='failed',
                reason_code=AUTH_FAILED_INVALID_SESSION,
                message=(
                    f'SberId-скрипт завершился с кодом {process.returncode}. '
                    f'stderr: {_tail_text(stderr_text)}'
                ),
                artifacts={
                    'domain': normalized_domain,
                    'script': str(script_path),
                    'cdp_endpoint': self._cdp_endpoint,
                    'resolved_cdp_endpoint': resolved_endpoint,
                    'stdout_tail': _tail_text(stdout_text),
                    'stderr_tail': _tail_text(stderr_text),
                },
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
                    'stdout_tail': _tail_text(stdout_text),
                    'stderr_tail': _tail_text(stderr_text),
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
                'stdout_tail': _tail_text(stdout_text),
                'stderr_tail': _tail_text(stderr_text),
            }
        )
        return AuthScriptResult(
            status=status or 'failed',
            reason_code=reason_code,
            message=message,
            artifacts=artifacts,
        )


def _build_endpoint_candidates(endpoint: str) -> list[str]:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {'http', 'https'}:
        return [endpoint]

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == 'https' else 80

    candidates: list[str] = []

    def add_candidate(raw: str) -> None:
        if raw not in candidates:
            candidates.append(raw)

    add_candidate(endpoint)

    if parsed.hostname not in {'localhost', '127.0.0.1'}:
        add_candidate(f'{parsed.scheme}://localhost:{port}')
        add_candidate(f'{parsed.scheme}://127.0.0.1:{port}')

    if parsed.hostname != 'host.docker.internal':
        add_candidate(f'{parsed.scheme}://host.docker.internal:{port}')

    return candidates


async def _resolve_single_http_endpoint(endpoint: str, *, client: httpx.AsyncClient) -> str:
    parsed = urlparse(endpoint)
    host_header = f'localhost:{parsed.port}' if parsed.port else 'localhost'
    version_url = endpoint.rstrip('/') + '/json/version'
    response = await client.get(version_url, headers={'Host': host_header})
    response.raise_for_status()
    payload = response.json()

    raw_ws = payload.get('webSocketDebuggerUrl')
    if not isinstance(raw_ws, str) or not raw_ws:
        raise RuntimeError('CDP endpoint не вернул webSocketDebuggerUrl.')

    ws_parsed = urlparse(raw_ws)
    return urlunparse((ws_parsed.scheme, parsed.netloc, ws_parsed.path, ws_parsed.params, ws_parsed.query, ws_parsed.fragment))


async def resolve_cdp_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme in {'ws', 'wss'}:
        return endpoint

    if parsed.scheme not in {'http', 'https'}:
        return endpoint

    candidates = _build_endpoint_candidates(endpoint)
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for candidate in candidates:
            try:
                return await _resolve_single_http_endpoint(candidate, client=client)
            except Exception as exc:  # noqa: BLE001
                failures.append(f'{candidate}: {exc}')

    details = '; '.join(failures[:4])
    raise RuntimeError(
        'Не удалось подключиться к browser-sidecar ни по одному CDP endpoint. '
        f'Пробовали: {", ".join(candidates)}. Ошибки: {details}'
    )


def _tail_text(text: str, limit: int = 500) -> str:
    compact = ' '.join((text or '').replace('\n', ' ').split())
    if len(compact) <= limit:
        return compact
    return compact[-limit:]
