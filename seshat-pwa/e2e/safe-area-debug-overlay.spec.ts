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

  test('toggling the experiment re-measures rather than showing stale numbers', async ({
    page,
  }) => {
    // Regression test for a real bug: the measurement effect used to only
    // depend on [enabled], so toggling the experiment changed the DOM but
    // never refreshed the *displayed* numbers — the owner's round-7
    // screenshot never actually proved anything either way because of this.
    // headless Chromium can't reproduce the real WebKit viewport bug (see
    // this file's header comment), so a height number can't tell "stale"
    // from "correctly re-measured but numerically unchanged" apart here —
    // activeElement can, since it's fully environment-independent.
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    const overlay = page.getByTestId('safe-area-debug-overlay');
    await overlay.waitFor();
    await expect(overlay).toContainText('activeElement: BODY');

    // Clicking a button focuses it (standard browser behavior) — so the
    // toggle click itself moves activeElement from BODY to BUTTON. A stale
    // display would still show the mount-time BODY; only a fresh
    // re-measurement at toggle time picks up BUTTON.
    await page.getByTestId('safe-area-experiment-toggle').click();

    await expect(overlay).toContainText('activeElement: BUTTON');
  });

  test('the experiment toggle button does not block the header gesture underneath it', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    const overlay = page.getByTestId('safe-area-debug-overlay');
    await overlay.waitFor();

    // No force: true — a real, unobstructed click is the whole point of
    // this test. The overlay starts below the header (see its `top` style)
    // specifically so the experiment button's pointerEvents:'auto' hit-box
    // can never steal taps meant for the header's own controls.
    const title = page.getByTestId('header-title');
    for (let i = 0; i < 5; i++) {
      await title.click();
    }
    await expect(overlay).toHaveCount(0);
  });

  test('the hamburger menu button stays clickable while the overlay is open', async ({ page }) => {
    // Self-review (round 7) caught the experiment button's hit-box
    // overlapping the header's own hamburger control when the overlay
    // started at top:0 — this asserts the actual regression, not just the
    // title's own gesture.
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(`${CHAT_URL}?debug=safearea`);

    await page.getByTestId('safe-area-debug-overlay').waitFor();
    await page.getByRole('button', { name: 'Open session list' }).click();
    await expect(page.getByText('Artifacts')).toBeVisible();
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
      await title.click();
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
