from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth_scripts import domain_from_url, is_domain_in_allowlist
from .models import AgentOutput, PaymentEvidence

LITRES_PAYMENT_EVIDENCE_SOURCE = 'litres_payecom_iframe'
BRANDSHOP_PAYMENT_EVIDENCE_SOURCE = 'brandshop_yoomoney_sberpay_redirect'
PAYECOM_PAYMENT_HOST = 'payecom.ru'
YOOMONEY_PAYMENT_HOST = 'yoomoney.ru'
YOOMONEY_PAYMENT_PATH = '/checkout/payments/v2/contract'
LITRES_DOMAINS = {'litres.ru'}
BRANDSHOP_DOMAINS = {'brandshop.ru'}

#TODO Payment verifier должен быть универсальным и поддерживать любые домены, а не только Litres и Brandshop. Оплата на yoomoney может использовать и на других сайтах. Iframe с payecom может быть встроен в любой сайт или даже открываться как отдельная страница, а не iframe. 
#TODO Сейчас домен не из списка сразу accepted=False. Но это не всегда значит, что платеж не подготовлен/не проведен. Возможно стоит добавить третий статус "unverified" для доменов вне списка, который будет означать, что мы не можем подтвердить факт оплаты, но и не можем подтвердить отсутствие оплаты.

@dataclass(frozen=True)
class PaymentVerificationResult:
    accepted: bool
    failure_reason: str | None = None
    order_id_host: str | None = None


def is_litres_url(raw_url: str) -> bool:
    return is_domain_in_allowlist(domain_from_url(raw_url), LITRES_DOMAINS)


def is_brandshop_url(raw_url: str) -> bool:
    return is_domain_in_allowlist(domain_from_url(raw_url), BRANDSHOP_DOMAINS)


def payecom_order_id_from_url(raw_url: str) -> str | None:
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None
    if parsed.scheme != 'https':
        return None
    if parsed.netloc != PAYECOM_PAYMENT_HOST:
        return None
    if parsed.path != '/pay_ru':
        return None
    if parsed.params:
        return None
    order_values = parse_qs(parsed.query, keep_blank_values=True).get('orderId') or []
    if len(order_values) != 1:
        return None
    order_id = str(order_values[0]).strip()
    return order_id or None


def yoomoney_order_id_from_url(raw_url: str) -> str | None:
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None
    if parsed.scheme != 'https':
        return None
    if parsed.netloc != YOOMONEY_PAYMENT_HOST:
        return None
    if parsed.path != YOOMONEY_PAYMENT_PATH:
        return None
    if parsed.params:
        return None
    order_values = parse_qs(parsed.query, keep_blank_values=True).get('orderId') or []
    if len(order_values) != 1:
        return None
    order_id = str(order_values[0]).strip()
    return order_id or None


def payment_evidence_from_purchase_script(script_result: Any) -> PaymentEvidence | None:
    artifacts = script_result.artifacts if isinstance(getattr(script_result, 'artifacts', None), dict) else {}
    frame_src = artifacts.get('payment_frame_src')
    order_id = str(getattr(script_result, 'order_id', '') or '').strip()
    if isinstance(frame_src, str) and order_id and payecom_order_id_from_url(frame_src) == order_id:
        return PaymentEvidence(source=LITRES_PAYMENT_EVIDENCE_SOURCE, url=frame_src)
    return None


def verify_completed_payment(start_url: str, result: AgentOutput) -> PaymentVerificationResult:
    if is_litres_url(start_url):
        return _verify_litres_payment(result)
    if is_brandshop_url(start_url):
        return _verify_brandshop_payment(result)

    domain = domain_from_url(start_url) or '<unknown>'
    return PaymentVerificationResult(
        accepted=False,
        failure_reason=(
            f'Completed result rejected: для домена {domain} не поддерживается verifier SberPay; '
            'completed success и payment_ready запрещены без domain-specific payment_evidence.'
        ),
    )


def _verify_litres_payment(result: AgentOutput) -> PaymentVerificationResult:
    order_id = str(result.order_id or '').strip()
    if not order_id:
        return PaymentVerificationResult(
            accepted=False,
            failure_reason='Litres completed result rejected: order_id обязателен для подтвержденного шага SberPay.',
        )
    order_id_host = _litres_payment_order_id_host(result, order_id)
    if order_id_host is None:
        return PaymentVerificationResult(
            accepted=False,
            failure_reason=(
                'Litres completed result rejected: order_id должен быть подтвержден '
                'payment_evidence из iframe https://payecom.ru/pay_ru?...orderId=...'
            ),
        )
    return PaymentVerificationResult(accepted=True, order_id_host=order_id_host)


def _verify_brandshop_payment(result: AgentOutput) -> PaymentVerificationResult:
    order_id = str(result.order_id or '').strip()
    if not order_id:
        return PaymentVerificationResult(
            accepted=False,
            failure_reason='Brandshop completed result rejected: order_id обязателен для подтвержденного шага SberPay.',
        )

    evidence = _payment_evidence_to_dict(result.payment_evidence)
    if evidence is None or evidence.get('source') != BRANDSHOP_PAYMENT_EVIDENCE_SOURCE:
        return PaymentVerificationResult(
            accepted=False,
            failure_reason=(
                'Brandshop completed result rejected: order_id должен быть подтвержден '
                'payment_evidence.source=brandshop_yoomoney_sberpay_redirect.'
            ),
        )

    url = evidence.get('url')
    evidence_order_id = yoomoney_order_id_from_url(url) if isinstance(url, str) else None
    if evidence_order_id != order_id:
        return PaymentVerificationResult(
            accepted=False,
            failure_reason=(
                'Brandshop completed result rejected: order_id должен совпадать с единственным непустым '
                'orderId из https://yoomoney.ru/checkout/payments/v2/contract?...'
            ),
        )

    return PaymentVerificationResult(accepted=True, order_id_host=YOOMONEY_PAYMENT_HOST)


def _litres_payment_order_id_host(result: AgentOutput, order_id: str) -> str | None:
    for url in _iter_litres_payment_evidence_urls(result):
        if payecom_order_id_from_url(url) == order_id:
            return PAYECOM_PAYMENT_HOST
    return None


def _iter_litres_payment_evidence_urls(result: AgentOutput) -> list[str]:
    urls: list[str] = []
    direct_evidence = _payment_evidence_to_dict(result.payment_evidence)
    if direct_evidence and direct_evidence.get('source') == LITRES_PAYMENT_EVIDENCE_SOURCE:
        url = direct_evidence.get('url')
        if isinstance(url, str):
            urls.append(url)

    artifact_sources: list[dict[str, Any]] = [result.artifacts]
    purchase_script = result.artifacts.get('purchase_script')
    if isinstance(purchase_script, dict):
        artifact_sources.append(purchase_script)

    for source in artifact_sources:
        frame_src = source.get('payment_frame_src')
        if isinstance(frame_src, str):
            urls.append(frame_src)
        evidence = _payment_evidence_to_dict(source.get('payment_evidence'))  # type: ignore[arg-type]
        if evidence and evidence.get('source') == LITRES_PAYMENT_EVIDENCE_SOURCE:
            url = evidence.get('url')
            if isinstance(url, str):
                urls.append(url)
    return urls


def _payment_evidence_to_dict(value: PaymentEvidence | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(value, PaymentEvidence):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return value
    return None
