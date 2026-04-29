from __future__ import annotations

import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import SplitResult, urlsplit


IpAddress = IPv4Address | IPv6Address
HostResolver = Callable[[str], Iterable[IpAddress | str]]


class UrlPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class _TrustedCallbackUrl:
    scheme: str
    hostname: str
    port: int
    path: str


DANGEROUS_HOSTS = {
    'localhost',
    'metadata',
    'metadata.google.internal',
    'host.docker.internal',
}
DANGEROUS_HOST_SUFFIXES = (
    '.localhost',
    '.local',
    '.localdomain',
    '.internal',
    '.lan',
    '.home',
    '.corp',
    '.docker',
)


def parse_url_allowlist(value: str | Iterable[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        items = value.split(',')
    else:
        items = value
    return tuple(item.strip() for item in items if item and item.strip())


def validate_start_url(url: str, *, resolver: HostResolver | None = None) -> str:
    resolved = resolver or _resolve_host_ips
    parts = _parse_http_url(url, field_name='start_url')
    _ensure_public_host(parts.hostname or '', field_name='start_url', resolver=resolved)
    return url


def validate_callback_url(
    url: str,
    *,
    default_callback_url: str,
    trusted_callback_urls: Iterable[str],
    resolver: HostResolver | None = None,
) -> str:
    resolved = resolver or _resolve_host_ips
    parts = _parse_http_url(url, field_name='callback_url')
    _ensure_no_query_or_fragment(parts, field_name='callback_url')
    if _is_trusted_callback_url(parts, default_callback_url, trusted_callback_urls):
        return url
    if parts.scheme.lower() != 'https':
        raise UrlPolicyError('callback_url: публичный callback должен использовать https или быть в trusted allowlist')
    _ensure_public_host(parts.hostname or '', field_name='callback_url', resolver=resolved)
    return url


def _parse_http_url(url: str, *, field_name: str) -> SplitResult:
    if not isinstance(url, str) or not url:
        raise UrlPolicyError(f'{field_name}: URL не должен быть пустым')
    if url != url.strip():
        raise UrlPolicyError(f'{field_name}: URL не должен содержать пробелы по краям')

    parts = urlsplit(url)
    if parts.scheme.lower() not in {'http', 'https'}:
        raise UrlPolicyError(f'{field_name}: разрешены только http/https URL')
    if not parts.hostname:
        raise UrlPolicyError(f'{field_name}: URL должен содержать hostname')
    if parts.username is not None or parts.password is not None:
        raise UrlPolicyError(f'{field_name}: userinfo в URL запрещен')
    try:
        _normalized_port(parts)
    except ValueError as exc:
        raise UrlPolicyError(f'{field_name}: некорректный порт в URL') from exc
    return parts


def _ensure_public_host(hostname: str, *, field_name: str, resolver: HostResolver) -> None:
    host = _normalize_hostname(hostname)
    if not host:
        raise UrlPolicyError(f'{field_name}: URL должен содержать hostname')
    if _is_dangerous_hostname(host):
        raise UrlPolicyError(f'{field_name}: hostname {hostname!r} запрещен URL policy')

    host_ip = _parse_ip_address(host)
    if host_ip is not None:
        _ensure_public_ip(host_ip, field_name=field_name, hostname=hostname)
        return

    resolved_ips = _resolved_ip_addresses(host, resolver=resolver, field_name=field_name)
    for resolved_ip in resolved_ips:
        _ensure_public_ip(resolved_ip, field_name=field_name, hostname=hostname)


def _is_trusted_callback_url(
    parts: SplitResult,
    default_callback_url: str,
    trusted_callback_urls: Iterable[str],
) -> bool:
    candidate = _trusted_callback_key(parts)
    trusted_values = (default_callback_url, *tuple(trusted_callback_urls))
    for raw_url in trusted_values:
        if not raw_url:
            continue
        trusted_parts = _parse_http_url(raw_url, field_name='trusted_callback_url')
        _ensure_no_query_or_fragment(trusted_parts, field_name='trusted_callback_url')
        if candidate == _trusted_callback_key(trusted_parts):
            return True
    return False


def _ensure_no_query_or_fragment(parts: SplitResult, *, field_name: str) -> None:
    if parts.query or parts.fragment:
        raise UrlPolicyError(f'{field_name}: query string и fragment запрещены')


def _trusted_callback_key(parts: SplitResult) -> _TrustedCallbackUrl:
    return _TrustedCallbackUrl(
        scheme=parts.scheme.lower(),
        hostname=_normalize_hostname(parts.hostname or ''),
        port=_normalized_port(parts),
        path=parts.path or '/',
    )


def _normalize_hostname(hostname: str) -> str:
    return hostname.rstrip('.').lower()


def _normalized_port(parts: SplitResult) -> int:
    port = parts.port
    if port is not None:
        return port
    return 443 if parts.scheme.lower() == 'https' else 80


def _is_dangerous_hostname(hostname: str) -> bool:
    if hostname in DANGEROUS_HOSTS:
        return True
    if any(hostname.endswith(suffix) for suffix in DANGEROUS_HOST_SUFFIXES):
        return True
    if '.' not in hostname and _parse_ip_address(hostname) is None:
        return True
    return False


def _parse_ip_address(hostname: str) -> IpAddress | None:
    try:
        return ip_address(hostname)
    except ValueError:
        return None


def _ensure_public_ip(address: IpAddress, *, field_name: str, hostname: str) -> None:
    mapped = address.ipv4_mapped if isinstance(address, IPv6Address) else None
    if mapped is not None:
        _ensure_public_ip(mapped, field_name=field_name, hostname=hostname)
        return
    if not address.is_global:
        raise UrlPolicyError(f'{field_name}: hostname {hostname!r} resolves to non-public address {address}')


def _resolved_ip_addresses(hostname: str, *, resolver: HostResolver, field_name: str) -> tuple[IpAddress, ...]:
    try:
        raw_addresses = tuple(resolver(hostname))
    except OSError as exc:
        raise UrlPolicyError(f'{field_name}: hostname {hostname!r} не удалось разрешить') from exc

    addresses: list[IpAddress] = []
    for raw_address in raw_addresses:
        try:
            addresses.append(ip_address(str(raw_address)))
        except ValueError as exc:
            raise UrlPolicyError(
                f'{field_name}: resolver вернул некорректный IP для hostname {hostname!r}'
            ) from exc
    if not addresses:
        raise UrlPolicyError(f'{field_name}: hostname {hostname!r} не имеет A/AAAA записей')
    return tuple(addresses)


def _resolve_host_ips(hostname: str) -> tuple[IpAddress, ...]:
    records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    addresses: list[IpAddress] = []
    for record in records:
        sockaddr = record[4]
        if not sockaddr:
            continue
        addresses.append(ip_address(sockaddr[0]))
    return tuple(dict.fromkeys(addresses))
