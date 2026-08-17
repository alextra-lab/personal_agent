/**
 * Playwright e2e tests for the fixed-shell layout (FRE-1266): the document
 * itself must never scroll — only the message list does, between a header
 * and composer that stay pinned. jsdom (the vitest unit-test environment)
 * doesn't compute box sizes or layout overflow, so these geometry ACs can
 * only be proven against a real rendered page.
 *
 * AC-1 and AC-4 are the discriminating pair: both regressed on `main` before
 * this fix. The regression had three contributing causes, only two of which
 * the ticket named: missing `min-h-0` on the scrolling `<main>`; a 3-row-tall
 * composer; and (found via these tests' own seeded-negative failures, not
 * named in the ticket) `<main>` being `position: static` while its parent is
 * `position: relative`, letting `position: absolute` sr-only descendants
 * escape the scroll clip and inflate the *document's* scrollHeight. See the
 * FRE-1266 ticket handoff comment for the seeded-negative pre-fix values
 * these tests recorded against the pre-fix code.
 */

import { test, expect } from '@playwright/test';
import { TEST_SESSION, stubWebSocket } from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;
const BACKEND = 'http://localhost:9000';

// Long enough, and enough of them, to guarantee the message list overflows
// a 390x844 viewport regardless of theme/typography.
const LONG_TEXT =
  'This is a long assistant reply used to force the message list to overflow the viewport height so the shell-scroll acceptance criteria can be exercised end to end. '.repeat(
    3,
  );

async function stubLongHistory(page: import('@playwright/test').Page): Promise<void> {
  const now = new Date().toISOString();
  const messages: { role: string; content: string; timestamp: string; trace_id?: string }[] = [];
  for (let i = 0; i < 20; i++) {
    messages.push({ role: 'user', content: `Question ${i}`, timestamp: now });
    messages.push({ role: 'assistant', content: LONG_TEXT, timestamp: now, trace_id: `trace-${i}` });
  }
  await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}/messages*`, (route) =>
    route.fulfill({ json: messages }),
  );
  await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}`, (route) =>
    route.fulfill({ status: 404, body: 'not found' }),
  );
  await page.route(`${BACKEND}/chat/stream`, (route) => route.fulfill({ status: 200, body: '' }));
}

test.describe('Shell viewport containment (FRE-1266)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('AC-1: the document never scrolls', async ({ page }) => {
    await stubLongHistory(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);
    await page.locator('main main').first().waitFor();

    const { scrollHeight, innerHeight } = await page.evaluate(() => ({
      scrollHeight: document.documentElement.scrollHeight,
      innerHeight: window.innerHeight,
    }));
    // Fails if the document scrolls at all — that is the regression itself.
    expect(Math.abs(scrollHeight - innerHeight)).toBeLessThanOrEqual(2);
  });

  test('AC-2: only the message list scrolls; scrolling it does not move the header', async ({ page }) => {
    await stubLongHistory(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const scrollingMain = page.locator('main main');
    await scrollingMain.first().waitFor();

    const overflowY = await scrollingMain
      .first()
      .evaluate((el) => getComputedStyle(el).overflowY);
    expect(['auto', 'scroll']).toContain(overflowY);

    const { scrollHeight, clientHeight } = await scrollingMain.first().evaluate((el) => ({
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
    }));
    expect(scrollHeight).toBeGreaterThan(clientHeight);

    const header = page.locator('header');
    const before = await header.boundingBox();
    await scrollingMain.first().evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    const after = await header.boundingBox();
    expect(after!.y).toBe(before!.y);
  });

  test('AC-3: the header stays put after scrolling the message list to the bottom', async ({ page }) => {
    await stubLongHistory(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const scrollingMain = page.locator('main main');
    await scrollingMain.first().waitFor();
    const header = page.locator('header');

    const before = await header.boundingBox();
    await scrollingMain.first().evaluate((el) => {
      el.scrollTop = el.scrollHeight;
    });
    const after = await header.boundingBox();
    expect(after!.y).toBe(before!.y);
  });

  test('AC-4: the composer is pinned to the bottom of the viewport', async ({ page }) => {
    await stubLongHistory(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    // The footer is the pinned region. FRE-1267 moved the safe-area-inset
    // clearance from the <form>'s own paddingBottom into the
    // `composer-container` card itself, so the card's fill now reaches the
    // true viewport bottom instead of floating above it — see
    // composer-safe-area.spec.ts for that geometry. What this test still
    // guards is that the *footer itself* never floats away from the true
    // viewport bottom, which is what the owner originally circled.
    const footer = page.locator('footer');
    await footer.waitFor();
    // The mount-time auto-scroll-to-latest-message runs `{behavior: 'smooth'}`
    // — measuring immediately races that animation and can read a stale,
    // still-mid-flight position. Wait for it to settle so this reflects what
    // the user actually sees, not a pre-animation snapshot.
    await page.waitForTimeout(700);
    const box = await footer.boundingBox();
    expect(box).not.toBeNull();

    // env(safe-area-inset-bottom) is 0 in headless chromium, so the footer
    // bottom should land within 2px of the viewport bottom, in either
    // direction — a gap remaining (positive) is the band the owner circled;
    // overflowing past the viewport bottom (negative) would be a new defect.
    const gap = 844 - (box!.y + box!.height);
    expect(Math.abs(gap)).toBeLessThanOrEqual(2);
  });

  test('AC-5: the textarea starts at one row and grows to the 200px ceiling', async ({ page }) => {
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const textarea = page.locator('[placeholder="Message Seshat..."]');
    await textarea.waitFor();

    const lineHeightPx = await textarea.evaluate((el) => parseFloat(getComputedStyle(el).lineHeight));
    const emptyBox = await textarea.boundingBox();
    expect(emptyBox).not.toBeNull();
    // Measurably less than two lines — one line-height plus padding, not three.
    expect(emptyBox!.height).toBeLessThan(lineHeightPx * 2);

    // Long enough to wrap to ~4 lines at a 390px-wide composer.
    const longText = 'word '.repeat(80);
    await textarea.fill(longText);
    const grownBox = await textarea.boundingBox();
    expect(grownBox).not.toBeNull();
    expect(grownBox!.height).toBeGreaterThan(emptyBox!.height);

    const maxHeightPx = await textarea.evaluate((el) => parseFloat(getComputedStyle(el).maxHeight));
    expect(maxHeightPx).toBe(200);

    // Long enough to actually exceed the 200px ceiling (not just wrap a few
    // lines) — proves the cap is real and the overflow scrolls internally,
    // not just that the CSS property says 200.
    await textarea.fill('word '.repeat(400));
    const cappedBox = await textarea.boundingBox();
    expect(cappedBox).not.toBeNull();
    expect(cappedBox!.height).toBeCloseTo(200, 0);
    const { scrollHeight, clientHeight } = await textarea.evaluate((el) => ({
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
    }));
    expect(scrollHeight).toBeGreaterThan(clientHeight);
  });
});
