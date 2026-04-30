import { chromium, type BrowserContext, type Locator, type Page } from 'playwright-core';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, URL } from 'node:url';

type ScriptResult = {
  status: 'completed' | 'failed';
  reason_code: string;
  message: string;
  artifacts: Record<string, unknown>;
};

type TraceEvent = {
  ts: string;
  event: string;
  url?: string;
  host?: string;
  details?: Record<string, unknown>;
};

type AuthVerification = {
  verified: boolean;
  callback_seen: boolean;
  markers: string[];
  login_form_seen: boolean;
};

function arg(name: string): string {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    throw new Error(`Missing argument: ${name}`);
  }
  return process.argv[index + 1]!;
}

function normalizeHost(value: string): string {
  const host = value.trim().toLowerCase();
  return host.startsWith('www.') ? host.slice(4) : host;
}

export function hostFromUrl(value: string): string {
  try {
    return normalizeHost(new URL(value).hostname);
  } catch {
    return '';
  }
}

export function isSameOrSubdomain(host: string, expected: string): boolean {
  if (!host || !expected) {
    return false;
  }
  return host === expected || host.endsWith(`.${expected}`);
}

function isSberIdHost(host: string): boolean {
  return host === 'id.sber.ru' || host.endsWith('.id.sber.ru');
}

function isLitresAuthCallbackUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    return isSameOrSubdomain(normalizeHost(url.hostname), 'litres.ru') && url.pathname.startsWith('/callbacks/social-auth/');
  } catch {
    return false;
  }
}

export function authEntryUrl(startUrl: string): string {
  const url = new URL(startUrl);
  const host = normalizeHost(url.hostname);
  if (!isSameOrSubdomain(host, 'litres.ru')) {
    return startUrl;
  }
  url.pathname = '/auth/login/';
  url.search = '';
  url.hash = '';
  return url.toString();
}

function authVerificationUrl(startUrl: string): string {
  const url = new URL(startUrl);
  url.pathname = '/me/profile/';
  url.search = '';
  url.hash = '';
  return url.toString();
}

export function verifyLitresAuthSnapshot(rawUrl: string, bodyText: string | null | undefined): AuthVerification {
  const callbackSeen = isLitresAuthCallbackUrl(rawUrl);
  const text = String(bodyText || '').replace(/\s+/g, ' ').trim();
  const markers: string[] = [];
  if (/(^|\s)Мои книги(\s|$)/u.test(text)) {
    markers.push('Мои книги');
  }
  if (/(^|\s)Профиль(\s|$)/u.test(text)) {
    markers.push('Профиль');
  }
  const loginFormSeen = /Вход или регистрация|Почта или логин|auth\/login|Продолжить с почтой/iu.test(text);
  return {
    verified: !callbackSeen && markers.includes('Мои книги') && markers.includes('Профиль') && !loginFormSeen,
    callback_seen: callbackSeen,
    markers,
    login_form_seen: loginFormSeen,
  };
}

function cookieCount(storageState: unknown): number {
  if (!storageState || typeof storageState !== 'object') {
    return 0;
  }
  const cookies = (storageState as { cookies?: unknown }).cookies;
  return Array.isArray(cookies) ? cookies.length : 0;
}

function storageCookies(storageState: unknown): Parameters<BrowserContext['addCookies']>[0] {
  if (!storageState || typeof storageState !== 'object') {
    return [];
  }
  const cookies = (storageState as { cookies?: unknown }).cookies;
  return Array.isArray(cookies) ? (cookies as Parameters<BrowserContext['addCookies']>[0]) : [];
}

function cookieSummary(storageState: unknown): Record<string, unknown> {
  if (!storageState || typeof storageState !== 'object') {
    return { cookies_count: 0, domains: [], names: [] };
  }
  const cookies = (storageState as { cookies?: unknown }).cookies;
  if (!Array.isArray(cookies)) {
    return { cookies_count: 0, domains: [], names: [] };
  }
  const domains = new Set<string>();
  const names = new Set<string>();
  for (const cookie of cookies) {
    if (!cookie || typeof cookie !== 'object') {
      continue;
    }
    const item = cookie as { domain?: unknown; name?: unknown; path?: unknown };
    if (typeof item.domain === 'string') {
      domains.add(item.domain);
    }
    if (typeof item.name === 'string') {
      names.add(item.name);
    }
  }
  return {
    cookies_count: cookies.length,
    domains: [...domains].sort(),
    names: [...names].sort(),
  };
}

