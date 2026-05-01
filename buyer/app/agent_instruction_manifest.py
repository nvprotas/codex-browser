from __future__ import annotations

from urllib.parse import urlparse

RUNTIME_DOC_ROOT = '/workspace/docs/buyer-agent'


def build_agent_instruction_manifest(*, start_url: str) -> dict[str, object]:
    host = _host(start_url)
    playbook = None
    if host.endswith('litres.ru'):
        playbook = f'{RUNTIME_DOC_ROOT}/playbooks/litres.md'
    if host.endswith('brandshop.ru'):
        playbook = f'{RUNTIME_DOC_ROOT}/playbooks/brandshop.md'

    return {
        'root': f'{RUNTIME_DOC_ROOT}/AGENTS-runtime.md',
        'always_read': [
            f'{RUNTIME_DOC_ROOT}/cdp-tool.md',
            f'{RUNTIME_DOC_ROOT}/context-contract.md',
        ],
        'domain_playbook': playbook,
    }


def _host(raw_url: str) -> str:
    try:
        return urlparse(raw_url).hostname or ''
    except Exception:
        return ''
