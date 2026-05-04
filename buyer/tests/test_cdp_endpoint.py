from __future__ import annotations

import unittest

import httpx

from buyer.app.cdp_endpoint import build_endpoint_candidates, resolve_cdp_endpoint, resolve_single_http_endpoint


class CdpEndpointTests(unittest.IsolatedAsyncioTestCase):
    def test_build_endpoint_candidates_adds_local_fallbacks(self) -> None:
        self.assertEqual(
            build_endpoint_candidates('http://browser:9223'),
            [
                'http://browser:9223',
                'http://localhost:9223',
                'http://127.0.0.1:9223',
                'http://host.docker.internal:9223',
            ],
        )

    async def test_resolve_cdp_endpoint_returns_websocket_endpoint_as_is(self) -> None:
        endpoint = 'ws://127.0.0.1:9223/devtools/browser/session'

        self.assertEqual(await resolve_cdp_endpoint(endpoint), endpoint)

    async def test_resolve_single_http_endpoint_uses_resolved_candidate_netloc(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), 'http://localhost:9223/json/version')
            self.assertEqual(request.headers.get('host'), 'localhost:9223')
            return httpx.Response(
                200,
                json={'webSocketDebuggerUrl': 'ws://browser:9222/devtools/browser/session?trace=1'},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolved = await resolve_single_http_endpoint('http://localhost:9223', client=client)

        self.assertEqual(resolved, 'ws://localhost:9223/devtools/browser/session?trace=1')
