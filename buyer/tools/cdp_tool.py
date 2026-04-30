#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

try:
    from buyer.app.url_policy import UrlPolicyError, validate_start_url
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.url_policy import UrlPolicyError, validate_start_url

HTML_STDOUT_LIMIT = 20_000
TEXT_STDOUT_LIMIT = 4_000
LINKS_DEFAULT_LIMIT = 50
SNAPSHOT_DEFAULT_LIMIT = 60
SNAPSHOT_TEXT_LIMIT = 160
SNAPSHOT_OPTION_TAGS = ('div', 'span', 'li')
SNAPSHOT_OPTION_CLASS_HINTS = (
    'product-plate',
    'size',
    'variant',
    'option',
    'sku',
    'swatch',
    'search',
    'filter',
    'product-card',
    'cart-item',
    'cart-product',
    'checkout-address',
    'radio-list',
)
SNAPSHOT_OPTION_DATA_ATTRIBUTES = ('data-size', 'data-value', 'data-variant', 'data-sku', 'data-color', 'data-option')
SNAPSHOT_OPTION_STATE_ATTRIBUTES = ('aria-selected', 'aria-checked', 'aria-disabled', 'disabled')
REQUEST_GUARD_ROUTE_PATTERN = '**/*'
NAVIGATION_RESOURCE_TYPES = {'document'}


class _WaitTimeoutMsAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | int | None,
        option_string: str | None = None,
    ) -> None:
        _ = option_string
        timeout_ms = int(values)
        setattr(namespace, self.dest, timeout_ms)
        setattr(namespace, 'seconds', timeout_ms / 1000.0)


class _CdpArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args: list[str] | None = None, namespace: argparse.Namespace | None = None) -> argparse.Namespace:
        parsed = super().parse_args(args, namespace)
        if getattr(parsed, 'command', None) == 'wait' and getattr(parsed, 'seconds', None) is None:
            self.error('wait requires --seconds or compatibility alias --timeout-ms')
        return parsed


