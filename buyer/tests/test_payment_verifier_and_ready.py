from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from buyer.app.models import AgentOutput, EventEnvelope, SessionStatus
from buyer.app.payment_verifier import (
    parse_payecom_payment_url,
    parse_yoomoney_payment_url,
    verify_completed_payment,
)
from buyer.app.service import BuyerService
from buyer.app.state import SessionState, SessionStore


def test_provider_parsers_return_order_id_without_merchant_policy() -> None:
    payecom = parse_payecom_payment_url('https://payecom.ru/pay_ru?orderId=order-1')
    yoomoney = parse_yoomoney_payment_url(
        'https://yoomoney.ru/checkout/payments/v2/contract?orderId=order-2'
    )

    assert payecom is not None
    assert payecom.provider == 'payecom'
    assert payecom.order_id == 'order-1'
    assert payecom.host == 'payecom.ru'
    assert yoomoney is not None
    assert yoomoney.provider == 'yoomoney'
    assert yoomoney.order_id == 'order-2'
    assert yoomoney.host == 'yoomoney.ru'


def test_payment_verification_result_statuses_are_explicit() -> None:
    accepted = verify_completed_payment(
        'https://www.litres.ru/',
        AgentOutput(
            status='completed',
            message='Generic runner дошел до Litres SberPay iframe',
            order_id='order-789',
            payment_evidence={
                'source': 'litres_payecom_iframe',
                'url': 'https://payecom.ru/pay_ru?orderId=order-789',
            },
            artifacts={},
        ),
    )
    rejected = verify_completed_payment(
        'https://example-shop.test/',
        AgentOutput(
            status='completed',
            message='Found YooMoney contract URL',
            order_id='unknown-order-123',
            payment_evidence={
                'source': 'yoomoney_payment_url',
                'url': 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=other-order',
            },
            artifacts={},
        ),
    )
    unverified = verify_completed_payment(
        'https://example-shop.test/',
        AgentOutput(
            status='completed',
            message='Found YooMoney contract URL',
            order_id='unknown-order-123',
            payment_evidence={
                'source': 'yoomoney_payment_url',
                'url': 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=unknown-order-123',
            },
            artifacts={},
        ),
    )

    assert accepted.status == 'accepted'
    assert accepted.provider == 'payecom'
    assert accepted.evidence_url == 'https://payecom.ru/pay_ru?orderId=order-789'
    assert accepted.order_id_host == 'payecom.ru'
    assert rejected.status == 'rejected'
    assert unverified.status == 'unverified'
    assert unverified.provider == 'yoomoney'
    assert unverified.evidence_url == 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=unknown-order-123'
    assert unverified.order_id_host == 'yoomoney.ru'


class _SequenceRunner:
    def __init__(self, outputs: list[AgentOutput]) -> None:
        self._outputs = outputs
        self.calls = 0

    async def run_step(self, **_: Any) -> AgentOutput:
        index = min(self.calls, len(self._outputs) - 1)
        self.calls += 1
        return self._outputs[index]


class _RecordingCallbackClient:
    def __init__(self) -> None:
        self.delivered: list[EventEnvelope] = []

    def build_envelope(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        idempotency_suffix: str | None = None,
        *,
        eval_run_id: str | None = None,
        eval_case_id: str | None = None,
    ) -> EventEnvelope:
        seq = len(self.delivered) + 1
        suffix = idempotency_suffix or str(seq)
        return EventEnvelope(
            event_id=f'event-{seq}',
            session_id=session_id,
            event_type=event_type,
            occurred_at=datetime.now(timezone.utc),
            idempotency_key=f'{session_id}:{event_type}:{suffix}',
            payload=payload,
            eval_run_id=eval_run_id,
            eval_case_id=eval_case_id,
        )

    async def deliver(self, callback_url: str, envelope: EventEnvelope, *, headers: dict[str, str] | None = None) -> None:
        _ = callback_url, headers
        self.delivered.append(envelope)


class _NoopAuthScriptRunner:
    def registry_snapshot(self) -> list[dict[str, str]]:
        return []

    async def run(self, **_: Any) -> Any:
        return None


