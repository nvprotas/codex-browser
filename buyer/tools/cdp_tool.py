#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

try:
    from buyer.app.cdp_endpoint import resolve_cdp_endpoint
    from buyer.app.url_policy import UrlPolicyError, validate_start_url
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.cdp_endpoint import resolve_cdp_endpoint
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
        if getattr(parsed, 'command', None) == 'wait-url' and not (
            getattr(parsed, 'contains', None) or getattr(parsed, 'regex', None)
        ):
            self.error('wait-url requires --contains or --regex')
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
    click.add_argument('--wait-url-contains')
    click.add_argument('--wait-url-regex')
    click.add_argument('--wait-selector')
    _add_timeout_alias(click)

    fill = sub.add_parser('fill')
    fill.add_argument('--selector', required=True)
    fill.add_argument('--value', required=True)
    _add_timeout_alias(fill)

    otp_fill = sub.add_parser('otp-fill')
    otp_fill.add_argument('--selector', required=True)
    otp_fill.add_argument('--code', required=True)
    otp_fill.add_argument('--digits', type=int, default=4)
    otp_fill.add_argument('--type-delay-ms', type=int, default=120)
    otp_fill.add_argument('--settle-ms', type=int, default=4000)
    otp_fill.add_argument('--wait-gone-selector')
    _add_timeout_alias(otp_fill)

    press = sub.add_parser('press')
    press.add_argument('--key', required=True)
    _add_timeout_alias(press)

    wait_cmd = sub.add_parser('wait')
    wait_cmd.add_argument('--seconds', type=float, required=False)
    wait_cmd.add_argument('--timeout-ms', dest='timeout_ms', type=int, action=_WaitTimeoutMsAction, default=argparse.SUPPRESS)

    wait_url = sub.add_parser('wait-url')
    wait_url.add_argument('--contains')
    wait_url.add_argument('--regex')
    _add_timeout_alias(wait_url)

    wait_selector = sub.add_parser('wait-selector')
    wait_selector.add_argument('--selector', required=True)
    _add_timeout_alias(wait_selector)

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
        details = {'selector': args.selector}
        if args.wait_url_contains:
            details['wait_url_contains'] = args.wait_url_contains
        if args.wait_url_regex:
            details['wait_url_regex'] = args.wait_url_regex
        if args.wait_selector:
            details['wait_selector'] = args.wait_selector
        return details
    if command == 'fill':
        return {'selector': args.selector, 'value_length': len(args.value)}
    if command == 'otp-fill':
        code = re.sub(r'\D+', '', args.code or '')
        return {
            'selector': args.selector,
            'code_length': len(code),
            'digits': args.digits,
            'type_delay_ms': args.type_delay_ms,
            'settle_ms': args.settle_ms,
            'wait_gone_selector': args.wait_gone_selector or args.selector,
        }
    if command == 'press':
        return {'key': args.key}
    if command == 'wait':
        return {'seconds': args.seconds}
    if command == 'wait-url':
        details = {}
        if args.contains:
            details['contains'] = args.contains
        if args.regex:
            details['regex'] = args.regex
        return details
    if command == 'wait-selector':
        return {'selector': args.selector}
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


def _command_log_correlation() -> dict[str, Any]:
    correlation: dict[str, Any] = {'command_id': uuid4().hex}
    attempt_id = os.getenv('BUYER_CODEX_ATTEMPT_ID', '').strip()
    if attempt_id:
        correlation['attempt_id'] = attempt_id
    return correlation


def _command_log_payload(correlation: dict[str, Any], event_sequence: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **correlation,
        'sequence': event_sequence,
        **payload,
    }


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
            if args.command == 'wait-url':
                return await _wait_for_url(page, args)
            if args.command == 'wait-selector':
                return await _wait_for_selector(page, args)
            return await _collect_text(page, args)
        except PlaywrightTimeoutError:
            raise
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
    try:
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
    except Exception as exc:  # noqa: BLE001 - добавляем контекст к strict-mode ошибкам Playwright
        error_text = normalize_error_text(exc)
        if 'strict mode violation' in error_text.lower():
            raise RuntimeError(
                'snapshot strict mode violation: '
                f'selector={args.selector!r}; url={getattr(page, "url", "")}; error={error_text}'
            ) from exc
        raise
    return {'ok': True, 'selector': args.selector, 'limit': limit, 'items': items, 'url': page.url}


def _wait_deadline(args: argparse.Namespace, explicit_deadline: float | None = None) -> float:
    loop_time = asyncio.get_running_loop().time()
    timeout_deadline = loop_time + max(args.timeout_ms, 1) / 1000.0
    if explicit_deadline is None:
        return timeout_deadline
    return min(timeout_deadline, explicit_deadline)


def _url_matches_conditions(url: str, *, contains: str | None, regex: str | None) -> dict[str, str]:
    matched: dict[str, str] = {}
    if contains and contains in url:
        matched['contains'] = contains
    if regex and re.search(regex, url):
        matched['regex'] = regex
    return matched


