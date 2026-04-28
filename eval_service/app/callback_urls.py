from __future__ import annotations

from urllib.parse import urlencode

from fastapi import Request


BUYER_CALLBACK_PATH = '/callbacks/buyer'


def build_buyer_callback_url(request: Request) -> str:
    base_url = getattr(request.app.state.settings, 'eval_callback_base_url', None)
    if base_url is None or not base_url.strip():
        raise ValueError('EVAL_CALLBACK_BASE_URL must be configured for buyer callbacks')

    callback_url = f'{base_url.rstrip("/")}{BUYER_CALLBACK_PATH}'
    secret = getattr(request.app.state.settings, 'eval_callback_secret', None)
    if secret is not None and secret.strip():
        return f'{callback_url}?{urlencode({"token": secret})}'
    return callback_url
