from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from .auth_scripts import domain_from_url, is_domain_in_allowlist
from .models import AgentOutput, PaymentEvidence

LITRES_PAYMENT_EVIDENCE_SOURCE = 'litres_payecom_iframe'
BRANDSHOP_PAYMENT_EVIDENCE_SOURCE = 'brandshop_yoomoney_sberpay_redirect'
PAYECOM_PAYMENT_EVIDENCE_SOURCE = 'payecom_payment_url'
YOOMONEY_PAYMENT_EVIDENCE_SOURCE = 'yoomoney_payment_url'
PAYECOM_PAYMENT_HOST = 'payecom.ru'
YOOMONEY_PAYMENT_HOST = 'yoomoney.ru'
YOOMONEY_PAYMENT_PATH = '/checkout/payments/v2/contract'
LITRES_DOMAINS = {'litres.ru'}
BRANDSHOP_DOMAINS = {'brandshop.ru'}

PaymentVerificationStatus = Literal['accepted', 'rejected', 'unverified']


@dataclass(frozen=True)
class ProviderPaymentEvidence:
    provider: str
    host: str
    order_id: str
    url: str


@dataclass(frozen=True)
class PaymentVerificationResult:
    status: PaymentVerificationStatus
    failure_reason: str | None = None
    order_id_host: str | None = None
    provider: str | None = None
    evidence_url: str | None = None


def is_litres_url(raw_url: str) -> bool:
    return is_domain_in_allowlist(domain_from_url(raw_url), LITRES_DOMAINS)


def is_brandshop_url(raw_url: str) -> bool:
    return is_domain_in_allowlist(domain_from_url(raw_url), BRANDSHOP_DOMAINS)


def parse_payecom_payment_url(raw_url: str) -> ProviderPaymentEvidence | None:
    order_id = _provider_order_id_from_url(raw_url, host=PAYECOM_PAYMENT_HOST, path='/pay_ru')
    if order_id is None:
        return None
    return ProviderPaymentEvidence(provider='payecom', host=PAYECOM_PAYMENT_HOST, order_id=order_id, url=raw_url)


def parse_yoomoney_payment_url(raw_url: str) -> ProviderPaymentEvidence | None:
    order_id = _provider_order_id_from_url(raw_url, host=YOOMONEY_PAYMENT_HOST, path=YOOMONEY_PAYMENT_PATH)
    if order_id is None:
        return None
    return ProviderPaymentEvidence(provider='yoomoney', host=YOOMONEY_PAYMENT_HOST, order_id=order_id, url=raw_url)


def _provider_order_id_from_url(raw_url: str, *, host: str, path: str) -> str | None:
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None
    if parsed.scheme != 'https':
        return None
    if parsed.netloc != host:
        return None
    if parsed.path != path:
        return None
    if parsed.params:
        return None
    order_values = parse_qs(parsed.query, keep_blank_values=True).get('orderId') or []
    if len(order_values) != 1:
        return None
    order_id = str(order_values[0]).strip()
    return order_id or None


def verify_completed_payment(start_url: str, result: AgentOutput) -> PaymentVerificationResult:
    if is_litres_url(start_url):
        return _verify_litres_payment(result)
    if is_brandshop_url(start_url):
        return _verify_brandshop_payment(result)

    domain = domain_from_url(start_url) or '<unknown>'
    return _verify_unknown_merchant_payment(result, domain=domain)


def _verify_litres_payment(result: AgentOutput) -> PaymentVerificationResult:
    order_id = str(result.order_id or '').strip()
    if not order_id:
        return _rejected('Litres completed result rejected: order_id обязателен для подтвержденного шага SberPay.')
    evidence = _litres_payment_evidence(result, order_id)
    if evidence is None:
        return _rejected(
            failure_reason=(
                'Litres completed result rejected: order_id должен быть подтвержден '
                'payment_evidence из iframe https://payecom.ru/pay_ru?...orderId=...'
            ),
        )
    return _accepted(evidence)