function save(path: string, payload: ScriptResult): void {
  writeFileSync(path, JSON.stringify(payload, null, 2), { encoding: 'utf-8' });
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function appendTrace(path: string, event: TraceEvent): void {
  writeFileSync(path, `${JSON.stringify(event)}\n`, { encoding: 'utf-8', flag: 'a' });
}

async function tracePage(
  page: Page,
  tracePath: string,
  event: string,
  details: Record<string, unknown> = {},
): Promise<void> {
  const url = page.url();
  const htmlSize = await page.content().then((html) => html.length).catch(() => null);
  const bodyText = await page
    .locator('body')
    .innerText({ timeout: 1000 })
    .then((text) => text.slice(0, 1500))
    .catch(() => null);
  appendTrace(tracePath, {
    ts: new Date().toISOString(),
    event,
    url,
    host: hostFromUrl(url),
    details: {
      ...details,
      html_size: htmlSize,
      body_text_head: bodyText,
    },
  });
}

async function pageBodyText(page: Page): Promise<string> {
  return page
    .locator('body')
    .innerText({ timeout: 1500 })
    .then((text) => text.slice(0, 3000))
    .catch(() => '');
}

async function verifyAuthPage(page: Page, tracePath: string, event: string): Promise<AuthVerification> {
  const bodyText = await pageBodyText(page);
  const verification = verifyLitresAuthSnapshot(page.url(), bodyText);
  appendTrace(tracePath, {
    ts: new Date().toISOString(),
    event,
    url: page.url(),
    host: hostFromUrl(page.url()),
    details: {
      ...verification,
      body_text_head: bodyText.slice(0, 500),
    },
  });
  return verification;
}

async function verifyCurrentOrProfilePage(page: Page, startUrl: string, tracePath: string): Promise<AuthVerification> {
  let verification = await verifyAuthPage(page, tracePath, 'auth_verify_current');
  if (verification.verified) {
    return verification;
  }

  await page.goto(authVerificationUrl(startUrl), { waitUntil: 'domcontentloaded', timeout: 12000 }).catch(() => undefined);
  await page.waitForLoadState('networkidle', { timeout: 3000 }).catch(() => undefined);
  verification = await verifyAuthPage(page, tracePath, 'auth_verify_profile');
  if (verification.verified) {
    return verification;
  }

  await page.goto(startUrl, { waitUntil: 'domcontentloaded', timeout: 12000 }).catch(() => undefined);
  await page.waitForLoadState('networkidle', { timeout: 3000 }).catch(() => undefined);
  return verifyAuthPage(page, tracePath, 'auth_verify_start_url');
}

type ClickTarget = {
  label: string;
  timeoutMs?: number;
  locator: (page: Page) => Locator;
};

type ClickResult = {
  label: string;
  page: Page;
};

async function clickFirstVisible(
  page: Page,
  targets: ClickTarget[],
  options: {
    timeoutMs?: number;
    popupTimeoutMs?: number;
    popupContext?: BrowserContext;
    tracePath?: string;
  } = {},
): Promise<ClickResult | null> {
  for (const target of targets) {
    const timeoutMs = target.timeoutMs ?? options.timeoutMs ?? 2500;
    const startedAt = Date.now();
    const locator = target.locator(page).first();
    if (options.tracePath) {
      appendTrace(options.tracePath, {
        ts: new Date().toISOString(),
        event: 'locator_attempt_started',
        url: page.url(),
        host: hostFromUrl(page.url()),
        details: {
          label: target.label,
          timeout_ms: timeoutMs,
        },
      });
    }
    try {
      await locator.waitFor({ state: 'visible', timeout: timeoutMs });
      const popupPromise = options.popupContext
        ? options.popupContext.waitForEvent('page', { timeout: options.popupTimeoutMs ?? 5000 }).catch(() => null)
        : Promise.resolve(null);
      await locator.click({ timeout: timeoutMs });
      const popup = await popupPromise;
      if (popup) {
        await popup.waitForLoadState('domcontentloaded', { timeout: 10000 }).catch(() => undefined);
      }
      if (options.tracePath) {
        appendTrace(options.tracePath, {
          ts: new Date().toISOString(),
          event: 'locator_attempt_finished',
          url: page.url(),
          host: hostFromUrl(page.url()),
          details: {
            label: target.label,
            success: true,
            elapsed_ms: Date.now() - startedAt,
            popup_opened: Boolean(popup),
          },
        });
      }
      return {
        label: target.label,
        page: popup ?? page,
      };
    } catch (error) {
      if (options.tracePath) {
        appendTrace(options.tracePath, {
          ts: new Date().toISOString(),
          event: 'locator_attempt_finished',
          url: page.url(),
          host: hostFromUrl(page.url()),
          details: {
            label: target.label,
            success: false,
            elapsed_ms: Date.now() - startedAt,
            error: String(error).replace(/\s+/g, ' ').slice(0, 500),
          },
        });
      }
      continue;
    }
  }
  return null;
}

async function waitForFirstVisible(page: Page, targets: ClickTarget[], timeoutMs: number): Promise<string | null> {
  if (targets.length === 0) {
    return null;
  }

  return new Promise((resolveMatch) => {
    let resolved = false;
    let pending = targets.length;

    for (const target of targets) {
      target
        .locator(page)
        .first()
        .waitFor({ state: 'visible', timeout: target.timeoutMs ?? timeoutMs })
        .then(() => {
          if (!resolved) {
            resolved = true;
            resolveMatch(target.label);
          }
        })
        .catch(() => {
          pending -= 1;
          if (!resolved && pending === 0) {
            resolved = true;
            resolveMatch(null);
          }
        });
    }
  });
}

async function clickOptionalChrome(page: Page): Promise<void> {
  await clickFirstVisible(
    page,
    [
      { label: 'accept-cookies', locator: (target) => target.getByRole('button', { name: /принять|соглас/i }) },
      { label: 'close-dialog', locator: (target) => target.getByRole('button', { name: /закрыть|close/i }) },
      { label: 'close-modal', locator: (target) => target.locator('[aria-label*="Закрыть"], [aria-label*="Close"]').first() },
    ],
    { timeoutMs: 900 },
  ).catch(() => null);
}

function loginTargets(): ClickTarget[] {
  return [
    { label: 'role-button-login', locator: (page) => page.getByRole('button', { name: /^войти$/i }) },
    { label: 'role-link-login', locator: (page) => page.getByRole('link', { name: /^войти$/i }) },
    { label: 'text-login', locator: (page) => page.getByText(/^Войти$/i) },
    { label: 'css-login', locator: (page) => page.locator('button:has-text("Войти"), a:has-text("Войти")') },
  ];
}

function otherWaysTargets(): ClickTarget[] {
  return [
    { label: 'role-button-other-ways', locator: (page) => page.getByRole('button', { name: /другие способы/i }) },
    { label: 'role-link-other-ways', locator: (page) => page.getByRole('link', { name: /другие способы/i }) },
    { label: 'text-other-ways', locator: (page) => page.getByText(/другие способы/i) },
    { label: 'css-other-ways', locator: (page) => page.locator('button:has-text("Другие способы"), a:has-text("Другие способы")') },
  ];
}

function sberIdTargets(): ClickTarget[] {
  return [
    {
      label: 'litres-sb-icon',
      timeoutMs: 1200,
      locator: (page) => page.locator('button:has([data-testid="icon"] img[alt="sb"]), button:has(img[alt="sb"])'),
    },
    {
      label: 'litres-sb-img',
      timeoutMs: 1000,
      locator: (page) => page.locator('img[alt="sb"]'),
    },
    {
      label: 'role-button-sber-id',
      timeoutMs: 800,
      locator: (page) => page.getByRole('button', { name: /sber\s*id|сбер\s*id|сберid/i }),
    },
    {
      label: 'role-link-sber-id',
      timeoutMs: 800,
      locator: (page) => page.getByRole('link', { name: /sber\s*id|сбер\s*id|сберid/i }),
    },
    {
      label: 'text-sber-id',
      timeoutMs: 800,
      locator: (page) => page.getByText(/sber\s*id|сбер\s*id|сберid/i),
    },
    {
      label: 'css-sber-id',
      timeoutMs: 1000,
      locator: (page) =>
        page.locator(
          'button:has-text("Sber ID"), a:has-text("Sber ID"), button:has-text("Сбер"), a:has-text("Сбер"), [aria-label*="Sber"], [aria-label*="Сбер"], a[href*="sber"], button[data-testid*="sber"], [data-testid*="sber"], button:has(img[alt="sb"]), img[alt="sb"]',
        ),
    },
  ];
}

export function sberIdTargetLabels(): string[] {
  return sberIdTargets().map((target) => target.label);
}

function authEntryReadyTargets(): ClickTarget[] {
  return [
    ...otherWaysTargets(),
    {
      label: 'login-email-input',
      locator: (page) => page.locator('input[name="email"], input[name="login"], input[type="email"]').first(),
    },
    { label: 'login-text-input', locator: (page) => page.locator('input[type="text"]').first() },
    ...loginTargets(),
  ];
}

async function main(): Promise<void> {
  const endpoint = arg('--endpoint');
  const startUrl = arg('--start-url');
  const storageStatePath = arg('--storage-state-path');
  const outputPath = arg('--output-path');
  const traceDir = dirname(outputPath);
  mkdirSync(traceDir, { recursive: true });
  const tracePath = join(traceDir, 'auth-script-litres-trace.jsonl');

  const targetHost = hostFromUrl(startUrl);
  if (!targetHost) {
    save(outputPath, {
      status: 'failed',
      reason_code: 'auth_failed_invalid_session',
      message: `Некорректный start_url: ${startUrl}`,
      artifacts: { start_url: startUrl },
    });
    return;
  }

  let storageState: unknown = null;
  try {
    storageState = JSON.parse(readFileSync(storageStatePath, 'utf-8'));
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'storage_state_loaded',
      details: cookieSummary(storageState),
    });
  } catch (error) {
    save(outputPath, {
      status: 'failed',
      reason_code: 'auth_failed_payload',
      message: `Не удалось прочитать storageState: ${String(error)}`,
      artifacts: { storage_state_path: storageStatePath },
    });
    return;
  }

  let browser;
  let context;
  let createdContext = false;
  let keepAuthContext = false;
  try {
    browser = await chromium.connectOverCDP(endpoint);
    const existingContexts = browser.contexts();
    context = existingContexts[0];
    if (!context) {
      context = await browser.newContext({
        viewport: { width: 1440, height: 900 },
      });
      createdContext = true;
    }
    const contextsToClose = existingContexts.length > 1 ? existingContexts.slice(1) : [];
    for (const existingContext of contextsToClose) {
      await existingContext.close().catch(() => undefined);
    }
    const cookiesLoaded = cookieCount(storageState);
    await context.addCookies(storageCookies(storageState));
    const existingPages = context.pages();
    const page = existingPages[0] ?? (await context.newPage());
    for (const extraPage of existingPages.slice(1)) {
      await extraPage.close().catch(() => undefined);
    }
    await page.setViewportSize({ width: 1440, height: 900 }).catch(() => undefined);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'browser_context_prepared',
      details: {
        reused_existing_context: !createdContext,
        existing_contexts: existingContexts.length,
        closed_extra_contexts: contextsToClose.length,
        existing_pages: existingPages.length,
        closed_extra_pages: Math.max(existingPages.length - 1, 0),
        cookies_added: cookiesLoaded,
      },
    });

    let sberLoops = 0;
    page.on('framenavigated', (frame) => {
      if (frame !== page.mainFrame()) {
        return;
      }
      if (isSberIdHost(hostFromUrl(frame.url()))) {
        sberLoops += 1;
      }
    });

    const entryUrl = authEntryUrl(startUrl);
    const existingAuth = await verifyCurrentOrProfilePage(page, startUrl, tracePath);
    if (existingAuth.verified) {
      const currentUrl = page.url();
      const currentHost = hostFromUrl(currentUrl);
      save(outputPath, {
        status: 'completed',
        reason_code: 'auth_ok',
        message: 'SberId-сессия Litres уже активна; повторный вход пропущен.',
        artifacts: {
          script: 'litres',
          final_url: currentUrl,
          final_host: currentHost,
          auth_entry_url: entryUrl,
          cookies_loaded: cookiesLoaded,
          trace_path: tracePath,
          sber_loops: sberLoops,
          already_authenticated: true,
          already_authenticated_diagnostic: {
            callback_seen: existingAuth.callback_seen,
            markers: existingAuth.markers,
            login_form_seen: existingAuth.login_form_seen,
          },
          auth_verified: true,
          auth_verified_url: currentUrl,
          auth_markers: existingAuth.markers,
          context_prepared_for_reuse: true,
        },
      });
      keepAuthContext = true;
      return;
    }

    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'goto_auth_entry',
      url: page.url(),
      host: hostFromUrl(page.url()),
      details: {
        start_url: startUrl,
        auth_entry_url: entryUrl,
      },
    });
    await page.goto(entryUrl, { waitUntil: 'domcontentloaded', timeout: 20000 });
    const entryReadyStartedAt = Date.now();
    const entryReadyTarget = await waitForFirstVisible(page, authEntryReadyTargets(), 2500);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_entry_ready',
      url: page.url(),
      host: hostFromUrl(page.url()),
      details: {
        matched_target: entryReadyTarget,
        ready: Boolean(entryReadyTarget),
        timeout_ms: 2500,
        elapsed_ms: Date.now() - entryReadyStartedAt,
      },
    });
    await clickOptionalChrome(page);
    await tracePage(page, tracePath, 'after_auth_entry_url', {
      start_url: startUrl,
      auth_entry_url: entryUrl,
      cookies_loaded: cookiesLoaded,
    });

    let loginClickLabel = 'direct-auth-login-url';
    let otherWaysClick: ClickResult | null = null;
    let sberVisibleTarget = await waitForFirstVisible(page, sberIdTargets(), 1200);
    if (!sberVisibleTarget) {
      otherWaysClick = await clickFirstVisible(page, otherWaysTargets(), { timeoutMs: 4000 });
    }
    if (!sberVisibleTarget && !otherWaysClick) {
      const loginClick = await clickFirstVisible(page, loginTargets(), { timeoutMs: 4000 });
      if (!loginClick) {
        const currentUrl = page.url();
        const currentHost = hostFromUrl(currentUrl);
        save(outputPath, {
          status: 'failed',
          reason_code: 'auth_failed_invalid_session',
          message: 'Не найдена кнопка входа на Litres после загрузки cookies.',
          artifacts: {
            script: 'litres',
            final_url: currentUrl,
            final_host: currentHost,
            expected_host: targetHost,
            auth_entry_url: entryUrl,
            cookies_loaded: cookiesLoaded,
            trace_path: tracePath,
            sber_loops: sberLoops,
            context_prepared_for_reuse: false,
          },
        });
        return;
      }

      loginClickLabel = loginClick.label;
      await page.waitForLoadState('domcontentloaded', { timeout: 5000 }).catch(() => undefined);
      await page.waitForTimeout(700);
      await tracePage(page, tracePath, 'after_login_click', { login_click: loginClickLabel });

      otherWaysClick = await clickFirstVisible(page, otherWaysTargets(), { timeoutMs: 4000 });
    }
    if (!sberVisibleTarget && !otherWaysClick) {
      const currentUrl = page.url();
      const currentHost = hostFromUrl(currentUrl);
      save(outputPath, {
        status: 'failed',
        reason_code: 'auth_failed_invalid_session',
        message: 'Не найдена кнопка "Другие способы" в форме входа Litres.',
        artifacts: {
          script: 'litres',
          final_url: currentUrl,
          final_host: currentHost,
          expected_host: targetHost,
          auth_entry_url: entryUrl,
          cookies_loaded: cookiesLoaded,
          login_click: loginClickLabel,
          trace_path: tracePath,
          sber_loops: sberLoops,
          context_prepared_for_reuse: false,
        },
      });
      return;
    }

    if (otherWaysClick) {
      await page.waitForLoadState('domcontentloaded', { timeout: 5000 }).catch(() => undefined);
      await page.waitForTimeout(700);
      await tracePage(page, tracePath, 'after_other_ways_click', {
        login_click: loginClickLabel,
        other_ways_click: otherWaysClick.label,
      });
      sberVisibleTarget = await waitForFirstVisible(page, sberIdTargets(), 2000);
    }

    const sberClickStartedAt = Date.now();
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'sber_click_started',
      url: page.url(),
      host: hostFromUrl(page.url()),
      details: {
        target_labels: sberIdTargetLabels(),
        matched_before_click: sberVisibleTarget,
        default_timeout_ms: 1200,
        popup_timeout_ms: 1200,
      },
    });
    const sberClick = await clickFirstVisible(page, sberIdTargets(), {
      timeoutMs: 1200,
      popupContext: context,
      popupTimeoutMs: 1200,
      tracePath,
    });
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'sber_click_finished',
      url: page.url(),
      host: hostFromUrl(page.url()),
      details: {
        success: Boolean(sberClick),
        selected_label: sberClick?.label ?? null,
        elapsed_ms: Date.now() - sberClickStartedAt,
      },
    });
    if (!sberClick) {
      const currentUrl = page.url();
      const currentHost = hostFromUrl(currentUrl);
      save(outputPath, {
        status: 'failed',
        reason_code: 'auth_failed_invalid_session',
        message: 'Не найдена кнопка Sber ID после выбора других способов входа.',
        artifacts: {
          script: 'litres',
          final_url: currentUrl,
          final_host: currentHost,
          expected_host: targetHost,
          auth_entry_url: entryUrl,
          cookies_loaded: cookiesLoaded,
          login_click: loginClickLabel,
          other_ways_click: otherWaysClick?.label ?? null,
          trace_path: tracePath,
          sber_loops: sberLoops,
          context_prepared_for_reuse: false,
        },
      });
      return;
    }

    const authPage = sberClick.page;
    await tracePage(authPage, tracePath, 'after_sber_id_click', {
      login_click: loginClickLabel,
      other_ways_click: otherWaysClick?.label ?? null,
      sber_id_click: sberClick.label,
    });
    if (authPage !== page) {
      authPage.on('framenavigated', (frame) => {
        if (frame !== authPage.mainFrame()) {
          return;
        }
        if (isSberIdHost(hostFromUrl(frame.url()))) {
          sberLoops += 1;
        }
      });
    }

    const deadline = Date.now() + 45000;
    let finalUrl = authPage.url();
    let finalHost = hostFromUrl(finalUrl);
    let callbackSeen = false;
    while (Date.now() < deadline) {
      await authPage.waitForLoadState('domcontentloaded', { timeout: 3000 }).catch(() => undefined);
      finalUrl = authPage.url();
      finalHost = hostFromUrl(finalUrl);
      if (isLitresAuthCallbackUrl(finalUrl)) {
        callbackSeen = true;
        await authPage.waitForTimeout(800);
        continue;
      }

      if (isSameOrSubdomain(finalHost, targetHost) && (sberLoops > 0 || finalUrl !== startUrl)) {
        const verification = await verifyCurrentOrProfilePage(authPage, startUrl, tracePath);
        finalUrl = authPage.url();
        finalHost = hostFromUrl(finalUrl);
        if (!verification.verified) {
          save(outputPath, {
            status: 'failed',
            reason_code: 'auth_failed_invalid_session',
            message: 'SberId callback вернулся на Litres, но залогиненное состояние не подтвердилось.',
            artifacts: {
              script: 'litres',
              final_url: finalUrl,
              final_host: finalHost,
              auth_entry_url: entryUrl,
              cookies_loaded: cookiesLoaded,
              login_click: loginClickLabel,
              other_ways_click: otherWaysClick?.label ?? null,
              sber_id_click: sberClick.label,
              trace_path: tracePath,
              sber_loops: sberLoops,
              callback_seen: callbackSeen || verification.callback_seen,
              auth_verified: false,
              auth_verified_url: finalUrl,
              auth_markers: verification.markers,
              login_form_seen: verification.login_form_seen,
              context_prepared_for_reuse: false,
            },
          });
          return;
        }
        save(outputPath, {
          status: 'completed',
          reason_code: 'auth_ok',
          message: 'SberId-сессия восстановлена скриптом litres через форму входа.',
          artifacts: {
            script: 'litres',
            final_url: finalUrl,
            final_host: finalHost,
            auth_entry_url: entryUrl,
            cookies_loaded: cookiesLoaded,
            login_click: loginClickLabel,
            other_ways_click: otherWaysClick?.label ?? null,
            sber_id_click: sberClick.label,
            trace_path: tracePath,
            sber_loops: sberLoops,
            callback_seen: callbackSeen || verification.callback_seen,
            auth_verified: true,
            auth_verified_url: finalUrl,
            auth_markers: verification.markers,
            context_prepared_for_reuse: true,
          },
        });
        keepAuthContext = true;
        return;
      }

      if (sberLoops > 2) {
        save(outputPath, {
          status: 'failed',
          reason_code: 'auth_failed_redirect_loop',
          message: 'Обнаружен redirect loop на id.sber.ru.',
          artifacts: {
            script: 'litres',
            final_url: finalUrl,
            final_host: finalHost,
            auth_entry_url: entryUrl,
            cookies_loaded: cookiesLoaded,
            login_click: loginClickLabel,
            other_ways_click: otherWaysClick?.label ?? null,
            sber_id_click: sberClick.label,
            trace_path: tracePath,
            sber_loops: sberLoops,
            context_prepared_for_reuse: false,
          },
        });
        return;
      }

      await authPage.waitForTimeout(800);
    }

    save(outputPath, {
      status: 'failed',
      reason_code: 'auth_failed_invalid_session',
      message: 'Sber ID не вернул браузер на Litres в авторизованном состоянии.',
      artifacts: {
        script: 'litres',
        final_url: finalUrl,
        final_host: finalHost,
        expected_host: targetHost,
        auth_entry_url: entryUrl,
        cookies_loaded: cookiesLoaded,
        login_click: loginClickLabel,
        other_ways_click: otherWaysClick?.label ?? null,
        sber_id_click: sberClick.label,
        trace_path: tracePath,
        sber_loops: sberLoops,
        callback_seen: callbackSeen,
        auth_verified: false,
        auth_verified_url: finalUrl,
        auth_markers: [],
        context_prepared_for_reuse: false,
      },
    });
  } catch (error) {
    save(outputPath, {
      status: 'failed',
      reason_code: 'auth_failed_invalid_session',
      message: `Сбой выполнения SberId-скрипта litres: ${String(error)}`,
      artifacts: {
        script: 'litres',
        endpoint,
        context_prepared_for_reuse: false,
      },
    });
  } finally {
    if (context && createdContext && !keepAuthContext) {
      await context.close().catch(() => undefined);
    }
    if (browser) {
      await browser.close().catch(() => undefined);
    }
  }
}

function isDirectRun(): boolean {
  const currentPath = fileURLToPath(import.meta.url);
  return process.argv[1] ? resolve(process.argv[1]) === currentPath : false;
}

if (isDirectRun()) {
  main().catch((error) => {
    const outputPathIndex = process.argv.indexOf('--output-path');
    const outputPath = outputPathIndex >= 0 ? process.argv[outputPathIndex + 1] : '';
    const payload: ScriptResult = {
      status: 'failed',
      reason_code: 'auth_failed_invalid_session',
      message: `Непредвиденный сбой скрипта litres: ${String(error)}`,
      artifacts: {
        script: 'litres',
      },
    };
    if (outputPath) {
      save(outputPath, payload);
      return;
    }
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  });
}