class PaymentVerifierReadyTests(unittest.IsolatedAsyncioTestCase):
    async def test_litres_payment_ready_includes_order_id_host(self) -> None:
        final_state = await self._run_single_output(
            start_url='https://www.litres.ru/',
            output=AgentOutput(
                status='completed',
                message='Generic runner дошел до Litres SberPay iframe',
                order_id='order-789',
                payment_evidence={
                    'source': 'litres_payecom_iframe',
                    'url': 'https://payecom.ru/pay_ru?orderId=order-789',
                },
                artifacts={'source': 'generic'},
            ),
        )

        self.assertEqual(final_state.status, SessionStatus.COMPLETED)
        payment_ready_events = self._events(final_state, 'payment_ready')
        self.assertEqual(len(payment_ready_events), 1)
        payload = payment_ready_events[0].payload
        self.assertEqual(payload.get('order_id'), 'order-789')
        self.assertEqual(payload.get('order_id_host'), 'payecom.ru')
        self.assertIsInstance(payload.get('message'), str)
        self.assertTrue(payload.get('message'))

    async def test_brandshop_yoomoney_evidence_emits_payment_ready_with_order_id_host(self) -> None:
        final_state = await self._run_single_output(
            start_url='https://brandshop.ru/',
            output=self._brandshop_completed(
                order_id='brandshop-order-123',
                payment_evidence={
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=brandshop-order-123',
                },
            ),
        )

        self.assertEqual(final_state.status, SessionStatus.COMPLETED)
        payment_ready_events = self._events(final_state, 'payment_ready')
        self.assertEqual(len(payment_ready_events), 1)
        payload = payment_ready_events[0].payload
        self.assertEqual(payload.get('order_id'), 'brandshop-order-123')
        self.assertEqual(payload.get('order_id_host'), 'yoomoney.ru')
        self.assertIsInstance(payload.get('message'), str)
        self.assertTrue(payload.get('message'))

    async def test_brandshop_rejects_invalid_or_missing_yoomoney_evidence(self) -> None:
        cases: list[tuple[str, str, dict[str, str] | None]] = [
            (
                'http',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'http://yoomoney.ru/checkout/payments/v2/contract?orderId=brandshop-order-123',
                },
            ),
            (
                'subdomain',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://pay.yoomoney.ru/checkout/payments/v2/contract?orderId=brandshop-order-123',
                },
            ),
            (
                'default_port',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://yoomoney.ru:443/checkout/payments/v2/contract?orderId=brandshop-order-123',
                },
            ),
            (
                'non_default_port',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://yoomoney.ru:444/checkout/payments/v2/contract?orderId=brandshop-order-123',
                },
            ),
            (
                'invalid_port',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://yoomoney.ru:bad/checkout/payments/v2/contract?orderId=brandshop-order-123',
                },
            ),
            (
                'wrong_path',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://yoomoney.ru/checkout/payments/v2/contract/extra?orderId=brandshop-order-123',
                },
            ),
            (
                'path_params',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': (
                        'https://yoomoney.ru/checkout/payments/v2/contract;notpay'
                        '?orderId=brandshop-order-123'
                    ),
                },
            ),
            (
                'duplicate_order_id',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': (
                        'https://yoomoney.ru/checkout/payments/v2/contract?'
                        'orderId=brandshop-order-123&orderId=brandshop-order-123'
                    ),
                },
            ),
            (
                'empty_order_id',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=',
                },
            ),
            (
                'mismatch',
                'brandshop-order-123',
                {
                    'source': 'brandshop_yoomoney_sberpay_redirect',
                    'url': 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=other-order',
                },
            ),
            ('missing_source', 'brandshop-order-123', None),
            (
                'wrong_source',
                'brandshop-order-123',
                {
                    'source': 'litres_payecom_iframe',
                    'url': 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=brandshop-order-123',
                },
            ),
        ]

        for case_name, order_id, payment_evidence in cases:
            with self.subTest(case_name=case_name):
                final_state = await self._run_single_output(
                    start_url='https://brandshop.ru/',
                    output=self._brandshop_completed(order_id=order_id, payment_evidence=payment_evidence),
                )

                self.assertEqual(final_state.status, SessionStatus.FAILED)
                self.assertEqual(self._events(final_state, 'payment_ready'), [])
                scenario_finished_events = self._events(final_state, 'scenario_finished')
                self.assertEqual(len(scenario_finished_events), 1)
                self.assertEqual(scenario_finished_events[0].payload.get('status'), 'failed')

    async def test_unknown_merchant_with_known_provider_evidence_finishes_unverified_without_payment_ready(self) -> None:
        final_state = await self._run_single_output(
            start_url='https://example-shop.test/',
            output=AgentOutput(
                status='completed',
                message='Found YooMoney contract URL',
                order_id='unknown-order-123',
                payment_evidence={
                    'source': 'yoomoney_payment_url',
                    'url': 'https://yoomoney.ru/checkout/payments/v2/contract?orderId=unknown-order-123',
                },
                artifacts={'source': 'generic'},
            ),
        )

        self.assertEqual(final_state.status, SessionStatus.UNVERIFIED)
        self.assertEqual(self._events(final_state, 'payment_ready'), [])
        payment_unverified_events = self._events(final_state, 'payment_unverified')
        self.assertEqual(len(payment_unverified_events), 1)
        payload = payment_unverified_events[0].payload
        self.assertEqual(payload.get('order_id'), 'unknown-order-123')
        self.assertEqual(payload.get('provider'), 'yoomoney')
        self.assertEqual(payload.get('order_id_host'), 'yoomoney.ru')
        scenario_finished_events = self._events(final_state, 'scenario_finished')
        self.assertEqual(len(scenario_finished_events), 1)
        self.assertEqual(scenario_finished_events[0].payload.get('status'), 'unverified')

    async def test_litres_rejects_payecom_port_variants(self) -> None:
        cases = [
            'https://payecom.ru:443/pay_ru?orderId=order-789',
            'https://payecom.ru:444/pay_ru?orderId=order-789',
            'https://payecom.ru:bad/pay_ru?orderId=order-789',
            'https://payecom.ru/pay_ru;notpay?orderId=order-789',
        ]

        for frame_src in cases:
            with self.subTest(frame_src=frame_src):
                final_state = await self._run_single_output(
                    start_url='https://www.litres.ru/',
                    output=AgentOutput(
                        status='completed',
                        message='Generic runner дошел до Litres SberPay iframe',
                        order_id='order-789',
                        payment_evidence={
                            'source': 'litres_payecom_iframe',
                            'url': frame_src,
                        },
                        artifacts={'source': 'generic'},
                    ),
                )

                self.assertEqual(final_state.status, SessionStatus.FAILED)
                self.assertEqual(self._events(final_state, 'payment_ready'), [])
                scenario_finished_events = self._events(final_state, 'scenario_finished')
                self.assertEqual(len(scenario_finished_events), 1)
                self.assertEqual(scenario_finished_events[0].payload.get('status'), 'failed')

    async def test_litres_rejects_blank_duplicate_payecom_order_ids(self) -> None:
        cases = [
            'https://payecom.ru/pay_ru?orderId=&orderId=order-789',
            'https://payecom.ru/pay_ru?orderId=order-789&orderId=',
        ]

        for frame_src in cases:
            with self.subTest(frame_src=frame_src):
                final_state = await self._run_single_output(
                    start_url='https://www.litres.ru/',
                    output=AgentOutput(
                        status='completed',
                        message='Generic runner дошел до Litres SberPay iframe',
                        order_id='order-789',
                        payment_evidence={
                            'source': 'litres_payecom_iframe',
                            'url': frame_src,
                        },
                        artifacts={'source': 'generic'},
                    ),
                )

                self.assertEqual(final_state.status, SessionStatus.FAILED)
                self.assertEqual(self._events(final_state, 'payment_ready'), [])
                scenario_finished_events = self._events(final_state, 'scenario_finished')
                self.assertEqual(len(scenario_finished_events), 1)
                self.assertEqual(scenario_finished_events[0].payload.get('status'), 'failed')

    async def test_litres_ignores_legacy_purchase_script_shaped_artifacts(self) -> None:
        final_state = await self._run_single_output(
            start_url='https://www.litres.ru/',
            output=AgentOutput(
                status='completed',
                message='Legacy script-shaped artifacts не должны подтверждать payment_ready',
                order_id='order-789',
                payment_evidence=None,
                artifacts={
                    'purchase_script': {
                        'payment_frame_src': 'https://payecom.ru/pay_ru?orderId=order-789',
                        'payment_evidence': {
                            'source': 'litres_payecom_iframe',
                            'url': 'https://payecom.ru/pay_ru?orderId=order-789',
                        },
                    }
                },
            ),
        )

        self.assertEqual(final_state.status, SessionStatus.FAILED)
        self.assertEqual(self._events(final_state, 'payment_ready'), [])
        scenario_finished_events = self._events(final_state, 'scenario_finished')
        self.assertEqual(len(scenario_finished_events), 1)
        self.assertEqual(scenario_finished_events[0].payload.get('status'), 'failed')

    async def _run_single_output(self, *, start_url: str, output: AgentOutput) -> SessionState:
        runner = _SequenceRunner([output])
        store = SessionStore(max_active_sessions=1)
        service = BuyerService(
            store=store,
            callback_client=_RecordingCallbackClient(),  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
            novnc_url='http://novnc',
            default_callback_url='http://callback',
            cdp_recovery_window_sec=0.2,
            cdp_recovery_interval_ms=1,
            sberid_allowlist=set(),
            auth_script_runner=_NoopAuthScriptRunner(),  # type: ignore[arg-type]
        )

        state = await service.create_session(
            task='Купить товар и дойти до SberPay',
            start_url=start_url,
            callback_url='http://callback',
            metadata={},
            auth=None,
        )
        await state.task_ref
        await service.shutdown_post_session_analysis()
        return await store.get(state.session_id)

    @staticmethod
    def _brandshop_completed(
        *,
        order_id: str,
        payment_evidence: dict[str, str] | None,
    ) -> AgentOutput:
        return AgentOutput(
            status='completed',
            message='Generic runner дошел до Brandshop YooMoney SberPay redirect',
            order_id=order_id,
            payment_evidence=payment_evidence,
            artifacts={'source': 'generic'},
        )

    @staticmethod
    def _events(state: SessionState, event_type: str) -> list[EventEnvelope]:
        return [event for event in state.events if event.event_type == event_type]
