/**
 * Playwright e2e tests for the composer's safe-area-inset-bottom handling
 * (FRE-1267). FRE-1266 fixed the document-scroll regression but its own
 * AC-4 measured the `footer` element, which was already flush — not the
 * `composer-container` card inside it, which floats above the true viewport
 * bottom by the inset amount because `ChatInput.tsx`'s `<form>` wrapper
 * carried that inset as `paddingBottom` *outside* the card. That gap is
 * invisible to any harness that measures at `env(safe-area-inset-bottom) ===
 * 0`, which is what headless Chromium always reports — so these tests force
 * a non-zero inset via `overrideSafeAreaBottom` (a CSS custom-property
 * override; Playwright/CDP cannot emulate env(safe-area-inset-*) directly)
 * to make the defect measurable at all. See the FRE-1267 ticket handoff for
 * the seeded-negative before/after numbers this file produced.
 */

import { test, expect } from '@playwright/test';
import {
  TEST_SESSION,
  stubRest,
  stubWebSocket,
  overrideSafeAreaBottom,
  contrastRatio,
} from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;
const VIEWPORT_HEIGHT = 844;
const TEST_INSET_PX = 34; // iPhone home-indicator safe-area-inset-bottom in standalone mode.

test.describe('Composer safe-area inset (FRE-1267)', () => {
  test.use({ viewport: { width: 390, height: VIEWPORT_HEIGHT } });

  test('AC-1: the composer container reaches the viewport bottom with a real inset present', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const container = page.getByTestId('composer-container');
    await container.waitFor();
    await overrideSafeAreaBottom(page, TEST_INSET_PX);

    const box = await container.boundingBox();
    expect(box).not.toBeNull();
    const gap = VIEWPORT_HEIGHT - (box!.y + box!.height);
    expect(Math.abs(gap)).toBeLessThanOrEqual(2);
  });

  test('AC-2: the inset is still respected as internal space above the controls row', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const container = page.getByTestId('composer-container');
    const controls = page.getByTestId('composer-controls');
    await container.waitFor();
    await overrideSafeAreaBottom(page, TEST_INSET_PX);

    const containerBox = await container.boundingBox();
    const controlsBox = await controls.boundingBox();
    expect(containerBox).not.toBeNull();
    expect(controlsBox).not.toBeNull();

    // Ticket wording: controls row bottom is at least the inset value above
    // the viewport bottom — nothing sits under the home indicator.
    const controlsToViewport = VIEWPORT_HEIGHT - (controlsBox!.y + controlsBox!.height);
    expect(controlsToViewport).toBeGreaterThanOrEqual(TEST_INSET_PX - 2);

    // Tighter, more discriminating check (a grossly oversized padding or an
    // unrelated footer gap could satisfy the lower bound above alone):
    // the clearance between the container's own bottom edge and the
    // controls row's bottom edge should track the inset directly, not just
    // exceed it by an arbitrary margin.
    const containerToControls =
      containerBox!.y + containerBox!.height - (controlsBox!.y + controlsBox!.height);
    expect(containerToControls).toBeGreaterThanOrEqual(TEST_INSET_PX - 2);
    expect(containerToControls).toBeLessThanOrEqual(TEST_INSET_PX + 4);
  });

  test('AC-4: zero-inset behaviour does not regress — the panel still reaches the bottom and the tap-comfort padding survives', async ({
    page,
  }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const container = page.getByTestId('composer-container');
    const controls = page.getByTestId('composer-controls');
    await container.waitFor();
    // No override — env(safe-area-inset-bottom) defaults to 0 in headless
    // Chromium, exercising the zero-inset path.

    const box = await container.boundingBox();
    expect(box).not.toBeNull();
    const gap = VIEWPORT_HEIGHT - (box!.y + box!.height);
    expect(Math.abs(gap)).toBeLessThanOrEqual(2);

    // Guards against the fallback tap-comfort padding being silently
    // dropped rather than genuinely preserved: assert the actual computed
    // values, not just that the container happens to be flush (which would
    // also be true if the padding were deleted outright).
    const controlsPaddingBottom = await controls.evaluate(
      (el) => getComputedStyle(el).paddingBottom,
    );
    expect(controlsPaddingBottom).toBe('8px'); // Tailwind pb-2 = 0.5rem, untouched by this fix.

    const containerPaddingBottom = await container.evaluate(
      (el) => getComputedStyle(el).paddingBottom,
    );
    expect(containerPaddingBottom).toBe('0px'); // var(--safe-bottom, 0px) at zero inset.
  });

  test('the focus ring meets 3:1 non-text UI contrast now the border is gone', async ({
    page,
  }) => {
    // Dropping the resting border (item 2 of the ticket) left the
    // focus-within ring as the only focus affordance for the composer. A
    // codex plan-review caught that the originally-proposed 30%-opacity
    // ring computed to well under the WCAG 3:1 UI-component threshold; the
    // shipped fix uses a full-opacity ring instead — this asserts that directly
    // rather than trusting the token math, matching this repo's established
    // pattern (theme.spec.ts / theme-extended.spec.ts) of measuring real
    // rendered contrast rather than reasoning about it.
    await stubRest(page);
    await stubWebSocket(page);
    await page.goto(CHAT_URL);

    const textarea = page.locator('[placeholder="Message Seshat..."]');
    await textarea.waitFor();
    await textarea.focus();

    const container = page.getByTestId('composer-container');
    const ringColor = await container.evaluate((el) =>
      getComputedStyle(el).getPropertyValue('--tw-ring-color').trim(),
    );
    const pageBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);

    expect(contrastRatio(ringColor, pageBg)).toBeGreaterThanOrEqual(3);
  });
});
