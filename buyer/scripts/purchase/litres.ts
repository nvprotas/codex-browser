import { chromium, type Locator, type Page } from 'playwright-core';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, URL } from 'node:url';

type ScriptStatus = 'completed' | 'failed';

type ScriptResult = {
  status: ScriptStatus;
  reason_code: string;
  message: string;
  order_id: string | null;
  artifacts: Record<string, unknown>;
};

type TraceEvent = {
  ts: string;
  event: string;
  url?: string;
  details?: Record<string, unknown>;
};

type BookCandidate = {
  href: string;
  text: string;
  title: string;
  score: number;
};

function arg(name: string): string {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    throw new Error(`Missing argument: ${name}`);
  }
  return process.argv[index + 1]!;
}

function optionalArg(name: string): string | null {
  const index = process.argv.indexOf(name);
  if (index < 0 || index + 1 >= process.argv.length) {
    return null;
  }
  return process.argv[index + 1]!;
}

export function extractLitresQuery(task: string): string | null {
  const compact = task.replace(/\s+/g, ' ').trim();
  const patterns = [
    /(?:^|[.!?]\s*)(?:ищи|найди|поищи)\s+(?:книгу|книжку|произведение)?\s*([^.!?]+)/i,
    /(?:^|[.!?]\s*)(?:купить|купи|открой|добавь)\s+(?:книгу|книжку|произведение)\s+([^.!?]+)/i,
  ];
  for (const pattern of patterns) {
    const match = compact.match(pattern);
    const candidate = cleanupQuery(match?.[1] || '');
    if (candidate) {
      return candidate;
    }
  }
  return null;
}

export function parseOrderId(rawUrl: string): string | null {
  try {
    const url = new URL(rawUrl);
    const order = url.searchParams.get('order');
    return order && order.trim() ? order.trim() : null;
  } catch {
    return null;
  }
}

function cleanupQuery(value: string): string | null {
  const cleaned = value
    .replace(/[«»"']/g, '')
    .replace(/\b(?:без\s+реального\s+платежа|до\s+шага\s+оплаты|через\s+sberpay|через\s+сбер(?:пэй|pay)?)\b.*$/i, '')
    .replace(/\s+/g, ' ')
    .trim();
  return cleaned.length >= 2 ? cleaned : null;
}

function normalizeTokens(value: string): string[] {
  const stopWords = new Set(['книга', 'книгу', 'книжку', 'автор', 'автора']);
  return value
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s-]+/gu, ' ')
    .split(/\s+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 1 && !stopWords.has(item));
}

function tokenVariants(token: string): string[] {
  const variants = new Set([token]);
  if (token.length > 4) {
    variants.add(token.replace(/[аяуыои]$/u, ''));
  }
  return [...variants].filter((item) => item.length > 1);
}

export function scoreBookCandidate(query: string, href: string, text: string, title: string): number {
  const tokens = normalizeTokens(query);
  const haystack = `${href} ${text} ${title}`.toLowerCase();
  let score = 0;
  for (const token of tokens) {
    if (tokenVariants(token).some((variant) => haystack.includes(variant))) {
      score += 10;
    }
  }
  if (/\/book\/[^/]+\/[^/]+-\d+\/?$/i.test(href)) {
    score += 5;
  }
  if (/одиссея/i.test(haystack) && /гомер/i.test(haystack)) {
    score += 12;
  }
  return score;
}

export function isSberPaymentUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    const method = (url.searchParams.get('method') || '').toLowerCase();
    const system = (url.searchParams.get('system') || '').toLowerCase();
    return method === 'sbp' || method.includes('sber') || system.includes('sber');
  } catch {
    return false;
  }
}

export function cartRowsMatchQuery(query: string, rows: string[]): boolean {
  const tokens = normalizeTokens(query);
  const uniqueRows = [...new Set(rows.map((row) => row.replace(/\s+/g, ' ').trim()).filter(Boolean))];
  if (tokens.length === 0 || uniqueRows.length !== 1) {
    return false;
  }
  const haystack = uniqueRows[0]!.toLowerCase();
  return tokens.every((token) => tokenVariants(token).some((variant) => haystack.includes(variant)));
}

