import type { Browser, BrowserContext, Page } from 'playwright-core';
import { writeFileSync } from 'node:fs';
import { URL } from 'node:url';

type TraceEvent = {
  ts: string;
  event: string;
  url?: string;
  host?: string;
  details?: Record<string, unknown>;
};

function normalizeHost(value: string): string {
  const host = value.trim().toLowerCase();
  return host.startsWith('www.') ? host.slice(4) : host;
}

function hostFromUrl(value: string): string {
  try {
    return normalizeHost(new URL(value).hostname);
  } catch {
    return '';
  }
}

function appendTrace(path: string, event: TraceEvent): void {
  writeFileSync(path, `${JSON.stringify(event)}\n`, { encoding: 'utf-8', flag: 'a' });
}

function compactError(error: unknown): string {
  return String(error).replace(/\s+/g, ' ').slice(0, 500);
}

function writeContainerError(event: string, details: Record<string, unknown>): void {
  process.stderr.write(`${JSON.stringify({ event, ...details })}\n`);
}

function pageSnapshot(page: Page): Record<string, unknown> {
  if (page.isClosed()) {
    return { closed: true, url: null, host: null };
  }
  const url = page.url();
  return { closed: false, url, host: hostFromUrl(url) };
}

function contextPageSnapshots(context: BrowserContext): Record<string, unknown>[] {
  return context.pages().map((page, index) => ({
    index,
    ...pageSnapshot(page),
  }));
}

export async function tracedGoto(
  page: Page,
  tracePath: string,
  stage: string,
  toUrl: string,
  options: Parameters<Page['goto']>[1],
  swallowErrors = false,
): Promise<void> {
  const fromUrl = page.url();
  const startedAt = Date.now();
  appendTrace(tracePath, {
    ts: new Date().toISOString(),
    event: 'auth_navigation_started',
    url: fromUrl,
    host: hostFromUrl(fromUrl),
    details: {
      stage,
      from_url: fromUrl,
      to_url: toUrl,
      to_host: hostFromUrl(toUrl),
      wait_until: options?.waitUntil ?? null,
      timeout_ms: options?.timeout ?? null,
    },
  });

  try {
    const response = await page.goto(toUrl, options);
    const finalUrl = page.url();
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_navigation_finished',
      url: finalUrl,
      host: hostFromUrl(finalUrl),
      details: {
        stage,
        ok: true,
        from_url: fromUrl,
        to_url: toUrl,
        final_url: finalUrl,
        final_host: hostFromUrl(finalUrl),
        response_status: response?.status() ?? null,
        elapsed_ms: Date.now() - startedAt,
      },
    });
  } catch (error) {
    const finalUrl = page.url();
    const errorText = compactError(error);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_navigation_finished',
      url: finalUrl,
      host: hostFromUrl(finalUrl),
      details: {
        stage,
        ok: false,
        from_url: fromUrl,
        to_url: toUrl,
        final_url: finalUrl,
        final_host: hostFromUrl(finalUrl),
        elapsed_ms: Date.now() - startedAt,
        error: errorText,
      },
    });
    writeContainerError('auth_navigation_failed', {
      stage,
      from_url: fromUrl,
      to_url: toUrl,
      final_url: finalUrl,
      final_host: hostFromUrl(finalUrl),
      elapsed_ms: Date.now() - startedAt,
      error: errorText,
    });
    if (!swallowErrors) {
      throw error;
    }
  }
}

export async function tracedPageClose(page: Page, tracePath: string, stage: string, reason: string): Promise<void> {
  const startedAt = Date.now();
  const before = pageSnapshot(page);
  const beforeUrl = typeof before.url === 'string' ? before.url : undefined;
  appendTrace(tracePath, {
    ts: new Date().toISOString(),
    event: 'auth_page_close_started',
    url: beforeUrl,
    host: beforeUrl ? hostFromUrl(beforeUrl) : undefined,
    details: { stage, reason, page: before },
  });

  try {
    await page.close();
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_page_close_finished',
      details: {
        stage,
        reason,
        ok: true,
        elapsed_ms: Date.now() - startedAt,
        page_before_close: before,
        page_after_close: pageSnapshot(page),
      },
    });
  } catch (error) {
    const errorText = compactError(error);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_page_close_finished',
      details: {
        stage,
        reason,
        ok: false,
        elapsed_ms: Date.now() - startedAt,
        page_before_close: before,
        error: errorText,
      },
    });
    writeContainerError('auth_page_close_failed', {
      stage,
      reason,
      elapsed_ms: Date.now() - startedAt,
      page_before_close: before,
      error: errorText,
    });
  }
}

export async function tracedContextClose(
  context: BrowserContext,
  tracePath: string,
  stage: string,
  reason: string,
): Promise<void> {
  const startedAt = Date.now();
  const pagesBeforeClose = contextPageSnapshots(context);
  appendTrace(tracePath, {
    ts: new Date().toISOString(),
    event: 'auth_context_close_started',
    details: {
      stage,
      reason,
      page_count: pagesBeforeClose.length,
      pages: pagesBeforeClose,
    },
  });

  try {
    await context.close();
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_context_close_finished',
      details: {
        stage,
        reason,
        ok: true,
        elapsed_ms: Date.now() - startedAt,
        pages_before_close: pagesBeforeClose,
      },
    });
  } catch (error) {
    const errorText = compactError(error);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_context_close_finished',
      details: {
        stage,
        reason,
        ok: false,
        elapsed_ms: Date.now() - startedAt,
        pages_before_close: pagesBeforeClose,
        error: errorText,
      },
    });
    writeContainerError('auth_context_close_failed', {
      stage,
      reason,
      elapsed_ms: Date.now() - startedAt,
      page_count: pagesBeforeClose.length,
      error: errorText,
    });
  }
}

export async function tracedBrowserClose(browser: Browser, tracePath: string, stage: string, reason: string): Promise<void> {
  const startedAt = Date.now();
  appendTrace(tracePath, {
    ts: new Date().toISOString(),
    event: 'auth_browser_close_started',
    details: { stage, reason },
  });

  try {
    await browser.close();
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_browser_close_finished',
      details: {
        stage,
        reason,
        ok: true,
        elapsed_ms: Date.now() - startedAt,
      },
    });
  } catch (error) {
    const errorText = compactError(error);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_browser_close_finished',
      details: {
        stage,
        reason,
        ok: false,
        elapsed_ms: Date.now() - startedAt,
        error: errorText,
      },
    });
    writeContainerError('auth_browser_close_failed', {
      stage,
      reason,
      elapsed_ms: Date.now() - startedAt,
      error: errorText,
    });
  }
}
