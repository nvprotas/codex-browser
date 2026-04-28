from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = '[redacted]'
REDACTED_LOCAL_STORAGE_VALUE = '[redacted-local-storage-value]'
REDACTED_PAYMENT_URL = '[redacted-payment-url]'

SENSITIVE_EXACT_KEYS = {
    'apikey',
    'authorization',
    'clientsecret',
    'cookie',
    'cookies',
    'csrftoken',
    'idempotencykey',
    'openaiapikey',
    'orderid',
    'orderurl',
    'password',
    'paymentid',
    'paymentlink',
    'paymenttoken',
    'paymenturl',
    'refreshtoken',
    'secret',
    'setcookie',
    'storagestate',
    'token',
    'xapikey',
    'xidempotencykey',
}
SENSITIVE_TOKEN_KEYS = {
    'accesstoken',
    'authtoken',
    'csrftoken',
    'idtoken',
    'paymenttoken',
    'refreshtoken',
}
SENSITIVE_QUERY_KEYS = {
    'access_token',
    'api_key',
    'apikey',
    'auth_code',
    'client_secret',
    'code',
    'id_token',
    'idempotency_key',
    'openai_api_key',
    'order',
    'order_id',
    'orderid',
    'payment',
    'payment_id',
    'payment_token',
    'paymentid',
    'paymenttoken',
    'refresh_token',
    'sid',
    'state',
    'token',
    'x-idempotency-key',
}
PAYMENT_URL_MARKERS = (
    '/pay',
    '/payment',
    '/payments',
    '/sberpay',
    'payment_',
    'payment-',
    'paymenttoken',
    'payment_token',
    'sberpay',
)

AUTH_BEARER_RE = re.compile(r'(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]+')
SET_COOKIE_RE = re.compile(r'(?i)\bset-cookie\s*:\s*[^;\s]+(?:;\s*[^;\s]+=[^;\s]+)*')
COOKIE_HEADER_RE = re.compile(r'(?i)(?<!set-)\bcookie\s*:\s*[^,\n\r]+')
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r'(?i)\b('
    r'OPENAI[_ -]?API[_ -]?KEY|'
    r'X-API-Key|'
    r'X-Idempotency-Key|'
    r'idempotency[_-]?key|'
    r'api[_ -]?key|'
    r'client[_ -]?secret|'
    r'access[_ -]?token|'
    r'refresh[_ -]?token|'
    r'payment[_ -]?token|'
    r'auth[_ -]?token|'
    r'csrf[_ -]?token|'
    r'id[_ -]?token|'
    r'order[_ -]?id|'
    r'orderId|'
    r'payment[_ -]?id|'
    r'password|'
    r'secret|'
    r'token'
    r')(\s*(?:[=:]\s*|\s+))([^\s,;&]+)'
)
URL_RE = re.compile(r'https?://[^\s\'"<>)}\]]+')
RELATIVE_PAYMENT_ID_RE = re.compile(
    r'(?i)(/(?:sberpay|payment|order|pay)[_-]?)([A-Za-z0-9._~-]{3,})(?=$|[/?#\s\'"<>)}\],;])'
)


def sanitize_for_judge_input(value: Any) -> Any:
    return _sanitize(value, inside_local_storage=False, parent_key=None)


def _sanitize(value: Any, *, inside_local_storage: bool, parent_key: str | None) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text, parent_key=parent_key):
                continue
            normalized_key = _normalize_key(key_text)
            if inside_local_storage and normalized_key == 'value':
                result[key_text] = REDACTED_LOCAL_STORAGE_VALUE
                continue
            if inside_local_storage and normalized_key == 'name':
                result[key_text] = _redact_text(item) if isinstance(item, str) else item
                continue
            if inside_local_storage and normalized_key != 'name' and not isinstance(item, (dict, list)):
                result[key_text] = REDACTED_LOCAL_STORAGE_VALUE
                continue
            result[key_text] = _sanitize(
                item,
                inside_local_storage=inside_local_storage or normalized_key == 'localstorage',
                parent_key=key_text,
            )
        return result

    if isinstance(value, list):
        return [
            _sanitize(item, inside_local_storage=inside_local_storage, parent_key=parent_key)
            for item in value
        ]

    if isinstance(value, str):
        if inside_local_storage:
            return REDACTED_LOCAL_STORAGE_VALUE
        return _redact_text(value)

    return value


def _is_sensitive_key(key: str, *, parent_key: str | None) -> bool:
    normalized = _normalize_key(key)
    if normalized in SENSITIVE_EXACT_KEYS or normalized in SENSITIVE_TOKEN_KEYS:
        return True
    if normalized.endswith('apikey') or normalized.endswith('secret'):
        return True
    if normalized.endswith('token') and normalized not in {'buyertokensused', 'judgetokensused'}:
        return True
    if normalized.startswith('cookie') and normalized != 'cookiebanner':
        return True

    parent_normalized = _normalize_key(parent_key or '')
    key_marks_id_or_url = normalized in {'id', 'url', 'link'} or normalized.endswith(('id', 'url', 'link'))
    parent_marks_payment = any(marker in parent_normalized for marker in ('order', 'payment', 'pay', 'sberpay'))
    key_marks_payment = any(marker in normalized for marker in ('order', 'payment', 'pay', 'sberpay'))
    return key_marks_id_or_url and (parent_marks_payment or key_marks_payment)


def _normalize_key(key: str) -> str:
    return re.sub(r'[^a-z0-9]', '', key.lower())


def _redact_text(text: str) -> str:
    parsed = _parse_json_like(text)
    if parsed is not None:
        return json.dumps(sanitize_for_judge_input(parsed), ensure_ascii=False, separators=(',', ':'))

    redacted = AUTH_BEARER_RE.sub('Authorization: ' + REDACTED, text)
    redacted = SET_COOKIE_RE.sub('Set-Cookie: ' + REDACTED, redacted)
    redacted = COOKIE_HEADER_RE.sub('Cookie: ' + REDACTED, redacted)
    redacted = URL_RE.sub(lambda match: _redact_url(match.group(0)), redacted)
    redacted = SENSITIVE_ASSIGNMENT_RE.sub(lambda match: f'{match.group(1)}{match.group(2)}{REDACTED}', redacted)
    redacted = RELATIVE_PAYMENT_ID_RE.sub(lambda match: f'{match.group(1)}{REDACTED}', redacted)
    return redacted


def _parse_json_like(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped or stripped[0] not in '[{':
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _redact_url(raw_url: str) -> str:
    try:
        parts = urlsplit(raw_url)
    except ValueError:
        return REDACTED

    lower_url = raw_url.lower()
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    fragment_pairs = parse_qsl(parts.fragment, keep_blank_values=True) if '=' in parts.fragment else []
    has_sensitive_query = any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in query_pairs)
    has_sensitive_fragment = any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in fragment_pairs)
    is_payment_url = any(marker in lower_url for marker in PAYMENT_URL_MARKERS)
    if is_payment_url or has_sensitive_query or has_sensitive_fragment:
        if is_payment_url:
            return REDACTED_PAYMENT_URL
        safe_query = [
            (key, REDACTED if key.lower() in SENSITIVE_QUERY_KEYS else value)
            for key, value in query_pairs
        ]
        safe_fragment = (
            urlencode(
                [
                    (key, REDACTED if key.lower() in SENSITIVE_QUERY_KEYS else value)
                    for key, value in fragment_pairs
                ]
            )
            if fragment_pairs
            else parts.fragment
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), safe_fragment))

    return raw_url
