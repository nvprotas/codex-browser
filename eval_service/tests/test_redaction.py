from __future__ import annotations

import json

from eval_service.app.redaction import sanitize_for_judge_input


def test_sanitize_for_judge_input_removes_auth_storage_and_payment_secrets() -> None:
    payload = {
        'safe': 'keep',
        'cookies': [{'name': 'sid', 'value': 'cookie-secret'}],
        'storageState': {'cookies': [{'name': 'auth', 'value': 'auth-cookie'}]},
        'headers': {
            'authorization': 'Bearer access-secret',
            'x-api-key': 'api-key-secret',
            'accept': 'application/json',
        },
        'orderId': 'ORDER-12345',
        'payment': {
            'paymentUrl': 'https://pay.example/sberpay/order/ORDER-12345?token=payment-token-secret',
            'payment_id': 'PAYMENT-67890',
            'method': 'sberpay',
        },
        'events': [
            {
                'name': 'payment_ready',
                'message': (
                    'Authorization: Bearer access-secret '
                    'orderId=ORDER-12345 api_key=api-key-secret '
                    'url=https://pay.example/sberpay/order/ORDER-12345?token=payment-token-secret'
                ),
                'refresh_token': 'refresh-secret',
            }
        ],
    }

    sanitized = sanitize_for_judge_input(payload)

    assert sanitized['safe'] == 'keep'
    assert sanitized['headers'] == {'accept': 'application/json'}
    assert sanitized['events'][0]['name'] == 'payment_ready'

    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert 'cookies' not in serialized
    assert 'storageState' not in serialized
    assert 'cookie-secret' not in serialized
    assert 'access-secret' not in serialized
    assert 'api-key-secret' not in serialized
    assert 'ORDER-12345' not in serialized
    assert 'PAYMENT-67890' not in serialized
    assert 'payment-token-secret' not in serialized
    assert 'refresh-secret' not in serialized
    assert 'https://pay.example' not in serialized


def test_sanitize_for_judge_input_redacts_sensitive_text_without_dropping_safe_context() -> None:
    payload = {
        'stdout_tail': (
            'POST /checkout/payment/order_777 result '
            'Set-Cookie: sid=cookie-secret; Path=/ '
            'X-Idempotency-Key: idem-secret '
            'openai_api_key=sk-secret '
            'safe checkout text'
        ),
        'url': 'https://shop.example/checkout/payment/order_777?payment_token=payment-secret&view=summary',
        'localStorage': [{'name': 'cart-state', 'value': 'contains-user-token'}],
    }

    sanitized = sanitize_for_judge_input(payload)
    serialized = json.dumps(sanitized, ensure_ascii=False)

    assert 'safe checkout text' in serialized
    assert 'cookie-secret' not in serialized
    assert 'idem-secret' not in serialized
    assert 'sk-secret' not in serialized
    assert 'order_777' not in serialized
    assert 'payment-secret' not in serialized
    assert 'contains-user-token' not in serialized
