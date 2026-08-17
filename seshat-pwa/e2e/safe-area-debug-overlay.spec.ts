/**
 * Playwright e2e tests for the temporary FRE-1269 on-device diagnostic
 * overlay (SafeAreaDebugOverlay). Headless Chromium can't reproduce the real
 * iOS standalone-mode bug (see composer-safe-area.spec.ts's header comment),
 * so this overlay exists to let the owner capture real numbers from their
 * own device. These tests only guard: (a) it stays invisible by default —
 * no accidental UX regression — (b) it renders the expected measurement
 * rows when explicitly requested, and (c) it never blocks interaction with
 * whatever's underneath (it's read-only — see round-9's history for why a
 * previous interactive version of this overlay needed that guarded
 * explicitly: a since-retired experiment toggle button's hit-box briefly
 * overlapped and stole taps from the header's hamburger control).
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