function save(path: string, payload: ScriptResult): void {
  writeFileSync(path, JSON.stringify(payload, null, 2), { encoding: 'utf-8' });
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

function appendTrace(path: string, event: TraceEvent): void {
  writeFileSync(path, `${JSON.stringify(event)}\n`, { encoding: 'utf-8', flag: 'a' });
}

function fail(outputPath: string, reasonCode: string, message: string, artifacts: Record<string, unknown>): void {
  save(outputPath, {
    status: 'failed',
    reason_code: reasonCode,
    message,
    order_id: null,
    artifacts,
  });
}

function litresOrigin(startUrl: string): string {
  const parsed = new URL(startUrl);
  return `${parsed.protocol}//${parsed.host}`;
}

async function clickFirstVisible(targets: Locator[], timeoutMs: number): Promise<boolean> {
  for (const target of targets) {
    try {
      const locator = target.first();
      await locator.waitFor({ state: 'visible', timeout: timeoutMs });
      await locator.click({ timeout: timeoutMs });
      return true;
    } catch {
      continue;
    }
  }
  return false;
}

async function collectBookCandidates(page: Page, query: string, limit = 60): Promise<BookCandidate[]> {
  const rawCandidates = await page.locator('a[href*="/book/"]').evaluateAll(
    `(nodes, limit) => {
      const compact = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
      const result = [];
      const seen = new Set();
      for (const node of nodes) {
        if (result.length >= limit) break;
        const href = node.href || node.getAttribute('href') || '';
        if (!href || seen.has(href)) continue;
        seen.add(href);
        result.push({
          href,
          text: compact(node.innerText || node.textContent || '').slice(0, 500),
          title: compact(node.getAttribute('title') || '').slice(0, 300),
        });
      }
      return result;
    }`,
    limit,
  ) as Array<{ href: string; text: string; title: string }>;

  return rawCandidates
    .map((candidate) => ({
      ...candidate,
      score: scoreBookCandidate(query, candidate.href, candidate.text, candidate.title),
    }))
    .filter((candidate) => candidate.score > 0)
    .sort((left, right) => right.score - left.score);
}

async function collectCartRows(page: Page): Promise<string[]> {
  const titles = await page.locator('[data-testid="art__title"]').allTextContents().catch(() => []);
  const authors = await page.locator('[data-testid="art__authorName"]').allTextContents().catch(() => []);
  return titles.map((title, index) => `${title} ${authors[index] || ''}`.replace(/\s+/g, ' ').trim()).filter(Boolean);
}

async function hasSelectedSberPayment(page: Page): Promise<boolean> {
  if (isSberPaymentUrl(page.url())) {
    return true;
  }
  const checkedCount = await page
    .locator('#payment-method-input-sbp:checked, input[name="selectedPaymentMethodId"][value*="sbp"]:checked')
    .count()
    .catch(() => 0);
  return checkedCount > 0;
}

async function selectSberPayment(page: Page): Promise<boolean> {
  if (await hasSelectedSberPayment(page)) {
    return true;
  }

  await clickFirstVisible(
    [
      page.locator('label[for="payment-method-input-sbp"]'),
      page.locator('[data-testid="payment__method--sbp"] label'),
      page.locator('#payment-method-input-sbp'),
      page.getByRole('radio', { name: /сбп|sber|сбер/i }),
      page.locator('label:has-text("СБП"), label:has-text("Sber"), label:has-text("Сбер")'),
    ],
    3000,
  );
  await page.waitForTimeout(500).catch(() => undefined);
  return hasSelectedSberPayment(page);
}

async function main(): Promise<void> {
  const endpoint = arg('--endpoint');
  const startUrl = arg('--start-url');
  const task = arg('--task');
  const outputPath = arg('--output-path');
  const tracePath = optionalArg('--trace-path') || `${dirname(outputPath)}/purchase-script-litres-trace.jsonl`;
  mkdirSync(dirname(outputPath), { recursive: true });
  mkdirSync(dirname(tracePath), { recursive: true });

  const query = extractLitresQuery(task);
  appendTrace(tracePath, {
    ts: new Date().toISOString(),
    event: 'query_extracted',
    details: { task, query },
  });
  if (!query) {
    fail(outputPath, 'purchase_script_query_missing', 'Не удалось извлечь поисковый запрос для Litres.', {
      script: 'litres',
      trace_path: tracePath,
    });
    return;
  }

  let browser;
  try {
    browser = await chromium.connectOverCDP(endpoint);
    const context = browser.contexts()[0] ?? (await browser.newContext({ viewport: { width: 1440, height: 900 } }));
    const page = context.pages()[0] ?? (await context.newPage());
    await page.setViewportSize({ width: 1440, height: 900 }).catch(() => undefined);

    const origin = litresOrigin(startUrl);
    const searchUrl = `${origin}/search/?q=${encodeURIComponent(query)}`;
    await page.goto(searchUrl, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => undefined);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'search_loaded',
      url: page.url(),
      details: { query },
    });

    const candidates = await collectBookCandidates(page, query);
    const selected = candidates[0];
    if (!selected) {
      fail(outputPath, 'purchase_script_no_candidates', 'Не удалось найти релевантную книгу на Litres.', {
        script: 'litres',
        query,
        search_url: searchUrl,
        trace_path: tracePath,
      });
      return;
    }
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'candidate_selected',
      url: page.url(),
      details: {
        query,
        selected,
        candidates: candidates.slice(0, 5),
      },
    });

    await page.goto(selected.href, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => undefined);
    const productUrl = page.url();
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'product_loaded',
      url: productUrl,
      details: { selected_title: selected.text || selected.title },
    });

    const addToCartClicked = await clickFirstVisible(
      [
        page.locator('[data-testid="book__addToCartButton"]'),
        page.getByRole('button', { name: /купить|в корзину|добавить/i }),
        page.locator('button:has-text("Купить"), button:has-text("В корзину"), button:has-text("Добавить")'),
      ],
      5000,
    );
    if (!addToCartClicked) {
      fail(outputPath, 'purchase_script_add_to_cart_missing', 'Не найдена кнопка добавления книги в корзину.', {
        script: 'litres',
        query,
        product_url: productUrl,
        trace_path: tracePath,
      });
      return;
    }
    await page.waitForTimeout(700);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'added_to_cart',
      url: page.url(),
      details: { product_url: productUrl },
    });

    const cartUrl = `${origin}/my-books/cart/`;
    await page.goto(cartUrl, { waitUntil: 'domcontentloaded', timeout: 20000 });
    await page.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => undefined);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'cart_loaded',
      url: page.url(),
      details: { cart_url: cartUrl },
    });

    const cartRows = await collectCartRows(page);
    if (!cartRowsMatchQuery(query, cartRows)) {
      fail(outputPath, 'purchase_script_cart_ambiguous', 'Корзина Litres не содержит ровно одну целевую книгу.', {
        script: 'litres',
        query,
        product_url: productUrl,
        cart_url: page.url(),
        cart_rows: cartRows,
        trace_path: tracePath,
      });
      return;
    }

    const checkoutClicked = await clickFirstVisible(
      [
        page.getByRole('button', { name: /оформить|перейти к оплате|оплатить/i }),
        page.getByRole('link', { name: /оформить|перейти к оплате|оплатить/i }),
        page.locator('button:has-text("Оформить"), button:has-text("Перейти к оплате"), a:has-text("Оформить")'),
      ],
      5000,
    );
    if (!checkoutClicked) {
      fail(outputPath, 'purchase_script_checkout_missing', 'Не найдена кнопка перехода к оплате в корзине.', {
        script: 'litres',
        query,
        product_url: productUrl,
        cart_url: page.url(),
        trace_path: tracePath,
      });
      return;
    }

    await page.waitForURL(/\/purchase\/ppd\//, { timeout: 15000 }).catch(() => undefined);
    if (!(await selectSberPayment(page))) {
      fail(outputPath, 'purchase_script_sber_payment_missing', 'Страница оплаты открыта, но способ Sber/SBP не выбран.', {
        script: 'litres',
        query,
        product_url: productUrl,
        final_url: page.url(),
        trace_path: tracePath,
      });
      return;
    }
    const finalUrl = page.url();
    const orderId = parseOrderId(finalUrl);
    appendTrace(tracePath, {
      ts: new Date().toISOString(),
      event: 'payment_ready',
      url: finalUrl,
      details: { order_id: orderId, sber_payment: true },
    });
    if (!orderId) {
      fail(outputPath, 'purchase_script_order_missing', 'Страница оплаты открыта, но orderId не найден.', {
        script: 'litres',
        query,
        product_url: productUrl,
        final_url: finalUrl,
        trace_path: tracePath,
      });
      return;
    }

    save(outputPath, {
      status: 'completed',
      reason_code: 'purchase_ready',
      message: 'Litres purchase-скрипт дошел до шага оплаты без выполнения платежа.',
      order_id: orderId,
      artifacts: {
        script: 'litres',
        query,
        final_url: finalUrl,
        product_url: productUrl,
        selected_title: selected.text || selected.title,
        trace_path: tracePath,
      },
    });
  } catch (error) {
    fail(outputPath, 'purchase_script_failed', `Сбой выполнения Litres purchase-скрипта: ${String(error)}`, {
      script: 'litres',
      query,
      trace_path: tracePath,
    });
  } finally {
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
    const outputPath = optionalArg('--output-path');
    const payload: ScriptResult = {
      status: 'failed',
      reason_code: 'purchase_script_unhandled',
      message: `Непредвиденный сбой Litres purchase-скрипта: ${String(error)}`,
      order_id: null,
      artifacts: { script: 'litres' },
    };
    if (outputPath) {
      save(outputPath, payload);
      return;
    }
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  });
}
