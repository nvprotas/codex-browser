from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_REDACTED_CONTEXT = '[redacted-sensitive-context]'
_SENSITIVE_TEXT_PATTERN = re.compile(
    r'('
    r'storage\s*state|storagestate|'
    r'local[\s_-]*storage|localstorage|'
    r'set-cookie|cookies?\s*[=:]|'
    r'authorization\s*[=:]|bearer\s+|'
    r'access[_-]?token|refresh[_-]?token|id[_-]?token|'
    r'payment[\s_-]*secret|'
    r'\b(?:token|secret|password)\b|'
    r'(?:token|secret|password|card[_\s-]*number|cvv|pan|orderid)\s*[=:]|'
    r'"cookies"|"origins"|"localStorage"'
    r')',
    re.IGNORECASE,
)


def write_agent_context_files(
    *,
    step_dir: Path,
    task: str,
    start_url: str,
    metadata: dict[str, Any],
    memory: list[dict[str, str]],
    latest_user_reply: str | None,
    user_profile_text: str | None,
    auth_state: dict[str, Any] | None,
) -> dict[str, str]:
    step_dir.mkdir(parents=True, exist_ok=True)

    return {
        'task': _write_json(
            step_dir / 'task.json',
            {'task': _sanitize_text(task), 'start_url': _sanitize_text(start_url)},
        ),
        'metadata': _write_json(step_dir / 'metadata.json', _sanitize_context_value(metadata)),
        'memory': _write_json(step_dir / 'memory.json', _normalize_memory(memory[-12:])),
        'latest_user_reply': _write_text(step_dir / 'latest-user-reply.md', _sanitize_text(latest_user_reply or '')),
        'user_profile': _write_text(step_dir / 'user-profile.md', _sanitize_text(user_profile_text or '')),
        'auth_state': _write_json(step_dir / 'auth-state.json', _sanitize_auth_state(auth_state)),
    }


def _write_json(path: Path, payload: Any) -> str:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(path)


def _write_text(path: Path, text: str) -> str:
    path.write_text(text, encoding='utf-8')
    return str(path)


def _normalize_memory(memory: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in memory:
        role = str(item.get('role') or '').strip()
        text = _sanitize_text(str(item.get('text') or item.get('content') or '').strip())
        if role and text:
            normalized.append({'role': role, 'text': text})
    return normalized


def _sanitize_auth_state(value: dict[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {'provided': False}
    provided = value.get('provided')
    authenticated = value.get('authenticated')
    provider = _sanitize_context_value(value.get('provider'))
    summary = _sanitize_auth_summary(value.get('summary')) if isinstance(value.get('summary'), dict) else {}
    return {
        'provided': provided if isinstance(provided, bool) else True,
        'provider': provider if isinstance(provider, str) and provider else None,
        'authenticated': authenticated if isinstance(authenticated, bool) else False,
        'summary': summary,
    }


def _sanitize_auth_summary(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {'source', 'mode', 'path', 'reason_code', 'attempts', 'script_status', 'context_prepared', 'domain'}
    summary: dict[str, Any] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, str):
            summary[key] = _sanitize_text(item)
        elif isinstance(item, (int, bool)) or item is None:
            summary[key] = item
    return summary


def _sanitize_context_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if _is_raw_auth_key(str(key)):
                continue
            safe[key] = _sanitize_context_value(item)
        return safe
    if isinstance(value, list):
        return [_sanitize_context_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


def sanitize_agent_context_text(value: str) -> str:
    if _SENSITIVE_TEXT_PATTERN.search(value):
        return _REDACTED_CONTEXT
    return value


def _sanitize_text(value: str) -> str:
    return sanitize_agent_context_text(value)


def _is_raw_auth_key(key: str) -> bool:
    compact = ''.join(char for char in key.lower() if char.isalnum())
    blocked = {
        'authcontext',
        'authpayload',
        'authorization',
        'cookie',
        'cookies',
        'localstorage',
        'origins',
        'rawauth',
        'storagestate',
        'storagestatepath',
    }
    markers = ('apikey', 'authorizationcode', 'password', 'secret', 'token')
    return compact in blocked or any(marker in compact for marker in markers)
