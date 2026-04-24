from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth_scripts import normalize_domain, resolve_cdp_endpoint

logger = logging.getLogger('uvicorn.error')

PURCHASE_SCRIPT_COMPLETED = 'completed'
PURCHASE_SCRIPT_FAILED = 'failed'


@dataclass(frozen=True)
class PurchaseScriptSpec:
    domain: str
    lifecycle: str
    relative_path: str


@dataclass
class PurchaseScriptResult:
    status: str
    reason_code: str
    message: str
    order_id: str | None
    artifacts: dict[str, Any]


class PurchaseScriptRunner:
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
        self._registry: dict[str, PurchaseScriptSpec] = {
            'litres.ru': PurchaseScriptSpec(domain='litres.ru', lifecycle='publish', relative_path='purchase/litres.ts'),
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
        task: str,
    ) -> PurchaseScriptResult:
        normalized_domain = normalize_domain(domain)
        spec = self._registry.get(normalized_domain)
        if spec is None:
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_not_registered',
                message=f'Для домена {normalized_domain or "<empty>"} нет purchase-скрипта.',
                order_id=None,
                artifacts={'domain': normalized_domain, 'script_registry': self.registry_snapshot()},
            )

        script_path = self._scripts_dir / spec.relative_path
        if spec.lifecycle != 'publish':
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_not_published',
                message=f'Purchase-скрипт для {normalized_domain} пока в lifecycle={spec.lifecycle}.',
                order_id=None,
                artifacts={'domain': normalized_domain, 'script': str(script_path), 'lifecycle': spec.lifecycle},
            )
        if not script_path.is_file():
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_missing',
                message=f'Файл purchase-скрипта {script_path} не найден.',
                order_id=None,
                artifacts={'domain': normalized_domain, 'script': str(script_path), 'lifecycle': spec.lifecycle},
            )
        if not self._tsx_bin.is_file():
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_runtime_missing',
                message=f'Локальный TSX рантайм не найден: {self._tsx_bin}',
                order_id=None,
                artifacts={'domain': normalized_domain, 'script': str(script_path), 'tsx_bin': str(self._tsx_bin)},
            )

        session_dir = self._trace_dir.expanduser() / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        output_path = session_dir / 'purchase-script-result.json'

        try:
            resolved_endpoint = await resolve_cdp_endpoint(self._cdp_endpoint)
        except Exception as exc:
            logger.error(
                'purchase_script_endpoint_resolve_failed session_id=%s domain=%s endpoint=%s error=%s',
                session_id,
                normalized_domain,
                self._cdp_endpoint,
                _tail_text(str(exc), limit=700),
            )
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_cdp_resolve_failed',
                message='Не удалось резолвить CDP endpoint для purchase-скрипта.',
                order_id=None,
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
            '--task',
            task,
            '--output-path',
            str(output_path),
        ]
        logger.info(
            'purchase_script_started session_id=%s domain=%s script=%s lifecycle=%s',
            session_id,
            normalized_domain,
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
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_runtime_missing',
                message='Node.js не найден в контейнере buyer, запуск purchase-скрипта невозможен.',
                order_id=None,
                artifacts={'domain': normalized_domain, 'script': str(script_path)},
            )

        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(process.communicate(), timeout=self._timeout_sec)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_timeout',
                message=f'Purchase-скрипт превысил таймаут {self._timeout_sec}с.',
                order_id=None,
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
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_process_failed',
                message=(
                    f'Purchase-скрипт завершился с кодом {process.returncode}. '
                    f'stderr: {_tail_text(stderr_text)}'
                ),
                order_id=None,
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
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_invalid_json',
                message='Purchase-скрипт не вернул валидный JSON-результат.',
                order_id=None,
                artifacts={
                    'domain': normalized_domain,
                    'script': str(script_path),
                    'cdp_endpoint': self._cdp_endpoint,
                    'resolved_cdp_endpoint': resolved_endpoint,
                    'stdout_tail': _tail_text(stdout_text),
                    'stderr_tail': _tail_text(stderr_text),
                },
            )

        status = str(parsed_payload.get('status') or PURCHASE_SCRIPT_FAILED).strip().lower()
        reason_code = str(parsed_payload.get('reason_code') or 'purchase_script_failed').strip()
        message = str(parsed_payload.get('message') or 'Purchase-скрипт завершился без сообщения.')
        raw_order_id = parsed_payload.get('order_id')
        order_id = str(raw_order_id).strip() if raw_order_id is not None and str(raw_order_id).strip() else None
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
        return PurchaseScriptResult(
            status=status or PURCHASE_SCRIPT_FAILED,
            reason_code=reason_code,
            message=message,
            order_id=order_id,
            artifacts=artifacts,
        )


def _tail_text(text: str, limit: int = 500) -> str:
    compact = ' '.join((text or '').replace('\n', ' ').split())
    if len(compact) <= limit:
        return compact
    return compact[-limit:]
