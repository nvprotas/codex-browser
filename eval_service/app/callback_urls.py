from __future__ import annotations

from fastapi import Request


BUYER_CALLBACK_PATH = '/callbacks/buyer'


def build_buyer_callback_url(request: Request) -> str:
    base_url = getattr(request.app.state.settings, 'eval_callback_base_url', None)
    if base_url is not None and base_url.strip():
        return f'{base_url.rstrip("/")}{BUYER_CALLBACK_PATH}'
    return str(request.url_for('receive_buyer_callback'))