async def _wait_for_url(page: Any, args: argparse.Namespace, deadline: float | None = None) -> dict[str, Any]:
    contains = getattr(args, 'contains', None) or getattr(args, 'wait_url_contains', None)
    regex = getattr(args, 'regex', None) or getattr(args, 'wait_url_regex', None)
    end = _wait_deadline(args, deadline)
    interval_sec = recovery_interval_sec(args)
    final_url = str(getattr(page, 'url', '') or '')

    while True:
        final_url = str(getattr(page, 'url', '') or '')
        matched = _url_matches_conditions(final_url, contains=contains, regex=regex)
        if matched:
            return {'ok': True, 'url': final_url, 'matched': matched}
        if asyncio.get_running_loop().time() >= end:
            expected: dict[str, str] = {}
            if contains:
                expected['contains'] = contains
            if regex:
                expected['regex'] = regex
            raise PlaywrightTimeoutError(f'wait-url timeout: expected={expected}; final_url={final_url}')
        await asyncio.sleep(interval_sec)


async def _wait_for_selector(page: Any, args: argparse.Namespace, deadline: float | None = None) -> dict[str, Any]:
    end = _wait_deadline(args, deadline)
    interval_sec = recovery_interval_sec(args)
    selector = args.selector if args.command == 'wait-selector' else args.wait_selector
    count = 0
    visible = False

    while True:
        locator = page.locator(selector)
        count = await locator.count()
        if count > 0:
            visible = await locator.first.is_visible()
            return {'ok': True, 'selector': selector, 'url': page.url, 'count': count, 'visible': visible}
        if asyncio.get_running_loop().time() >= end:
            raise PlaywrightTimeoutError(f'wait-selector timeout: selector={selector!r}; url={page.url}; count={count}; visible={visible}')
        await asyncio.sleep(interval_sec)



OTP_INVALID_PATTERNS = (
    r'неверн[ыо]й\s+код',
    r'код\s+не\s+подходит',
    r'код\s+ист[её]к',
    r'время\s+жизни\s+кода\s+истекло',
    r'попробуйте\s+ещ[её]\s+раз',
)


def _normalize_otp_code(raw: str, digits: int) -> str:
    if digits <= 0:
        raise RuntimeError('OTP_CODE_INVALID: --digits must be greater than 0')
    code = re.sub(r'\D+', '', raw or '')
    if len(code) != digits:
        raise RuntimeError(f'OTP_CODE_INVALID: expected {digits} digits, got {len(code)}')
    return code


def _find_otp_invalid_text(text: str) -> str | None:
    for pattern in OTP_INVALID_PATTERNS:
        match = re.search(pattern, text or '', flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return None


async def _otp_body_excerpt(page: Any, max_chars: int = 1200) -> str:
    try:
        body_text = await page.locator('body').inner_text(timeout=1000)
    except Exception:
        return ''
    return ' '.join(str(body_text).split())[:max_chars]


async def _otp_input_state(page: Any, selector: str) -> dict[str, Any]:
    locator = page.locator(selector)
    try:
        count = await locator.count()
    except Exception:
        return {'count': 0, 'visible': False, 'value_length': None}

    visible = False
    value_length: int | None = None
    if count:
        first = locator.first
        try:
            visible = await first.is_visible()
        except Exception:
            visible = False
        try:
            value = await first.evaluate('(node) => "value" in node ? String(node.value || "") : ""')
            value_length = len(value)
        except Exception:
            value_length = None

    return {'count': count, 'visible': visible, 'value_length': value_length}


async def _run_otp_fill(page: Any, args: argparse.Namespace) -> dict[str, Any]:
    code = _normalize_otp_code(args.code, args.digits)
    selector = args.selector
    wait_gone_selector = args.wait_gone_selector or selector
    type_delay_ms = max(0, args.type_delay_ms)
    settle_ms = max(0, args.settle_ms)
    url_before = page.url

    locator = page.locator(selector).first
    await locator.wait_for(state='visible', timeout=args.timeout_ms)
    await locator.click()
    await page.keyboard.press('Control+A')
    await page.keyboard.press('Backspace')
    await page.keyboard.type(code, delay=type_delay_ms)

    deadline = asyncio.get_running_loop().time() + (settle_ms / 1000)
    body_excerpt = ''
    invalid_text = None
    input_state: dict[str, Any] = {'count': None, 'visible': None, 'value_length': None}

    while True:
        input_state = await _otp_input_state(page, wait_gone_selector)
        body_excerpt = await _otp_body_excerpt(page)
        invalid_text = _find_otp_invalid_text(body_excerpt)
        input_gone = input_state.get('count') == 0 or input_state.get('visible') is False
        if invalid_text or input_gone or asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.25)

    url_after = page.url
    input_gone = input_state.get('count') == 0 or input_state.get('visible') is False
    accepted = bool(input_gone and not invalid_text)
    still_open = bool(not input_gone)

    return {
        'ok': True,
        'selector': selector,
        'code_length': len(code),
        'digits': args.digits,
        'accepted': accepted,
        'still_open': still_open,
        'input_gone': bool(input_gone),
        'invalid_code': bool(invalid_text),
        'url_before': url_before,
        'url_after': url_after,
        'url_changed': url_before != url_after,
        'evidence': {
            'input_state': input_state,
            'invalid_text': invalid_text,
            'body_excerpt': body_excerpt,
        },
    }



