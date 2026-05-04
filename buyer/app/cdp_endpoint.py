from __future__ import annotations

from urllib.parse import urlparse, urlunparse

import httpx


def build_endpoint_candidates(endpoint: str) -> list[str]:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {'http', 'https'}:
        return [endpoint]

    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == 'https' else 80

    candidates: list[str] = []

    def add_candidate(raw: str) -> None:
        if raw not in candidates:
            candidates.append(raw)

    add_candidate(endpoint)

    if parsed.hostname not in {'localhost', '127.0.0.1'}:
        add_candidate(f'{parsed.scheme}://localhost:{port}')
        add_candidate(f'{parsed.scheme}://127.0.0.1:{port}')

    if parsed.hostname != 'host.docker.internal':
        add_candidate(f'{parsed.scheme}://host.docker.internal:{port}')

    return candidates


async def resolve_single_http_endpoint(endpoint: str, *, client: httpx.AsyncClient) -> str:
    parsed = urlparse(endpoint)
    host_header = f'localhost:{parsed.port}' if parsed.port else 'localhost'
    version_url = endpoint.rstrip('/') + '/json/version'
    response = await client.get(version_url, headers={'Host': host_header})
    response.raise_for_status()
    payload = response.json()

    raw_ws = payload.get('webSocketDebuggerUrl')
    if not isinstance(raw_ws, str) or not raw_ws:
        raise RuntimeError('CDP endpoint не вернул webSocketDebuggerUrl.')

    ws_parsed = urlparse(raw_ws)
    return urlunparse((ws_parsed.scheme, parsed.netloc, ws_parsed.path, ws_parsed.params, ws_parsed.query, ws_parsed.fragment))


async def resolve_cdp_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme in {'ws', 'wss'}:
        return endpoint

    if parsed.scheme not in {'http', 'https'}:
        return endpoint

    candidates = build_endpoint_candidates(endpoint)
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for candidate in candidates:
            try:
                return await resolve_single_http_endpoint(candidate, client=client)
            except Exception as exc:  # noqa: BLE001 - сохраняем диагностику по всем кандидатам
                failures.append(f'{candidate}: {exc}')

    details = '; '.join(failures[:4])
    raise RuntimeError(
        'Не удалось подключиться к browser-sidecar ни по одному CDP endpoint. '
        f'Пробовали: {", ".join(candidates)}. Ошибки: {details}'
    )
