/**
 * Playwright e2e tests for the temporary FRE-1269 on-device diagnostic
 * overlay (SafeAreaDebugOverlay). Headless Chromium can't reproduce the real
 * iOS standalone-mode bug (see composer-safe-area.spec.ts's header comment),
 * so this overlay exists to let the owner capture real numbers from their
 * own device. These tests only guard: (a) it stays invisible by default —
 * no accidental UX regression — and (b) it renders the expected measurement
 * rows when explicitly requested.
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
