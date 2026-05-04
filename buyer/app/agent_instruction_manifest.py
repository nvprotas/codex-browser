from __future__ import annotations

RUNTIME_DOC_ROOT = '/workspace/docs/buyer-agent'


def build_agent_instruction_manifest(*, start_url: str) -> dict[str, object]:
    _ = start_url

    return {
        'root': f'{RUNTIME_DOC_ROOT}/AGENTS-runtime.md',
        'always_read': [
            f'{RUNTIME_DOC_ROOT}/cdp-tool.md',
            f'{RUNTIME_DOC_ROOT}/context-contract.md',
        ],
        'instructions_dir': f'{RUNTIME_DOC_ROOT}/instructions',
    }
