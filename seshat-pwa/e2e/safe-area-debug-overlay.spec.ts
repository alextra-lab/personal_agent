/**
 * Playwright e2e tests for the temporary FRE-1269 on-device diagnostic
 * overlay (SafeAreaDebugOverlay). Headless Chromium can't reproduce the real
 * iOS standalone-mode bug (see composer-safe-area.spec.ts's header comment),
 * so this overlay exists to let the owner capture real numbers from their
 * own device. These tests only guard: (a) it stays invisible by default —
 * no accidental UX regression — (b) it renders the expected measurement
 * rows when explicitly requested, (c) the round-10 keyboard-pan-fix toggle
 * applies/reverts its candidate style changes correctly, and (d) nothing in
 * the overlay ever blocks interaction with whatever's underneath it (a
 * since-retired round-7 toggle button's hit-box once overlapped and stole
 * taps from the header's hamburger control — that's why every interactive
 * element here gets its own obstruction check, not just the header title).
 */

import { test, expect } from '@playwright/test';
import { TEST_SESSION, stubRest, stubWebSocket } from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;

test.describe('SafeAreaDebugOverlay (FRE-1269, temporary diagnostic)', () => {
  test('is absent by default', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    await expect(page.getByTestId('safe-area-debug-overlay')).toHaveCount(0);
  });

  test('renders the measurement rows when ?debug=safearea is present', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    const overlay = page.getByTestId('safe-area-debug-overlay');
    await overlay.waitFor();
    const text = await overlay.innerText();

    expect(text).toContain('innerHeight');
    expect(text).toContain('--safe-bottom');
    expect(text).toContain('composer bottom');
    expect(text).toContain('footer bottom');
    expect(text).toContain('100dvh probe');
    expect(text).toContain('display-mode: standalone');
    expect(text).toContain('--safe-top');
    expect(text).toContain('scrollY');
    expect(text).toContain('html.scrollHeight');
    expect(text).toContain('visualViewport.offsetTop');
    expect(text).toContain('visualViewport.pageTop');
    expect(text).toContain('activeElement');
  });

  test('does not block the hamburger menu button while open', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    await page.getByTestId('safe-area-debug-overlay').waitFor();
    // No force: true — a real, unobstructed click is the whole point.
    await page.getByRole('button', { name: 'Open session list' }).click();
    await expect(page.getByText('Artifacts')).toBeVisible();
  });

  test('the keyboard-pan-fix toggle applies and reverts overflow + height overrides', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    await page.getByTestId('safe-area-debug-overlay').waitFor();

    const before = await page.evaluate(() => ({
      overflow: document.documentElement.style.overflow,
      height: document.documentElement.style.height,
    }));
    expect(before.overflow).toBe('');
    expect(before.height).toBe('');

    const toggle = page.getByTestId('safe-area-keyboard-fix-toggle');
    await toggle.click();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.style.overflow))
      .toBe('hidden');
    const bodyOverflowOn = await page.evaluate(() => document.body.style.overflow);
    expect(bodyOverflowOn).toBe('hidden');

    await toggle.click();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.style.overflow))
      .toBe('');
    const bodyOverflowOff = await page.evaluate(() => document.body.style.overflow);
    expect(bodyOverflowOff).toBe('');
  });

  test('the keyboard-pan-fix toggle button does not block the hamburger menu button', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    await page.getByTestId('safe-area-debug-overlay').waitFor();
    await page.getByTestId('safe-area-keyboard-fix-toggle').click();

    // No force: true — a real, unobstructed click is the whole point.
    await page.getByRole('button', { name: 'Open session list' }).click();
    await expect(page.getByText('Artifacts')).toBeVisible();
  });

  test('closing the overlay reverts an active keyboard-pan fix', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    const overlay = page.getByTestId('safe-area-debug-overlay');
    await overlay.waitFor();
    await page.getByTestId('safe-area-keyboard-fix-toggle').click();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.style.overflow))
      .toBe('hidden');

    const title = page.getByTestId('header-title');
    for (let i = 0; i < 5; i++) {
      await title.click();
    }
    await expect(overlay).toHaveCount(0);

    const overflowAfterClose = await page.evaluate(() => document.documentElement.style.overflow);
    expect(overflowAfterClose).toBe('');
  });

  test('5 rapid taps on the header title toggles the overlay open, then closed again', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const title = page.getByTestId('header-title');
    await title.waitFor();
    const overlay = page.getByTestId('safe-area-debug-overlay');
    await expect(overlay).toHaveCount(0);

    for (let i = 0; i < 5; i++) {
      await title.click();
    }
    await expect(overlay).toBeVisible();

    for (let i = 0; i < 5; i++) {
      await title.click();
    }
    await expect(overlay).toHaveCount(0);
  });

  test('4 taps is not enough to trigger the gesture', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const title = page.getByTestId('header-title');
    await title.waitFor();

    for (let i = 0; i < 4; i++) {
      await title.click();
    }
    await expect(page.getByTestId('safe-area-debug-overlay')).toHaveCount(0);
  });
});