def _log_command_finished(
    args: argparse.Namespace,
    started: float,
    command_details: dict[str, Any],
    result: dict[str, Any],
    correlation: dict[str, Any],
) -> None:
    _append_action_log(
        'browser_command_finished',
        _command_log_payload(
            correlation,
            2,
            {
                'command': args.command,
                'ok': bool(result.get('ok')),
                'duration_ms': int((asyncio.get_running_loop().time() - started) * 1000),
                'details': command_details,
                'result': _sanitize_result_for_log(result),
            },
        ),
    )


async def run_command(args: argparse.Namespace) -> dict:
    started = asyncio.get_running_loop().time()
    command_details = _extract_command_details_for_log(args)
    correlation = _command_log_correlation()
    _append_action_log(
        'browser_command_started',
        _command_log_payload(
            correlation,
            1,
            {
                'command': args.command,
                'endpoint': args.endpoint,
                'details': command_details,
            },
        ),
    )

    if args.recovery_window_sec < 0:
        result = {'ok': False, 'error': 'CDP_CONFIG_ERROR: --recovery-window-sec не может быть отрицательным.'}
        _log_command_finished(args, started, command_details, result, correlation)
        return result
    if args.recovery_interval_ms <= 0:
        result = {'ok': False, 'error': 'CDP_CONFIG_ERROR: --recovery-interval-ms должен быть > 0.'}
        _log_command_finished(args, started, command_details, result, correlation)
        return result
    if args.command == 'goto':
        policy_error = _navigation_url_policy_error(args.url)
        if policy_error is not None:
            result = {'ok': False, 'error': f'CDP_COMMAND_ERROR: {policy_error}'}
            _log_command_finished(args, started, command_details, result, correlation)
            return result

    playwright = await async_playwright().start()
    try:
        if args.command in {'title', 'text', 'url', 'exists', 'attr', 'links', 'snapshot', 'wait-url', 'wait-selector'}:
            result = await run_read_command_with_retry(playwright=playwright, args=args)
            _log_command_finished(args, started, command_details, result, correlation)
            return result

        deadline = asyncio.get_running_loop().time() + max(args.recovery_window_sec, 0.0)
        browser, page = await connect_page_with_retry(playwright=playwright, args=args, deadline=deadline)

        try:
            if args.command == 'goto':
                await page.goto(args.url, wait_until='domcontentloaded')
                result = {'ok': True, 'url': page.url, 'title': await page.title()}
                _log_command_finished(args, started, command_details, result, correlation)
                return result

            if args.command == 'click':
                await page.click(args.selector)
                result = {'ok': True, 'selector': args.selector, 'url': page.url}
                if args.wait_url_contains or args.wait_url_regex:
                    result['wait_url'] = await _wait_for_url(page, args)
                    result['url'] = result['wait_url']['url']
                if args.wait_selector:
                    result['wait_selector'] = await _wait_for_selector(page, args)
                    result['url'] = result['wait_selector']['url']
                _log_command_finished(args, started, command_details, result, correlation)
                return result

            if args.command == 'fill':
                await page.fill(args.selector, args.value)
                result = {'ok': True, 'selector': args.selector, 'url': page.url}
                _log_command_finished(args, started, command_details, result, correlation)
                return result

            if args.command == 'otp-fill':
                result = await _run_otp_fill(page, args)
                _log_command_finished(args, started, command_details, result, correlation)
                return result

            if args.command == 'press':
                await page.keyboard.press(args.key)
                result = {'ok': True, 'key': args.key, 'url': page.url}
                _log_command_finished(args, started, command_details, result, correlation)
                return result

            if args.command == 'wait':
                await asyncio.sleep(args.seconds)
                result = {'ok': True, 'seconds': args.seconds, 'url': page.url}
                _log_command_finished(args, started, command_details, result, correlation)
                return result

            if args.command == 'screenshot':
                path = Path(args.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(path), full_page=True)
                result = {'ok': True, 'path': str(path), 'url': page.url}
                _log_command_finished(args, started, command_details, result, correlation)
                return result

            if args.command == 'html':
                content = await page.content()
                if args.path:
                    path = Path(args.path)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding='utf-8')
                    result = {'ok': True, 'path': str(path), 'size': len(content), 'url': page.url}
                    _log_command_finished(args, started, command_details, result, correlation)
                    return result
                result = _format_html_result(content=content, url=page.url, max_chars=args.max_chars, full=args.full)
                _log_command_finished(args, started, command_details, result, correlation)
                return result

            result = {'ok': False, 'error': f'Неизвестная команда: {args.command}'}
            _log_command_finished(args, started, command_details, result, correlation)
            return result
        except PlaywrightTimeoutError:
            raise
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
            _command_log_payload(
                correlation,
                2,
                {
                    'command': args.command,
                    'duration_ms': int((asyncio.get_running_loop().time() - started) * 1000),
                    'details': command_details,
                    'error': normalize_error_text(exc),
                },
            ),
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
