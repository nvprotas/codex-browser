from __future__ import annotations

import asyncio
import json

import httpx

from eval_service.app.buyer_client import BuyerClient


def test_create_task_posts_buyer_payload_with_inline_storage_state() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code=201,
            json={
                'session_id': 'session-123',
                'status': 'running',
                'novnc_url': 'http://novnc.local/vnc.html',
            },
        )

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url='http://buyer.test') as http_client:
            client = BuyerClient(http_client=http_client)

            response = await client.create_task(
                task='Подготовь покупку до SberPay без оплаты.',
                start_url='https://www.litres.ru/',
                metadata={'eval_case_id': 'litres_book_odyssey_001'},
                callback_url='http://eval.test/callbacks/buyer',
                storage_state={'cookies': [{'name': 'session', 'value': 'secret-cookie'}], 'origins': []},
            )

        assert response.session_id == 'session-123'
        assert response.status == 'running'

    asyncio.run(run())

    assert len(requests) == 1
    assert requests[0].method == 'POST'
    assert requests[0].url.path == '/v1/tasks'
    assert json.loads(requests[0].content) == {
        'task': 'Подготовь покупку до SberPay без оплаты.',
        'start_url': 'https://www.litres.ru/',
        'metadata': {'eval_case_id': 'litres_book_odyssey_001'},
        'callback_url': 'http://eval.test/callbacks/buyer',
        'auth': {
            'provider': 'sberid',
            'storageState': {
                'cookies': [{'name': 'session', 'value': 'secret-cookie'}],
                'origins': [],
            },
        },
    }


def test_create_task_omits_auth_when_storage_state_is_absent() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code=201,
            json={'session_id': 'session-123', 'status': 'created', 'novnc_url': 'http://novnc.local'},
        )

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url='http://buyer.test') as http_client:
            client = BuyerClient(http_client=http_client)

            await client.create_task(
                task='Задача без авторизации.',
                start_url='https://example.com/',
                metadata={},
                callback_url='http://eval.test/callbacks/buyer',
                storage_state=None,
            )

    asyncio.run(run())

    assert 'auth' not in json.loads(requests[0].content)


def test_get_session_calls_buyer_session_endpoint() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code=200,
            json={
                'session_id': 'session-123',
                'status': 'waiting_user',
                'start_url': 'https://www.litres.ru/',
                'callback_url': 'http://eval.test/callbacks/buyer',
                'novnc_url': 'http://novnc.local',
                'created_at': '2026-04-28T12:00:00Z',
                'updated_at': '2026-04-28T12:05:00Z',
                'waiting_reply_id': 'reply-1',
                'last_error': None,
                'events': [],
            },
        )

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url='http://buyer.test') as http_client:
            client = BuyerClient(http_client=http_client)

            response = await client.get_session('session-123')

        assert response.session_id == 'session-123'
        assert response.waiting_reply_id == 'reply-1'

    asyncio.run(run())

    assert requests[0].method == 'GET'
    assert requests[0].url.path == '/v1/sessions/session-123'


def test_send_reply_posts_reply_payload() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code=200,
            json={'session_id': 'session-123', 'accepted': True, 'status': 'running'},
        )

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url='http://buyer.test') as http_client:
            client = BuyerClient(http_client=http_client)

            response = await client.send_reply(
                session_id='session-123',
                reply_id='reply-1',
                message='Да, продолжай.',
            )

        assert response.accepted is True
        assert response.status == 'running'

    asyncio.run(run())

    assert requests[0].method == 'POST'
    assert requests[0].url.path == '/v1/replies'
    assert json.loads(requests[0].content) == {
        'session_id': 'session-123',
        'reply_id': 'reply-1',
        'message': 'Да, продолжай.',
    }