def _add_timeout_alias(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument('--timeout-ms', type=int, default=argparse.SUPPRESS)


def parser() -> argparse.ArgumentParser:
    cli = _CdpArgumentParser(description='Утилита управления browser-sidecar через Playwright CDP')
    cli.add_argument('--endpoint', default=os.getenv('BROWSER_CDP_ENDPOINT', 'http://browser:9223'))
    cli.add_argument('--timeout-ms', type=int, default=15000)
    cli.add_argument('--recovery-window-sec', type=float, default=float(os.getenv('CDP_RECOVERY_WINDOW_SEC', '20')))
    cli.add_argument('--recovery-interval-ms', type=int, default=int(os.getenv('CDP_RECOVERY_INTERVAL_MS', '500')))

    sub = cli.add_subparsers(dest='command', required=True)

    goto = sub.add_parser('goto')
    goto.add_argument('--url', required=True)
    _add_timeout_alias(goto)

    click = sub.add_parser('click')
    click.add_argument('--selector', required=True)
    _add_timeout_alias(click)

    fill = sub.add_parser('fill')
    fill.add_argument('--selector', required=True)
    fill.add_argument('--value', required=True)
    _add_timeout_alias(fill)

    press = sub.add_parser('press')
    press.add_argument('--key', required=True)
    _add_timeout_alias(press)

    wait_cmd = sub.add_parser('wait')
    wait_cmd.add_argument('--seconds', type=float, required=False)
    wait_cmd.add_argument('--timeout-ms', dest='timeout_ms', type=int, action=_WaitTimeoutMsAction, default=argparse.SUPPRESS)

    text = sub.add_parser('text')
    text.add_argument('--selector', required=True)
    text.add_argument('--max-chars', '--limit', dest='max_chars', type=int, default=TEXT_STDOUT_LIMIT)
    text.add_argument('--full', action='store_true')
    _add_timeout_alias(text)

    exists = sub.add_parser('exists')
    exists.add_argument('--selector', required=True)
    _add_timeout_alias(exists)

    attr = sub.add_parser('attr')
    attr.add_argument('--selector', required=True)
    attr.add_argument('--name', required=True)
    _add_timeout_alias(attr)

    links = sub.add_parser('links')
    links.add_argument('--selector', default='body')
    links.add_argument('--limit', type=int, default=LINKS_DEFAULT_LIMIT)
    _add_timeout_alias(links)

    snapshot = sub.add_parser('snapshot')
    snapshot.add_argument('--selector', default='body')
    snapshot.add_argument('--limit', type=int, default=SNAPSHOT_DEFAULT_LIMIT)
    _add_timeout_alias(snapshot)

    title = sub.add_parser('title')

    current = sub.add_parser('url')

    screenshot = sub.add_parser('screenshot')
    screenshot.add_argument('--path', required=True)
    _add_timeout_alias(screenshot)

    html = sub.add_parser('html')
    html.add_argument('--path', required=False)
    html.add_argument('--max-chars', type=int, default=HTML_STDOUT_LIMIT)
    html.add_argument('--full', action='store_true')
    _add_timeout_alias(html)

    return cli


TRANSIENT_CONTEXT_MARKERS = (
    'execution context was destroyed',
    'target page, context or browser has been closed',
    'target closed',
    'page closed',
    'context closed',
    'browser has been closed',
)


def normalize_error_text(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return 'unknown error'
    return ' '.join(text.replace('\n', ' ').split())


def is_transient_context_error(error_text: str) -> bool:
    lowered = error_text.lower()
    return any(marker in lowered for marker in TRANSIENT_CONTEXT_MARKERS)


def _navigation_url_policy_error(url: str) -> str | None:
    try:
        validate_start_url(url)
    except UrlPolicyError as exc:
        return normalize_error_text(exc)
    return None


def _is_guarded_navigation_request(request: Any) -> bool:
    resource_type = str(getattr(request, 'resource_type', '') or '').lower()
    if resource_type in NAVIGATION_RESOURCE_TYPES:
        return True

    checker = getattr(request, 'is_navigation_request', None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:  # noqa: BLE001 - неизвестный request не должен ломать ненавигационные команды
        return False


async def _guard_navigation_request_route(route: Any) -> None:
    request = getattr(route, 'request', None)
    if request is None or not _is_guarded_navigation_request(request):
        await route.continue_()
        return

    url = str(getattr(request, 'url', '') or '')
    policy_error = _navigation_url_policy_error(url)
    if policy_error is None:
        await route.continue_()
        return

    _append_action_log(
        'browser_request_blocked',
        {
            'url': url,
            'resource_type': str(getattr(request, 'resource_type', '') or ''),
            'error': policy_error,
        },
    )
    await route.abort('blockedbyclient')


async def install_navigation_request_guard(page: Any) -> None:
    context = getattr(page, 'context', None)
    if context is None:
        return
    await context.route(REQUEST_GUARD_ROUTE_PATTERN, _guard_navigation_request_route)


def recovery_interval_sec(args: argparse.Namespace) -> float:
    return max(args.recovery_interval_ms, 1) / 1000.0


def _log_path() -> Path | None:
    raw = os.getenv('BUYER_CDP_ACTIONS_LOG_PATH', '').strip()
    if not raw:
        return None
    return Path(raw)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_command_details_for_log(args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == 'goto':
        return {'url': args.url}
    if command == 'click':
        return {'selector': args.selector}
    if command == 'fill':
        return {'selector': args.selector, 'value_length': len(args.value)}
    if command == 'press':
        return {'key': args.key}
    if command == 'wait':
        return {'seconds': args.seconds}
    if command == 'text':
        return {'selector': args.selector, 'max_chars': args.max_chars, 'full': args.full}
    if command == 'exists':
        return {'selector': args.selector}
    if command == 'attr':
        return {'selector': args.selector, 'name': args.name}
    if command == 'links':
        return {'selector': args.selector, 'limit': args.limit}
    if command == 'snapshot':
        return {'selector': args.selector, 'limit': args.limit}
    if command == 'screenshot':
        return {'path': args.path}
    if command == 'html':
        return {'path': args.path, 'max_chars': args.max_chars, 'full': args.full}
    return {}


def _sanitize_result_for_log(result: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(result)
    if isinstance(sanitized.get('html'), str):
        html = sanitized.pop('html')
        sanitized.setdefault('html_size', len(html))
    if isinstance(sanitized.get('text'), str):
        text = sanitized['text']
        if len(text) > 400:
            sanitized['text'] = f'{text[:400]}...'
    return sanitized


def _append_action_log(event_type: str, payload: dict[str, Any]) -> None:
    path = _log_path()
    if path is None:
        return

    record = {
        'ts': _utc_now(),
        'event': event_type,
        **payload,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False))
            fh.write('\n')
    except OSError:
        return


def _page_priority(page: Any) -> int:
    url = (getattr(page, 'url', '') or '').strip().lower()
    if not url or url == 'about:blank':
        return 0
    if url.startswith(('http://', 'https://')):
        return 20
    return 10


def _describe_contexts(browser) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for context_index, context in enumerate(browser.contexts):
        summary.append(
            {
                'context_index': context_index,
                'pages': [
                    {
                        'page_index': page_index,
                        'url': page.url,
                    }
                    for page_index, page in enumerate(context.pages)
                ],
            }
        )
    return summary


async def ensure_page(browser):
    best: tuple[tuple[int, int, int], Any, Any] | None = None
    for context_index, context in enumerate(browser.contexts):
        for page_index, page in enumerate(context.pages):
            priority = _page_priority(page)
            candidate_key = (priority, context_index, page_index)
            candidate = (candidate_key, context, page)
            if best is None or candidate_key > best[0]:
                best = candidate

    if best is not None and best[0][0] > 0:
        (priority, context_index, page_index), _context, page = best
        _append_action_log(
            'browser_page_selected',
            {
                'strategy': 'existing_non_blank_page',
                'priority': priority,
                'context_index': context_index,
                'page_index': page_index,
                'url': page.url,
                'contexts': _describe_contexts(browser),
            },
        )
        return page

    if browser.contexts:
        context = browser.contexts[-1]
        context_index = len(browser.contexts) - 1
    else:
        context = await browser.new_context(viewport={'width': 1440, 'height': 900})
        context_index = 0

    if context.pages:
        page = context.pages[-1]
        page_index = len(context.pages) - 1
    else:
        page = await context.new_page()
        page_index = len(context.pages) - 1

    _append_action_log(
        'browser_page_selected',
        {
            'strategy': 'fallback_last_context',
            'context_index': context_index,
            'page_index': page_index,
            'url': page.url,
            'contexts': _describe_contexts(browser),
        },
    )
    return page


def _build_endpoint_candidates(endpoint: str) -> list[str]:
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


async def _resolve_single_http_endpoint(endpoint: str, *, client: httpx.AsyncClient) -> str:
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

    candidates = _build_endpoint_candidates(endpoint)
    failures: list[str] = []

    async with httpx.AsyncClient(timeout=10.0) as client:
        for candidate in candidates:
            try:
                return await _resolve_single_http_endpoint(candidate, client=client)
            except Exception as exc:  # noqa: BLE001 - собираем диагностический хвост по всем кандидатам
                failures.append(f'{candidate}: {exc}')

    details = '; '.join(failures[:4])
    raise RuntimeError(
        'Не удалось подключиться к browser-sidecar ни по одному CDP endpoint. '
        f'Пробовали: {", ".join(candidates)}. Ошибки: {details}'
    )


async def connect_page_with_retry(
    *,
    playwright,
    args: argparse.Namespace,
    deadline: float,
) -> tuple[Any, Any]:
    attempts = 0
    last_error = 'unknown error'
    interval_sec = recovery_interval_sec(args)

    while True:
        attempts += 1
        browser = None
        try:
            target = await resolve_cdp_endpoint(args.endpoint)
            browser = await playwright.chromium.connect_over_cdp(target)
            page = await ensure_page(browser)
            page.set_default_timeout(args.timeout_ms)
            await install_navigation_request_guard(page)
            return browser, page
        except Exception as exc:  # noqa: BLE001 - нормализуем и возвращаем стабильный код ошибки
            last_error = normalize_error_text(exc)
            if browser is not None:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    'CDP_CONNECT_ERROR: '
                    f'recovery_window_sec={args.recovery_window_sec}; attempts={attempts}; last_error={last_error}'
                ) from exc
            await asyncio.sleep(interval_sec)


async def run_read_command_with_retry(*, playwright, args: argparse.Namespace) -> dict:
    deadline = asyncio.get_running_loop().time() + max(args.recovery_window_sec, 0.0)
    attempts = 0
    last_error = 'unknown error'
    interval_sec = recovery_interval_sec(args)

    while True:
        attempts += 1
        browser, page = await connect_page_with_retry(playwright=playwright, args=args, deadline=deadline)
        try:
            if args.command == 'title':
                return {'ok': True, 'title': await page.title()}
            if args.command == 'url':
                return {'ok': True, 'url': page.url}
            if args.command == 'exists':
                count = await page.locator(args.selector).count()
                return {'ok': True, 'selector': args.selector, 'exists': count > 0, 'count': count, 'url': page.url}
            if args.command == 'attr':
                locator = page.locator(args.selector).first
                count = await page.locator(args.selector).count()
                value = await locator.get_attribute(args.name) if count > 0 else None
                return {
                    'ok': True,
                    'selector': args.selector,
                    'name': args.name,
                    'exists': count > 0,
                    'value': value,
                    'url': page.url,
                }
            if args.command == 'links':
                return await _collect_links(page, args)
            if args.command == 'snapshot':
                return await _collect_snapshot(page, args)
            return await _collect_text(page, args)
        except Exception as exc:  # noqa: BLE001 - приводим transient-ошибки к единому формату
            last_error = normalize_error_text(exc)
            if not is_transient_context_error(last_error):
                raise RuntimeError(f'CDP_COMMAND_ERROR: {last_error}') from exc
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    'CDP_TRANSIENT_ERROR: '
                    f'command={args.command}; recovery_window_sec={args.recovery_window_sec}; attempts={attempts}; last_error={last_error}'
                ) from exc
            await asyncio.sleep(interval_sec)
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass


async def _collect_links(page: Any, args: argparse.Namespace) -> dict[str, Any]:
    limit = _normalize_limit(args.limit, default=LINKS_DEFAULT_LIMIT, maximum=300)
    links = await page.locator(args.selector).locator('a').evaluate_all(
        """(nodes, limit) => {
            const compact = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const result = [];
            const seen = new Set();
            for (const node of nodes) {
                if (result.length >= limit) break;
                const href = node.href || node.getAttribute('href') || '';
                const text = compact(node.innerText || node.textContent || '').slice(0, 180);
                const title = compact(node.getAttribute('title') || '').slice(0, 180);
                const ariaLabel = compact(node.getAttribute('aria-label') || '').slice(0, 180);
                const key = `${href}\\n${text}\\n${title}\\n${ariaLabel}`;
                if (!href && !text && !title && !ariaLabel) continue;
                if (seen.has(key)) continue;
                seen.add(key);
                result.push({ href, text, title, aria_label: ariaLabel });
            }
            return result;
        }""",
        limit,
    )
    return {'ok': True, 'selector': args.selector, 'limit': limit, 'links': links, 'url': page.url}


async def _collect_text(page: Any, args: argparse.Namespace) -> dict[str, Any]:
    locator = page.locator(args.selector).first
    value = await locator.evaluate(
        """(node) => {
            const inner = typeof node.innerText === 'string' ? node.innerText : '';
            if (inner.trim()) return inner;
            return node.textContent || '';
        }"""
    )
    return _format_text_result(
        text=str(value or ''),
        selector=args.selector,
        url=page.url,
        max_chars=args.max_chars,
        full=args.full,
    )


async def _collect_snapshot(page: Any, args: argparse.Namespace) -> dict[str, Any]:
    limit = _normalize_limit(args.limit, default=SNAPSHOT_DEFAULT_LIMIT, maximum=500)
    items = await page.locator(args.selector).evaluate(
        """(root, options) => {
            const limit = options.limit;
            const textLimit = options.textLimit;
            const optionTags = options.optionTags;
            const optionClassHints = options.optionClassHints;
            const optionDataAttributes = options.optionDataAttributes;
            const optionStateAttributes = options.optionStateAttributes;
            const compact = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const escapeAttrValue = (value) => String(value).replace(/\\\\/g, '\\\\\\\\').replace(/"/g, '\\\\"');
            const hasAttribute = (node, name) => typeof node.hasAttribute === 'function' && node.hasAttribute(name);
            const selector = [
                'a',
                'button',
                'input',
                'textarea',
                'select',
                '[role]',
                '[data-testid]',
                'h1',
                'h2',
                'h3',
                'label',
                'p'
            ].join(',');
            const optionSelector = [
                ...optionTags.flatMap((tag) => optionClassHints.map((hint) => `${tag}[class*="${escapeAttrValue(hint)}"]`)),
                ...optionTags.flatMap((tag) => optionDataAttributes.map((name) => `${tag}[${name}]`)),
                ...optionTags.flatMap((tag) => optionStateAttributes.map((name) => `${tag}[${name}]`)),
            ].join(',');
            const nodes = [];
            const seen = new Set();
            const addNode = (node) => {
                if (seen.has(node)) return;
                seen.add(node);
                nodes.push(node);
            };
            const addMatchingNodes = (currentSelector) => {
                if (!currentSelector) return;
                if (root.matches && root.matches(currentSelector)) addNode(root);
                root.querySelectorAll(currentSelector).forEach(addNode);
            };
            const hasOptionLikeClass = (node) => {
                const className = compact(node.getAttribute('class') || '').toLowerCase();
                return optionClassHints.some((hint) => className.includes(hint));
            };
            const hasOptionLikeAttribute = (node) => {
                return [...optionDataAttributes, ...optionStateAttributes].some((name) => hasAttribute(node, name));
            };
            const isOptionLikeNode = (node) => {
                const tag = node.tagName.toLowerCase();
                return optionTags.includes(tag) && (hasOptionLikeClass(node) || hasOptionLikeAttribute(node));
            };
            const collectDataAttributes = (node) => {
                const data = {};
                for (const name of optionDataAttributes) {
                    const value = node.getAttribute(name);
                    if (value !== null) data[name] = compact(value).slice(0, textLimit);
                }
                return data;
            };
            addMatchingNodes(selector);
            addMatchingNodes(optionSelector);
            const result = [];
            for (const node of nodes) {
                if (result.length >= limit) break;
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                const visible = rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                const tag = node.tagName.toLowerCase();
                const data = collectDataAttributes(node);
                const optionLike = isOptionLikeNode(node);
                const className = compact(node.getAttribute('class') || '').slice(0, textLimit);
                const id = compact(node.getAttribute('id') || '').slice(0, textLimit);
                const disabled = Boolean(node.disabled) || hasAttribute(node, 'disabled');
                const ariaSelected = node.getAttribute('aria-selected');
                const ariaChecked = node.getAttribute('aria-checked');
                const ariaDisabled = node.getAttribute('aria-disabled');
                const item = {
                    tag,
                    role: node.getAttribute('role') || null,
                    testid: node.getAttribute('data-testid') || null,
                    text: compact(node.innerText || node.textContent || '').slice(0, textLimit),
                    href: node.href || node.getAttribute('href') || null,
                    aria_label: compact(node.getAttribute('aria-label') || '').slice(0, textLimit) || null,
                    name: node.getAttribute('name') || null,
                    type: node.getAttribute('type') || null,
                    placeholder: compact(node.getAttribute('placeholder') || '').slice(0, textLimit) || null,
                    visible,
                };
                const hasData = Object.keys(data).length > 0;
                const hasState = disabled || ariaSelected !== null || ariaChecked !== null || ariaDisabled !== null;
                if (optionLike) {
                    if (className) item.class = className;
                    if (id) item.id = id;
                    item.disabled = disabled;
                    if (ariaSelected !== null) item.aria_selected = ariaSelected;
                    if (ariaChecked !== null) item.aria_checked = ariaChecked;
                    if (ariaDisabled !== null) item.aria_disabled = ariaDisabled;
                    if (hasData) item.data = data;
                } else {
                    if (disabled) item.disabled = true;
                    if (ariaDisabled !== null) item.aria_disabled = ariaDisabled;
                }
                const usefulOption = optionLike && (item.text || item.aria_label || hasData || hasState);
                if (!item.text && !item.href && !item.aria_label && !item.testid && !item.role && !item.placeholder && !usefulOption) {
                    continue;
                }
                result.push(item);
            }
            return result;
        }""",
        {
            'limit': limit,
            'textLimit': SNAPSHOT_TEXT_LIMIT,
            'optionTags': list(SNAPSHOT_OPTION_TAGS),
            'optionClassHints': list(SNAPSHOT_OPTION_CLASS_HINTS),
            'optionDataAttributes': list(SNAPSHOT_OPTION_DATA_ATTRIBUTES),
            'optionStateAttributes': list(SNAPSHOT_OPTION_STATE_ATTRIBUTES),
        },
    )
    return {'ok': True, 'selector': args.selector, 'limit': limit, 'items': items, 'url': page.url}


def _log_command_finished(
    args: argparse.Namespace,
    started: float,
    command_details: dict[str, Any],
    result: dict[str, Any],
) -> None:
    _append_action_log(
        'browser_command_finished',
        {
            'command': args.command,
            'ok': bool(result.get('ok')),
            'duration_ms': int((asyncio.get_running_loop().time() - started) * 1000),
            'details': command_details,
            'result': _sanitize_result_for_log(result),
        },
    )


async def run_command(args: argparse.Namespace) -> dict:
    started = asyncio.get_running_loop().time()
    command_details = _extract_command_details_for_log(args)
    _append_action_log(
        'browser_command_started',
        {
            'command': args.command,
            'endpoint': args.endpoint,
            'details': command_details,
        },
    )

    if args.recovery_window_sec < 0:
        result = {'ok': False, 'error': 'CDP_CONFIG_ERROR: --recovery-window-sec не может быть отрицательным.'}
        _log_command_finished(args, started, command_details, result)
        return result
    if args.recovery_interval_ms <= 0:
        result = {'ok': False, 'error': 'CDP_CONFIG_ERROR: --recovery-interval-ms должен быть > 0.'}
        _log_command_finished(args, started, command_details, result)
        return result
    if args.command == 'goto':
        policy_error = _navigation_url_policy_error(args.url)
        if policy_error is not None:
            result = {'ok': False, 'error': f'CDP_COMMAND_ERROR: {policy_error}'}
            _log_command_finished(args, started, command_details, result)
            return result

    playwright = await async_playwright().start()
    try:
        if args.command in {'title', 'text', 'url', 'exists', 'attr', 'links', 'snapshot'}:
            result = await run_read_command_with_retry(playwright=playwright, args=args)
            _log_command_finished(args, started, command_details, result)
            return result

        deadline = asyncio.get_running_loop().time() + max(args.recovery_window_sec, 0.0)
        browser, page = await connect_page_with_retry(playwright=playwright, args=args, deadline=deadline)

        try:
            if args.command == 'goto':
                await page.goto(args.url, wait_until='domcontentloaded')
                result = {'ok': True, 'url': page.url, 'title': await page.title()}
                _log_command_finished(args, started, command_details, result)
                return result

            if args.command == 'click':
                await page.click(args.selector)
                result = {'ok': True, 'selector': args.selector, 'url': page.url}
                _log_command_finished(args, started, command_details, result)
                return result

            if args.command == 'fill':
                await page.fill(args.selector, args.value)
                result = {'ok': True, 'selector': args.selector, 'url': page.url}
                _log_command_finished(args, started, command_details, result)
                return result

            if args.command == 'press':
                await page.keyboard.press(args.key)
                result = {'ok': True, 'key': args.key, 'url': page.url}
                _log_command_finished(args, started, command_details, result)
                return result

            if args.command == 'wait':
                await asyncio.sleep(args.seconds)
                result = {'ok': True, 'seconds': args.seconds, 'url': page.url}
                _log_command_finished(args, started, command_details, result)
                return result

            if args.command == 'screenshot':
                path = Path(args.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(path), full_page=True)
                result = {'ok': True, 'path': str(path), 'url': page.url}
                _log_command_finished(args, started, command_details, result)
                return result

            if args.command == 'html':
                content = await page.content()
                if args.path:
                    path = Path(args.path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding='utf-8')
                    result = {'ok': True, 'path': str(path), 'size': len(content), 'url': page.url}
                    _log_command_finished(args, started, command_details, result)
                    return result
                result = _format_html_result(content=content, url=page.url, max_chars=args.max_chars, full=args.full)
                _log_command_finished(args, started, command_details, result)
                return result

            result = {'ok': False, 'error': f'Неизвестная команда: {args.command}'}
            _log_command_finished(args, started, command_details, result)
            return result
        except Exception as exc:  # noqa: BLE001 - унифицируем текст ошибок на уровне CLI
            normalized = normalize_error_text(exc)
            if is_transient_context_error(normalized):
                raise RuntimeError(f'CDP_TRANSIENT_ERROR: {normalized}') from exc
            raise RuntimeError(f'CDP_COMMAND_ERROR: {normalized}') from exc
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
    except Exception as exc:  # noqa: BLE001 - единая запись в аудит при любой ошибке
        _append_action_log(
            'browser_command_failed',
            {
                'command': args.command,
                'duration_ms': int((asyncio.get_running_loop().time() - started) * 1000),
                'details': command_details,
                'error': normalize_error_text(exc),
            },
        )
        raise
    finally:
        await playwright.stop()


def _normalize_limit(value: int, *, default: int, maximum: int) -> int:
    if value <= 0:
        return default
    return min(value, maximum)


def _format_html_result(*, content: str, url: str, max_chars: int = HTML_STDOUT_LIMIT, full: bool = False) -> dict[str, Any]:
    html_size = len(content)
    limit = html_size if full or max_chars == 0 else _normalize_limit(max_chars, default=HTML_STDOUT_LIMIT, maximum=html_size)
    truncated = html_size > limit
    return {
        'ok': True,
        'html': content[:limit],
        'html_size': html_size,
        'truncated': truncated,
        'url': url,
    }


def _format_text_result(
    *,
    text: str,
    selector: str,
    url: str,
    max_chars: int = TEXT_STDOUT_LIMIT,
    full: bool = False,
) -> dict[str, Any]:
    text_size = len(text)
    limit = text_size if full or max_chars == 0 else _normalize_limit(max_chars, default=TEXT_STDOUT_LIMIT, maximum=text_size)
    truncated = text_size > limit
    return {
        'ok': True,
        'selector': selector,
        'text': text[:limit],
        'text_size': text_size,
        'truncated': truncated,
        'url': url,
    }


async def main() -> int:
    args = parser().parse_args()
    try:
        result = await run_command(args)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ok') else 1
    except PlaywrightTimeoutError as exc:
        print(json.dumps({'ok': False, 'error': f'CDP_COMMAND_TIMEOUT: {normalize_error_text(exc)}'}, ensure_ascii=False))
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI должен вернуть читаемую ошибку
        print(json.dumps({'ok': False, 'error': normalize_error_text(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
