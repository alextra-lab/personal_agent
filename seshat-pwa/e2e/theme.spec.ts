/**
 * Playwright e2e tests for the theme-aware visual system (FRE-1264 AC-1,
 * AC-2, AC-7). Real rendered CSS is required here — jsdom (used by the
 * vitest unit tests) doesn't resolve CSS custom properties or compute
 * contrast, so these ACs can only be proven against a real page.
 */

import { test, expect } from '@playwright/test';
import { TEST_SESSION, stubRest, stubWebSocket } from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;
const THEME_STORAGE_KEY = 'seshat-theme-override';

async function bodyBackground(page: import('@playwright/test').Page): Promise<string> {
  return page.evaluate(() => getComputedStyle(document.body).backgroundColor);
}

/** Parse an `rgb(r, g, b)` / `rgba(r, g, b, a)` string into channel values. */
function parseRgb(color: string): [number, number, number] {
  const m = color.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/);
  if (!m) throw new Error(`Unparseable colour: ${color}`);
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

/** WCAG relative luminance (sRGB, 0..1). */
function relativeLuminance([r, g, b]: [number, number, number]): number {
  const chan = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  const [rl, gl, bl] = [chan(r), chan(g), chan(b)];
  return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl;
}

/** WCAG contrast ratio between two colours, each 4.5:1-testable either order. */
function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(parseRgb(a));
  const lb = relativeLuminance(parseRgb(b));
  const [lighter, darker] = la > lb ? [la, lb] : [lb, la];
  return (lighter + 0.05) / (darker + 0.05);
}

test.describe('Theme layer (FRE-1264 AC-1, AC-2, AC-7)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('AC-1: body background follows the emulated colour scheme, both directions', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);

    await page.emulateMedia({ colorScheme: 'light' });
    await page.goto(CHAT_URL);
    const lightBg = await bodyBackground(page);

    await page.emulateMedia({ colorScheme: 'dark' });
    await page.reload();
    const darkBg = await bodyBackground(page);

    expect(lightBg).not.toBe(darkBg);
    // The light ground is a warm near-white, not the old hardcoded dark navy.
    const [lr, lg, lb] = parseRgb(lightBg);
    expect(lr).toBeGreaterThan(200);
    expect(lg).toBeGreaterThan(200);
    expect(lb).toBeGreaterThan(200);
    // The dark ground stays dark.
    const [dr, dg, db] = parseRgb(darkBg);
    expect(dr).toBeLessThan(100);
  });

  test('AC-2: a stored override beats the system preference and survives a reload', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);

    // System says light; a stored override says dark. Set the override
    // before the app's first script runs (matches how a real write-path
    // would land it — this test proves the read-path honors it).
    await page.addInitScript(
      (key) => localStorage.setItem(key, 'dark'),
      THEME_STORAGE_KEY,
    );
    await page.emulateMedia({ colorScheme: 'light' });

    await page.goto(CHAT_URL);
    const html = page.locator('html');
    await expect(html).toHaveClass(/dark/);

    await page.reload();
    await expect(html).toHaveClass(/dark/);
  });

  test('AC-7: body text meets 4.5:1 contrast against its background, in both themes', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);

    for (const scheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto(CHAT_URL);

      const [bg, color] = await page.evaluate(() => {
        const s = getComputedStyle(document.body);
        return [s.backgroundColor, s.color];
      });

      expect(contrastRatio(bg, color)).toBeGreaterThanOrEqual(4.5);
    }
  });
});
