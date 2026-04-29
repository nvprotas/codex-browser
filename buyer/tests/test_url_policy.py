from __future__ import annotations

import unittest
from ipaddress import ip_address
from typing import Iterable

from fastapi.testclient import TestClient

from buyer.app import main as buyer_main
from buyer.app.url_policy import (
    UrlPolicyError,
    parse_url_allowlist,
    validate_callback_url,
    validate_start_url,
)


class UrlPolicyTests(unittest.TestCase):
    def _public_resolver(self, host: str) -> Iterable[object]:
        _ = host
        return [ip_address('93.184.216.34')]

    def test_start_url_accepts_public_https_url(self) -> None:
        url = 'https://example.com/catalog?item=1'

        result = validate_start_url(url, resolver=self._public_resolver)

        self.assertEqual(result, url)

    def test_start_url_rejects_private_loopback_link_local_and_metadata_addresses(self) -> None:
        blocked = [
            'http://127.0.0.1/',
            'http://[::1]/',
            'http://169.254.169.254/latest/meta-data/',
            'http://10.0.0.5/',
            'http://172.16.0.1/',
            'http://192.168.1.10/',
        ]

        for url in blocked:
            with self.subTest(url=url), self.assertRaises(UrlPolicyError):
                validate_start_url(url, resolver=self._public_resolver)

    def test_start_url_rejects_localhost_userinfo_and_non_http_schemes(self) -> None:
        blocked = [
            'http://localhost:8000/',
            'https://user@example.com/catalog',
            'https://user:pass@example.com/catalog',
            'file:///etc/passwd',
        ]

        for url in blocked:
            with self.subTest(url=url), self.assertRaises(UrlPolicyError):
                validate_start_url(url, resolver=self._public_resolver)

    def test_start_url_rejects_hostname_resolving_to_private_ip(self) -> None:
        def private_resolver(host: str) -> Iterable[object]:
            _ = host
            return [ip_address('10.1.2.3')]

        with self.assertRaises(UrlPolicyError):
            validate_start_url('https://shop.example/catalog', resolver=private_resolver)

    def test_callback_url_rejects_dangerous_task_provided_hosts(self) -> None:
        default_callback_url = 'http://micro-ui:8080/callbacks'
        blocked = [
            'http://127.0.0.1/callbacks',
            'http://[::1]/callbacks',
            'http://localhost:8080/callbacks',
            'http://169.254.169.254/callbacks',
            'http://10.0.0.5/callbacks',
            'http://172.16.0.1/callbacks',
            'http://192.168.1.10/callbacks',
            'http://user:pass@callback.example.com/callbacks',
        ]

        for url in blocked:
            with self.subTest(url=url), self.assertRaises(UrlPolicyError):
                validate_callback_url(
                    url,
                    default_callback_url=default_callback_url,
                    trusted_callback_urls=(),
                    resolver=self._public_resolver,
                )

    def test_callback_url_accepts_public_https_url(self) -> None:
        url = 'https://middle.example.com/callbacks'

        result = validate_callback_url(
            url,
            default_callback_url='http://micro-ui:8080/callbacks',
            trusted_callback_urls=(),
            resolver=self._public_resolver,
        )

        self.assertEqual(result, url)

    def test_callback_url_accepts_default_internal_callback_without_dns(self) -> None:
        url = 'http://micro-ui:8080/callbacks'

        def forbidden_resolver(host: str) -> Iterable[object]:
            self.fail(f'allowlisted callback URL should not resolve host {host}')

        result = validate_callback_url(
            url,
            default_callback_url=url,
            trusted_callback_urls=(),
            resolver=forbidden_resolver,
        )

        self.assertEqual(result, url)

    def test_callback_url_accepts_trusted_internal_eval_callback_allowlist(self) -> None:
        url = 'http://eval_service:8090/callbacks/buyer'

        def forbidden_resolver(host: str) -> Iterable[object]:
            self.fail(f'trusted callback URL should not resolve host {host}')

        result = validate_callback_url(
            url,
            default_callback_url='http://micro-ui:8080/callbacks',
            trusted_callback_urls=parse_url_allowlist('http://eval_service:8090/callbacks/buyer'),
            resolver=forbidden_resolver,
        )

        self.assertEqual(result, url)

    def test_callback_url_rejects_query_and_fragment_for_public_default_and_trusted_urls(self) -> None:
        default_callback_url = 'http://micro-ui:8080/callbacks'
        trusted_callback_urls = parse_url_allowlist('http://eval_service:8090/callbacks/buyer')
        blocked = [
            'https://middle.example.com/callbacks?token=secret',
            'https://middle.example.com/callbacks#fragment',
            'http://micro-ui:8080/callbacks?token=secret',
            'http://micro-ui:8080/callbacks#fragment',
            'http://eval_service:8090/callbacks/buyer?token=secret',
            'http://eval_service:8090/callbacks/buyer#fragment',
        ]

        for url in blocked:
            with self.subTest(url=url), self.assertRaises(UrlPolicyError):
                validate_callback_url(
                    url,
                    default_callback_url=default_callback_url,
                    trusted_callback_urls=trusted_callback_urls,
                    resolver=self._public_resolver,
                )

    def test_callback_url_rejects_trusted_allowlist_entry_with_query_token(self) -> None:
        with self.assertRaises(UrlPolicyError):
            validate_callback_url(
                'http://eval_service:8090/callbacks/buyer',
                default_callback_url='http://micro-ui:8080/callbacks',
                trusted_callback_urls=parse_url_allowlist(
                    'http://eval_service:8090/callbacks/buyer?token=legacy-secret'
                ),
                resolver=self._public_resolver,
            )


class TaskEndpointUrlPolicyTests(unittest.TestCase):
    def test_create_task_rejects_unsafe_start_url_before_creating_session(self) -> None:
        class FailingService:
            async def create_session(self, **_: object) -> object:
                raise AssertionError('unsafe start_url must be rejected before service call')

        original_service = buyer_main.service
        buyer_main.service = FailingService()
        try:
            client = TestClient(buyer_main.app)
            response = client.post(
                '/v1/tasks',
                json={
                    'task': 'Купить книгу',
                    'start_url': 'http://127.0.0.1:8000/admin',
                },
            )
        finally:
            buyer_main.service = original_service

        self.assertEqual(response.status_code, 422)
        self.assertIn('start_url', response.text)
