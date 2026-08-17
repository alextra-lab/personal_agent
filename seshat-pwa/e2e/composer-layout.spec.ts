/**
 * Playwright e2e tests for the two-row composer layout (FRE-1263).
 *
 * These assert the acceptance criteria that need real CSS layout — jsdom
 * (used by the vitest unit tests in ChatInput.test.tsx) does not compute
 * box sizes or resolve Tailwind classes to actual pixel values, so AC-2,
 * AC-3, and AC-4 can only be proven against a real rendered page. AC-1 and
 * AC-5 are covered by both the unit tests and here, for a single source of
 * truth at the viewport the ticket names.
 */

import { test, expect } from '@playwright/test';
import { TEST_SESSION, stubRest, stubWebSocket } from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;

test.describe('Composer two-row layout (FRE-1263)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('AC-1..AC-5: textarea spans the composer, at comfortable size, with controls intact', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);

    await page.goto(CHAT_URL);
    const textarea = page.locator('[placeholder="Message Seshat..."]');
    await textarea.waitFor();

    // AC-1: the textarea's parent element has no button descendants.
    const parentButtonCount = await textarea.evaluate(
      (el) => el.parentElement?.querySelectorAll('button').length ?? -1,
    );
    expect(parentButtonCount).toBe(0);

    // AC-2: the textarea spans at least 90% of the composer container's inner width.
    const container = page.getByTestId('composer-container');
    const textareaBox = await textarea.boundingBox();
    const containerBox = await container.boundingBox();
    expect(textareaBox).not.toBeNull();
    expect(containerBox).not.toBeNull();
    expect(textareaBox!.width).toBeGreaterThanOrEqual(containerBox!.width * 0.9);

    // AC-3: comfortable type size — at least 16px (also the iOS focus-zoom threshold).
    const fontSizePx = await textarea.evaluate((el) =>
      parseFloat(getComputedStyle(el).fontSize),
    );
    expect(fontSizePx).toBeGreaterThanOrEqual(16);

    // AC-4: the max-height the textarea can grow to is taller than the old
    // 140px ceiling. (The empty-state row count this AC originally asserted —
    // "at least 3 text lines tall" — was corrected by FRE-1266: the textarea
    // now starts at a single row, not 3. See viewport-shell.spec.ts AC-5.)
    const maxHeightPx = await textarea.evaluate((el) =>
      parseFloat(getComputedStyle(el).maxHeight),
    );
    expect(maxHeightPx).toBeGreaterThan(140);

    // AC-5: nothing is lost — every control is present and reachable by its aria-label.
    await expect(page.getByLabel('Choose model')).toBeVisible();
    await expect(page.getByLabel('Attach file')).toBeVisible();
    await expect(page.getByLabel('Send message')).toBeVisible();
  });
});
