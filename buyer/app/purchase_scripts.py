from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auth_scripts import normalize_domain, resolve_cdp_endpoint
from .script_runtime import (
    ScriptSpec,
    read_script_result_payload,
    registry_snapshot,
    remove_script_output,
    script_stdio_artifacts,
    unique_script_output_path,
)
from ._utils import tail_text

logger = logging.getLogger('uvicorn.error')

PURCHASE_SCRIPT_COMPLETED = 'completed'
PURCHASE_SCRIPT_FAILED = 'failed'


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
        self._registry: dict[str, ScriptSpec] = {}

    def registry_snapshot(self) -> list[dict[str, str]]:
        return registry_snapshot(self._registry)

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
        remove_script_output(session_dir / 'purchase-script-result.json')
        output_path = unique_script_output_path(session_dir, 'purchase-script-result')
        remove_script_output(output_path)

        try:
            resolved_endpoint = await resolve_cdp_endpoint(self._cdp_endpoint)
        except Exception as exc:
            logger.error(
                'purchase_script_endpoint_resolve_failed session_id=%s domain=%s endpoint=%s error=%s',
                session_id,
                normalized_domain,
                self._cdp_endpoint,
                tail_text(str(exc), limit=700),
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
            return PurchaseScriptResult(
                status=PURCHASE_SCRIPT_FAILED,
                reason_code='purchase_script_process_failed',
                message=(
                    f'Purchase-скрипт завершился с кодом {process.returncode}. '
                    f'stderr: {tail_text(stderr_text)}'
                ),
                order_id=None,
                artifacts=artifacts,
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
                    **stdio_artifacts,
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
                **stdio_artifacts,
            }
        )
        return PurchaseScriptResult(
            status=status or PURCHASE_SCRIPT_FAILED,
            reason_code=reason_code,
            message=message,
            order_id=order_id,
            artifacts=artifacts,
        )
