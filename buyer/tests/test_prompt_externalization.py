from __future__ import annotations

import json
from pathlib import Path


def test_prompt_builder_has_no_review_todo_comments() -> None:
    path = Path('buyer/app/prompt_builder.py')

    offenders = [
        f'{path}:{line_number}: {line.strip()}'
        for line_number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1)
        if '#TODO' in line or 'TODO:' in line
    ]

    assert offenders == []


def test_instruction_manifest_points_to_runtime_markdown_files() -> None:
    from buyer.app.agent_instruction_manifest import build_agent_instruction_manifest

    manifest = build_agent_instruction_manifest()

    assert manifest['root'] == '/workspace/docs/buyer-agent/AGENTS-runtime.md'
    assert '/workspace/docs/buyer-agent/cdp-tool.md' in manifest['always_read']
    assert '/workspace/docs/buyer-agent/context-contract.md' in manifest['always_read']
    assert manifest['instructions_dir'] == '/workspace/docs/buyer-agent/instructions'


def test_context_files_are_written_without_raw_auth_payload(tmp_path: Path) -> None:
    from buyer.app.agent_context_files import write_agent_context_files

    manifest = write_agent_context_files(
        step_dir=tmp_path,
        task='Купить книгу. token text-secret-token',
        start_url='https://www.litres.ru/?access_token=url-secret',
        metadata={
            'format': 'ebook',
            'comment': 'Authorization: Bearer metadata-secret',
            'payment_note': 'card_number=4111111111111111 cvv=123',
            'payment_secret_note': 'payment-secret',
            'auth_payload': 'opaque-auth-payload-secret',
            'storageState': {'cookies': [{'name': 'sid', 'value': 'metadata-secret'}], 'origins': []},
            'cookies': [{'name': 'sid', 'value': 'metadata-secret'}],
        },
        memory=[
            {'role': 'user', 'text': 'Предпочитает электронные книги'},
            {'role': 'assistant', 'text': '{"cookies":[{"value":"memory-secret"}]}'},
            {'role': 'system', 'text': 'local_storage memory-local-secret'},
        ],
        latest_user_reply='Нужен EPUB. access_token=reply-secret',
        user_profile_text='Любит фантастику. Cookie: sid=profile-secret',
        auth_state={
            'authenticated': True,
            'profile': 'litres_sberid',
            'extra': 'safe-looking-extra',
            'note': 'Bearer auth-note-secret',
            'storageState': {'cookies': [{'name': 'sid', 'value': 'secret'}], 'origins': []},
            'cookies': [{'name': 'sid', 'value': 'secret'}],
            'access_token': 'token-secret',
            'summary': {
                'source': 'inline',
                'reason_code': 'auth_ok',
                'artifacts': {'stdout_tail': 'neutral-key-secret'},
            },
        },
    )

    assert Path(manifest['task']).is_file()
    assert Path(manifest['metadata']).is_file()
    assert Path(manifest['memory']).is_file()
    assert Path(manifest['latest_user_reply']).read_text(encoding='utf-8') == '[redacted-sensitive-context]'
    assert Path(manifest['user_profile']).read_text(encoding='utf-8') == '[redacted-sensitive-context]'
    assert 'auth_payload' not in json.dumps(manifest, ensure_ascii=False)
    written_text = ''.join(path.read_text(encoding='utf-8') for path in tmp_path.iterdir())
    assert 'Предпочитает электронные книги' in written_text
    assert 'storageState' not in written_text
    assert 'cookies' not in written_text
    assert 'secret' not in written_text
    assert 'metadata-secret' not in written_text
    assert 'token-secret' not in written_text
    assert 'opaque-auth-payload-secret' not in written_text
    assert 'reply-secret' not in written_text
    assert 'profile-secret' not in written_text
    assert 'memory-secret' not in written_text
    assert 'auth-note-secret' not in written_text
    assert 'text-secret-token' not in written_text
    assert 'url-secret' not in written_text
    assert '4111111111111111' not in written_text
    assert 'memory-local-secret' not in written_text
    assert 'payment-secret' not in written_text
    assert 'safe-looking-extra' not in written_text
    assert 'neutral-key-secret' not in written_text


def test_context_files_include_empty_optional_files(tmp_path: Path) -> None:
    from buyer.app.agent_context_files import write_agent_context_files

    manifest = write_agent_context_files(
        step_dir=tmp_path,
        task='Купить книгу',
        start_url='https://www.litres.ru/',
        metadata={},
        memory=[],
        latest_user_reply=None,
        user_profile_text=None,
        auth_state=None,
    )

    assert set(manifest) == {
        'task',
        'metadata',
        'memory',
        'latest_user_reply',
        'user_profile',
        'auth_state',
    }
    assert (tmp_path / 'latest-user-reply.md').read_text(encoding='utf-8') == ''
    assert (tmp_path / 'user-profile.md').read_text(encoding='utf-8') == ''
    assert json.loads((tmp_path / 'auth-state.json').read_text(encoding='utf-8')) == {'provided': False}


