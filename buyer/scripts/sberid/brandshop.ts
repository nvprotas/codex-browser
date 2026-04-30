import { chromium, type BrowserContext, type Locator, type Page } from 'playwright-core';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, URL } from 'node:url';

const SCRIPT_NAME = 'brandshop';
const BRANDSHOP_DOMAIN = 'brandshop.ru';

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
  markers: string[];
  login_form_seen: boolean;
  account_url_seen: boolean;
};

type ClickTarget = {
  label: string;
  timeoutMs?: number;
  locator: (page: Page) => Locator;
};

type ClickResult = {
  label: string;
  page: Page;
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

function authVerificationUrl(startUrl: string): string {
  const url = new URL(startUrl);
  if (isSameOrSubdomain(normalizeHost(url.hostname), BRANDSHOP_DOMAIN)) {
    url.hostname = BRANDSHOP_DOMAIN;
  }
  url.pathname = '/account/';
  url.search = '';
  url.hash = '';
  return url.toString();
}

export function verifyBrandshopAuthSnapshot(rawUrl: string, bodyText: string | null | undefined): AuthVerification {
  const text = String(bodyText || '').replace(/\s+/g, ' ').trim();
  const markers: string[] = [];
  if (/Личный кабинет/iu.test(text)) {
    markers.push('Личный кабинет');
  }
  if (/Профиль/iu.test(text)) {
    markers.push('Профиль');
  }
  if (/Мои заказы|История заказов|Заказы/iu.test(text)) {
    markers.push('Мои заказы');
  }
  if (/Личные данные|Персональные данные|Мои данные|Контактные данные|Адреса доставки|Мои адреса/iu.test(text)) {
    markers.push('Личные данные');
  }
  if (/Выйти/iu.test(text)) {
    markers.push('Выйти');
  }
  if (/Logout|Log out|Sign out/iu.test(text)) {
    markers.push('Logout');
  }

  let accountUrlSeen = false;
  try {
    const url = new URL(rawUrl);
    accountUrlSeen = /\/(account|profile|personal|cabinet)(\/|$)/iu.test(url.pathname);
  } catch {
    accountUrlSeen = false;
  }

  const loginFormSeen =
    /Войти с\s+Сбер\s*ID|Войти через\s+Сбер\s*ID|Номер телефона|Получить код|Регистрация|Зарегистрироваться/iu.test(
      text,
    ) ||
    /(^|[\s,.;:!?()«»"'`-])(?:Войти|Вход|Авторизация)(?=$|[\s,.;:!?()«»"'`-])/iu.test(text) ||
    /\b(?:Login|Log in|Sign in|Sign up|Register)\b/iu.test(text);
  const hasLogout = markers.includes('Выйти') || markers.includes('Logout');
  const hasProfile = markers.includes('Профиль') || markers.includes('Личный кабинет');
  const hasOrders = markers.includes('Мои заказы');
  const hasAccountUserInfo = markers.includes('Личные данные');
  const hasStrongAuthenticatedMarker = hasOrders || hasLogout || hasAccountUserInfo;
  return {
    verified: !loginFormSeen && hasStrongAuthenticatedMarker && (hasProfile || hasLogout || hasAccountUserInfo),
    markers,
    login_form_seen: loginFormSeen,
    account_url_seen: accountUrlSeen,
  };
}

export function authEntryUrl(startUrl: string): string {
  const url = new URL(startUrl);
  const host = normalizeHost(url.hostname);
  if (!isSameOrSubdomain(host, BRANDSHOP_DOMAIN)) {
    return startUrl;
  }
  url.hostname = BRANDSHOP_DOMAIN;
  url.pathname = '/';
  url.search = '';
  url.hash = '';
  return url.toString();
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
    const item = cookie as { domain?: unknown; name?: unknown };
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
  const verification = verifyBrandshopAuthSnapshot(page.url(), bodyText);
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

async function verifyCurrentOrAccountPage(page: Page, startUrl: string, tracePath: string): Promise<AuthVerification> {
  let verification = await verifyAuthPage(page, tracePath, 'auth_verify_current');
  if (verification.verified) {
    return verification;
  }

  await page.goto(authVerificationUrl(startUrl), { waitUntil: 'domcontentloaded', timeout: 12000 }).catch(() => undefined);
  await page.waitForLoadState('networkidle', { timeout: 3000 }).catch(() => undefined);
  verification = await verifyAuthPage(page, tracePath, 'auth_verify_account');
  if (verification.verified) {
    return verification;
  }

  await page.goto(startUrl, { waitUntil: 'domcontentloaded', timeout: 12000 }).catch(() => undefined);
  await page.waitForLoadState('networkidle', { timeout: 3000 }).catch(() => undefined);
  return verifyAuthPage(page, tracePath, 'auth_verify_start_url');
}

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

function profileTargets(): ClickTarget[] {
  return [
    {
      label: 'brandshop-profile-aria',
      locator: (page) => page.locator('button[aria-label="profile"]'),
    },
    {
      label: 'brandshop-profile-header',
      locator: (page) => page.locator('.profile-header button, button.profile-header__icon'),
    },
    {
      label: 'role-button-profile',
      locator: (page) => page.getByRole('button', { name: /profile|профиль|личный кабинет|войти/i }),
    },
  ];
}

function sberIdTargets(): ClickTarget[] {
  return [
    {
      label: 'brandshop-sber-social-btn',
      timeoutMs: 1000,
      locator: (page) => page.locator('.login__social-btn_sber'),
    },
    {
      label: 'brandshop-role-button-sber-id',
      timeoutMs: 1000,
      locator: (page) => page.getByRole('button', { name: /войти с\s+сбер\s*id|sber\s*id|сбер\s*id|сберid/i }),
    },
    {
      label: 'brandshop-css-button-sber-id',
      timeoutMs: 1000,
      locator: (page) =>
        page.locator(
          'button:has-text("Войти с Сбер ID"), button:has-text("Sber ID"), button:has-text("Сбер"), [aria-label*="Sber"], [aria-label*="Сбер"], [class*="sber"]',
        ),
    },
  ];
}

export function sberIdTargetLabels(): string[] {
  return sberIdTargets().map((target) => target.label);
}

function authEntryReadyTargets(): ClickTarget[] {
  return [
    ...sberIdTargets(),
    {
      label: 'brandshop-phone-input',
      locator: (page) => page.locator('input[type="tel"], input[placeholder*="номер" i]').first(),
    },
    ...profileTargets(),
  ];
}

async function main(): Promise<void> {
  const endpoint = arg('--endpoint');
  const startUrl = arg('--start-url');
  const storageStatePath = arg('--storage-state-path');
  const outputPath = arg('--output-path');
  const traceDir = dirname(outputPath);
  mkdirSync(traceDir, { recursive: true });
  const tracePath = join(traceDir, 'auth-script-brandshop-trace.jsonl');

  const targetHost = hostFromUrl(startUrl);
  if (!targetHost) {
    save(outputPath, {
      status: 'failed',
      reason_code: 'auth_failed_invalid_session',
      message: `Некорректный start_url: ${startUrl}`,
      artifacts: { script: SCRIPT_NAME, start_url: startUrl },
    });
    return;
  }

  const expectedHost = isSameOrSubdomain(targetHost, BRANDSHOP_DOMAIN) ? BRANDSHOP_DOMAIN : targetHost;

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
      artifacts: { script: SCRIPT_NAME, storage_state_path: storageStatePath },
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
    const existingAuth = await verifyCurrentOrAccountPage(page, startUrl, tracePath);
    if (existingAuth.verified) {
      const currentUrl = page.url();
      const currentHost = hostFromUrl(currentUrl);
      save(outputPath, {
        status: 'completed',
        reason_code: 'auth_ok',
        message: 'SberId-сессия Brandshop уже активна; повторный вход пропущен.',
        artifacts: {
          script: SCRIPT_NAME,
          final_url: currentUrl,
          final_host: currentHost,
          expected_host: expectedHost,
          auth_entry_url: entryUrl,
          cookies_loaded: cookiesLoaded,
          trace_path: tracePath,
          sber_loops: sberLoops,
          already_authenticated: true,
          already_authenticated_diagnostic: {
            markers: existingAuth.markers,
            login_form_seen: existingAuth.login_form_seen,
            account_url_seen: existingAuth.account_url_seen,
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
    const entryReadyTarget = await waitForFirstVisible(page, authEntryReadyTargets(), 4000);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'auth_entry_ready',
      url: page.url(),
      host: hostFromUrl(page.url()),
      details: {
        matched_target: entryReadyTarget,
        ready: Boolean(entryReadyTarget),
        timeout_ms: 4000,
        elapsed_ms: Date.now() - entryReadyStartedAt,
      },
    });
    await clickOptionalChrome(page);
    await tracePage(page, tracePath, 'after_auth_entry_url', {
      start_url: startUrl,
      auth_entry_url: entryUrl,
      cookies_loaded: cookiesLoaded,
    });

    let profileClickLabel = 'sber-visible-at-entry';
    let sberVisibleTarget = await waitForFirstVisible(page, sberIdTargets(), 1200);
    if (!sberVisibleTarget) {
      const profileClick = await clickFirstVisible(page, profileTargets(), { timeoutMs: 5000, tracePath });
      if (!profileClick) {
        const currentUrl = page.url();
        const currentHost = hostFromUrl(currentUrl);
        save(outputPath, {
          status: 'failed',
          reason_code: 'auth_failed_invalid_session',
          message: 'Не найдена кнопка профиля Brandshop после загрузки cookies.',
          artifacts: {
            script: SCRIPT_NAME,
            final_url: currentUrl,
            final_host: currentHost,
            expected_host: expectedHost,
            auth_entry_url: entryUrl,
            cookies_loaded: cookiesLoaded,
            trace_path: tracePath,
            sber_loops: sberLoops,
            context_prepared_for_reuse: false,
          },
        });
        return;
      }

      profileClickLabel = profileClick.label;
      await page.waitForLoadState('domcontentloaded', { timeout: 5000 }).catch(() => undefined);
      await page.waitForTimeout(700);
      await tracePage(page, tracePath, 'after_profile_click', { profile_click: profileClickLabel });
      sberVisibleTarget = await waitForFirstVisible(page, sberIdTargets(), 3000);
    }

    if (!sberVisibleTarget) {
      const currentUrl = page.url();
      const currentHost = hostFromUrl(currentUrl);
      save(outputPath, {
        status: 'failed',
        reason_code: 'auth_failed_invalid_session',
        message: 'Не найдена кнопка Sber ID в форме входа Brandshop.',
        artifacts: {
          script: SCRIPT_NAME,
          final_url: currentUrl,
          final_host: currentHost,
          expected_host: expectedHost,
          auth_entry_url: entryUrl,
          cookies_loaded: cookiesLoaded,
          profile_click: profileClickLabel,
          trace_path: tracePath,
          sber_loops: sberLoops,
          context_prepared_for_reuse: false,
        },
      });
      return;
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
        message: 'Не удалось нажать кнопку Sber ID в Brandshop.',
        artifacts: {
          script: SCRIPT_NAME,
          final_url: currentUrl,
          final_host: currentHost,
          expected_host: expectedHost,
          auth_entry_url: entryUrl,
          cookies_loaded: cookiesLoaded,
          profile_click: profileClickLabel,
          trace_path: tracePath,
          sber_loops: sberLoops,
          context_prepared_for_reuse: false,
        },
      });
      return;
    }

    const authPage = sberClick.page;
    await tracePage(authPage, tracePath, 'after_sber_id_click', {
      profile_click: profileClickLabel,
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
    let lastAuthVerification: AuthVerification | null = null;
    while (Date.now() < deadline) {
      await authPage.waitForLoadState('domcontentloaded', { timeout: 3000 }).catch(() => undefined);
      finalUrl = authPage.url();
      finalHost = hostFromUrl(finalUrl);

      if (isSameOrSubdomain(finalHost, expectedHost) && (sberLoops > 0 || finalUrl !== entryUrl)) {
        const verification = await verifyCurrentOrAccountPage(authPage, startUrl, tracePath);
        finalUrl = authPage.url();
        finalHost = hostFromUrl(finalUrl);
        lastAuthVerification = verification;
        if (!verification.verified) {
          await authPage.waitForTimeout(800);
          continue;
        }

        save(outputPath, {
          status: 'completed',
          reason_code: 'auth_ok',
          message: 'SberId-сессия восстановлена скриптом brandshop через форму входа.',
          artifacts: {
            script: SCRIPT_NAME,
            final_url: finalUrl,
            final_host: finalHost,
            expected_host: expectedHost,
            auth_entry_url: entryUrl,
            cookies_loaded: cookiesLoaded,
            profile_click: profileClickLabel,
            sber_id_click: sberClick.label,
            trace_path: tracePath,
            sber_loops: sberLoops,
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
            script: SCRIPT_NAME,
            final_url: finalUrl,
            final_host: finalHost,
            expected_host: expectedHost,
            auth_entry_url: entryUrl,
            cookies_loaded: cookiesLoaded,
            profile_click: profileClickLabel,
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
      message: 'Sber ID не вернул браузер на Brandshop в авторизованном состоянии.',
      artifacts: {
        script: SCRIPT_NAME,
        final_url: finalUrl,
        final_host: finalHost,
        expected_host: expectedHost,
        auth_entry_url: entryUrl,
        cookies_loaded: cookiesLoaded,
        profile_click: profileClickLabel,
        sber_id_click: sberClick.label,
        trace_path: tracePath,
        sber_loops: sberLoops,
        auth_verified: false,
        auth_verified_url: finalUrl,
        auth_markers: lastAuthVerification?.markers ?? [],
        login_form_seen: lastAuthVerification?.login_form_seen ?? false,
        context_prepared_for_reuse: false,
      },
    });
  } catch (error) {
    save(outputPath, {
      status: 'failed',
      reason_code: 'auth_failed_invalid_session',
      message: `Сбой выполнения SberId-скрипта brandshop: ${String(error)}`,
      artifacts: {
        script: SCRIPT_NAME,
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
      message: `Непредвиденный сбой скрипта brandshop: ${String(error)}`,
      artifacts: {
        script: SCRIPT_NAME,
      },
    };
    if (outputPath) {
      save(outputPath, payload);
      return;
    }
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  });
}
