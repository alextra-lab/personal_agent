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
    // FRE-1269 round-7 additions — codex flagged the original probe set as
    // insufficient to distinguish CSS-resolution from actual paintability,
    // and the 62px gap's split (safe-area vs unexplained) was unproven
    // without also measuring the top inset.
    expect(text).toContain('--safe-top');
    expect(text).toContain('scrollY');
    expect(text).toContain('html.scrollHeight');
    expect(text).toContain('visualViewport.offsetTop');
    expect(text).toContain('visualViewport.pageTop');
    expect(text).toContain('activeElement');
  });

  test('the 100vh live-experiment toggle applies and reverts an inline height override', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    const overlay = page.getByTestId('safe-area-debug-overlay');
    await overlay.waitFor();

    const heightBefore = await page.evaluate(() => document.documentElement.style.height);
    expect(heightBefore).toBe('');

    const toggle = page.getByTestId('safe-area-experiment-toggle');
    await toggle.click();

    await expect
      .poll(() => page.evaluate(() => document.documentElement.style.height))
      .toBe('100vh');
    const bodyHeightOn = await page.evaluate(() => document.body.style.height);
    expect(bodyHeightOn).toBe('100vh');

    await toggle.click();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.style.height))
      .toBe('');
    const bodyHeightOff = await page.evaluate(() => document.body.style.height);
    expect(bodyHeightOff).toBe('');
  });

  test('the experiment toggle button does not block the header gesture underneath it', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    const overlay = page.getByTestId('safe-area-debug-overlay');
    await overlay.waitFor();

    // The overlay's non-button area sits on top of the header (fixed, top:0)
    // — closing via the same 5-tap gesture must still work while it's open.
    const title = page.getByTestId('header-title');
    for (let i = 0; i < 5; i++) {
      await title.click({ force: true });
    }
    await expect(overlay).toHaveCount(0);
  });

  test('closing the overlay reverts an active experiment', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    const overlay = page.getByTestId('safe-area-debug-overlay');
    await overlay.waitFor();
    await page.getByTestId('safe-area-experiment-toggle').click();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.style.height))
      .toBe('100vh');

    const title = page.getByTestId('header-title');
    for (let i = 0; i < 5; i++) {
      await title.click({ force: true });
    }
    await expect(overlay).toHaveCount(0);

    const heightAfterClose = await page.evaluate(() => document.documentElement.style.height);
    expect(heightAfterClose).toBe('');
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