def test_prompt_is_short_bootstrap_with_instruction_and_context_paths() -> None:
    from buyer.app.prompt_builder import build_agent_prompt

    prompt = build_agent_prompt(
        task='Купи светлые кроссовки Jordan Air High 45 EU',
        start_url='https://brandshop.ru/',
        browser_cdp_endpoint='http://browser:9223',
        instruction_manifest={
            'root': '/workspace/docs/buyer-agent/AGENTS-runtime.md',
            'always_read': [
                '/workspace/docs/buyer-agent/cdp-tool.md',
                '/workspace/docs/buyer-agent/context-contract.md',
            ],
            'instructions_dir': '/workspace/docs/buyer-agent/instructions',
        },
        context_file_manifest={
            'task': '/workspace/.tmp/buyer-observability/session/step/task.json',
            'metadata': '/workspace/.tmp/buyer-observability/session/step/metadata.json',
            'memory': '/workspace/.tmp/buyer-observability/session/step/memory.json',
        },
    )

    assert '/workspace/docs/buyer-agent/AGENTS-runtime.md' in prompt
    assert '/workspace/docs/buyer-agent/instructions' in prompt
    assert 'Не выполняй реальный платеж' in prompt
    assert 'SBP/FPS/СБП' in prompt
    assert 'Jordan Air High 45 EU' in prompt
    assert 'latest_user_reply' in prompt
    assert 'header search button' not in prompt
    assert '<memory_json>' not in prompt
    assert '<metadata_json>' not in prompt
    assert '<auth_payload_json>' not in prompt
    assert '<auth_context_json>' not in prompt
    assert 'Любит фантастику' not in prompt


def test_prompt_payment_boundary_defaults_to_sberpay_but_allows_explicit_overrides() -> None:
    from buyer.app.prompt_builder import build_agent_prompt

    prompt = build_agent_prompt(
        task="Купить товар на Lamoda и дойти до формы банковской карты",
        start_url="https://www.lamoda.ru/",
        browser_cdp_endpoint="http://browser:9223",
        instruction_manifest={
            "root": "/workspace/docs/buyer-agent/AGENTS-runtime.md",
            "always_read": [],
            "instructions_dir": "/workspace/docs/buyer-agent/instructions",
        },
        context_file_manifest={"task": "/workspace/.tmp/buyer-observability/session/step/task.json"},
    )

    assert "Активная платежная граница по умолчанию - SberPay" in prompt
    assert "site-specific instruction явно не задают другую границу" in prompt
    assert "`bank_card_form`" in prompt
    assert "Для SberPay boundary допустимы только SberPay" in prompt
    assert "Для `bank_card_form` не возвращай `completed`" in prompt


def test_prompt_does_not_inline_latest_user_reply_text() -> None:
    from buyer.app.prompt_builder import build_agent_prompt

    prompt = build_agent_prompt(
        task='Купить товар',
        start_url='https://example-shop.test/',
        browser_cdp_endpoint='http://browser:9223',
        instruction_manifest={'root': '/workspace/docs/buyer-agent/AGENTS-runtime.md', 'always_read': []},
        context_file_manifest={
            'latest_user_reply': '/workspace/.tmp/buyer-observability/session/step/latest-user-reply.md',
        },
    )

    assert '/workspace/.tmp/buyer-observability/session/step/latest-user-reply.md' in prompt
    assert 'Новые инструкции: выбери СБП вместо SberPay' not in prompt
    assert 'access_token=secret' not in prompt


def test_prompt_redacts_secret_like_task_and_start_url() -> None:
    from buyer.app.prompt_builder import build_agent_prompt

    prompt = build_agent_prompt(
        task='Купить товар. token text-secret-token',
        start_url='https://example-shop.test/?access_token=url-secret',
        browser_cdp_endpoint='http://browser:9223',
        instruction_manifest={'root': '/workspace/docs/buyer-agent/AGENTS-runtime.md', 'always_read': []},
        context_file_manifest={'task': '/workspace/.tmp/buyer-observability/session/step/task.json'},
    )

    assert '[redacted-sensitive-context]' in prompt
    assert 'text-secret-token' not in prompt
    assert 'url-secret' not in prompt


def test_agent_auth_state_keeps_only_allowlisted_summary_fields() -> None:
    from buyer.app.models import TaskAuthPayload
    from buyer.app.runner import _build_agent_auth_state

    state = _build_agent_auth_state(
        auth=TaskAuthPayload(provider='sberid'),
        auth_context={
            'provider': 'sberid',
            'domain': 'brandshop.ru',
            'source': 'inline',
            'mode': 'sberid',
            'path': 'script',
            'reason_code': 'auth_ok',
            'attempts': 1,
            'context_prepared': True,
            'artifacts': {'stdout_tail': 'neutral-key-secret'},
            'external_auth': {'payload': 'external-secret'},
            'script_registry': [{'domain': 'brandshop.ru'}],
            'allowlist': ['brandshop.ru'],
        },
    )

    dumped = json.dumps(state, ensure_ascii=False)
    assert state['authenticated'] is True
    assert state['summary']['reason_code'] == 'auth_ok'
    assert state['summary']['domain'] == 'brandshop.ru'
    assert 'neutral-key-secret' not in dumped
    assert 'external-secret' not in dumped
    assert 'script_registry' not in dumped
    assert 'allowlist' not in dumped
