/**
 * Playwright e2e test for wide assistant content (FRE-1651 AC-4).
 * A wide table or display-math line must scroll inside its own box and must
 * never make the message list scroll sideways. jsdom cannot compute layout,
 * so this runs against a real page at an iPhone viewport.
 */

import { test, expect } from '@playwright/test';
import { TEST_SESSION, stubWebSocket } from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;
const BACKEND = 'http://localhost:9000';

// Synthetic fixture: money in text, a 6-column table, and one long display-math line.
const ASSISTANT_TEXT = [
  'Fares run $554–816 typical; cheapest $506. Other dates: $425–580 against $660–820.',
  '',
  // Unbreakable cells (ISO timestamps) are what make a real table wider than the column.
  '| Route | Carrier | Outbound | Return | Stops | Price |',
  '|-------|---------|----------|--------|-------|-------|',
  '| AAA-BBB | ExampleAirOne | 2026-11-02T08:15:00+01:00 | 2026-11-16T17:40:00+01:00 | nonstop | $554 |',
  '| AAA-CCC | ExampleAirTwo | 2026-11-03T09:30:00+01:00 | 2026-11-17T19:05:00+01:00 | onestop | $506 |',
  '',
  '$$',
  Array.from({ length: 24 }, (_, i) => `a_{${i}}`).join(' + '),
  '$$',
].join('\n');

test.describe('Wide assistant content (FRE-1651 AC-4)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('the message list does not scroll sideways', async ({ page }) => {
    const now = new Date().toISOString();
    await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}/messages*`, (route) =>
      route.fulfill({
        json: [
          { role: 'user', content: 'Find flights', timestamp: now },
          { role: 'assistant', content: ASSISTANT_TEXT, timestamp: now, trace_id: 'trace-1' },
        ],
      }),
    );
    await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}`, (route) =>
      route.fulfill({ status: 404, body: 'not found' }),
    );
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const list = page.locator('main main');
    await list.locator('table').waitFor();

    // Money renders as text, with its dollar sign.
    await expect(list.locator('.katex:not(.katex-display .katex)')).toHaveCount(0);
    await expect(list).toContainText('$554–816 typical; cheapest $506');

    // The list itself does not scroll sideways.
    const listWidths = await list.evaluate((el) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
    }));
    expect(listWidths.scrollWidth).toBe(listWidths.clientWidth);

    // The table scrolls inside its own box.
    const tableBox = await list.locator('table').evaluate((el) => {
      const box = el.parentElement!;
      return { scrollWidth: box.scrollWidth, clientWidth: box.clientWidth };
    });
    expect(tableBox.scrollWidth).toBeGreaterThan(tableBox.clientWidth);

    // The display-math line scrolls inside its own box.
    const mathBox = await list.locator('.katex-display').evaluate((el) => {
      const box = el.parentElement!;
      return { scrollWidth: box.scrollWidth, clientWidth: box.clientWidth };
    });
    expect(mathBox.scrollWidth).toBeGreaterThan(mathBox.clientWidth);
  });
});