def _verify_brandshop_payment(result: AgentOutput) -> PaymentVerificationResult:
    order_id = str(result.order_id or '').strip()
    if not order_id:
        return _rejected('Brandshop completed result rejected: order_id обязателен для подтвержденного шага SberPay.')

    evidence = _payment_evidence_to_dict(result.payment_evidence)
    if evidence is None or evidence.get('source') != BRANDSHOP_PAYMENT_EVIDENCE_SOURCE:
        return _rejected(
            failure_reason=(
                'Brandshop completed result rejected: order_id должен быть подтвержден '
                'payment_evidence.source=brandshop_yoomoney_sberpay_redirect.'
            ),
        )

    url = evidence.get('url')
    provider_evidence = parse_yoomoney_payment_url(url) if isinstance(url, str) else None
    if provider_evidence is None or provider_evidence.order_id != order_id:
        return _rejected(
            failure_reason=(
                'Brandshop completed result rejected: order_id должен совпадать с единственным непустым '
                'orderId из https://yoomoney.ru/checkout/payments/v2/contract?...'
            ),
        )

    return _accepted(provider_evidence)


def _verify_unknown_merchant_payment(result: AgentOutput, *, domain: str) -> PaymentVerificationResult:
    order_id = str(result.order_id or '').strip()
    if not order_id:
        return _rejected(
            f'Completed result rejected: для домена {domain} не поддерживается verifier SberPay, '
            'а order_id отсутствует.'
        )

    for evidence in _iter_provider_payment_evidence(result):
        if evidence.order_id == order_id:
            return _unverified(
                evidence,
                reason=(
                    f'Completed result unverified: для домена {domain} нет merchant verifier, '
                    f'но найден provider evidence {evidence.provider} с совпадающим order_id.'
                ),
            )

    return _rejected(
        f'Completed result rejected: для домена {domain} не поддерживается verifier SberPay; '
        'нет matching PayEcom/YooMoney payment_evidence для top-level order_id.'
    )


def _accepted(evidence: ProviderPaymentEvidence) -> PaymentVerificationResult:
    return PaymentVerificationResult(
        status='accepted',
        order_id_host=evidence.host,
        provider=evidence.provider,
        evidence_url=evidence.url,
    )


def _rejected(failure_reason: str) -> PaymentVerificationResult:
    return PaymentVerificationResult(status='rejected', failure_reason=failure_reason)


def _unverified(evidence: ProviderPaymentEvidence, *, reason: str) -> PaymentVerificationResult:
    return PaymentVerificationResult(
        status='unverified',
        failure_reason=reason,
        order_id_host=evidence.host,
        provider=evidence.provider,
        evidence_url=evidence.url,
    )


def _litres_payment_evidence(result: AgentOutput, order_id: str) -> ProviderPaymentEvidence | None:
    evidence = _payment_evidence_to_dict(result.payment_evidence)
    if evidence is None or evidence.get('source') != LITRES_PAYMENT_EVIDENCE_SOURCE:
        return None
    url = evidence.get('url')
    provider_evidence = parse_payecom_payment_url(url) if isinstance(url, str) else None
    if provider_evidence is not None and provider_evidence.order_id == order_id:
        return provider_evidence
    return None


def _iter_provider_payment_evidence(result: AgentOutput) -> list[ProviderPaymentEvidence]:
    evidence: list[ProviderPaymentEvidence] = []
    direct_evidence = _payment_evidence_to_dict(result.payment_evidence)
    if direct_evidence is None:
        return evidence
    url = direct_evidence.get('url')
    parsed = None
    if isinstance(url, str):
        parsed = parse_payecom_payment_url(url) or parse_yoomoney_payment_url(url)
    if parsed is not None:
        evidence.append(parsed)
    return evidence


def _payment_evidence_to_dict(value: PaymentEvidence | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(value, PaymentEvidence):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return value
    return None
