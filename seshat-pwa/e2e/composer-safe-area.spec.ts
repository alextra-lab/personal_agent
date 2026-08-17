/**
 * Playwright e2e tests for the composer's safe-area-inset-bottom handling
 * (FRE-1267, revised FRE-1269 rounds 11-12). FRE-1266 fixed the
 * document-scroll regression but its own AC-4 measured the `footer`
 * element, which was already flush — not the `composer-container` card
 * inside it, which floated above the true viewport bottom by the inset
 * amount because `ChatInput.tsx`'s `<form>` wrapper carried that inset as
 * `paddingBottom` *outside* the card, with nothing reserving that space at
 * all.
 *
 * FRE-1267 fixed that by making the card itself absorb the inset and reach
 * fully flush to the true bottom. Once shipped for real (FRE-1269), the
 * owner found a fully edge-to-edge panel felt oversized — round 11
 * reintroduced a small, deliberate `CARD_GAP_PX` gap (the form's own
 * bottom padding), widened in round 12 after the owner found round 11's
 * gap still clipped the card's rounded bottom corners on device. Explicitly
 * NOT the same defect FRE-1267 fixed: this gap is a fixed, intentional
 * margin, not an unreserved dead zone that grows with the device's
 * safe-area inset (the difference AC-1/AC-4 below assert).
 *
 * These tests force a non-zero inset via `overrideSafeAreaBottom` (a CSS
 * custom-property override; Playwright/CDP cannot emulate
 * env(safe-area-inset-*) directly) since headless Chromium always reports
 * `env(safe-area-inset-bottom) === 0`.
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
const CARD_GAP_PX = 20; // form's pb-5 (1.25rem) — round-11's pb-3/12px still clipped the card's rounded corners on device; widened round 12.

test.describe('Composer safe-area inset (FRE-1267, revised FRE-1269 rounds 11-12)', () => {
  test.use({ viewport: { width: 390, height: VIEWPORT_HEIGHT } });

  test('AC-1: the composer container sits a small, fixed gap above the viewport bottom, regardless of inset size', async ({
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
    // A fixed gap, not one that scales with the inset — distinguishes this
    // deliberate margin from the pre-FRE-1267 defect (an unreserved dead
    // zone the size of the inset itself).
    const gap = VIEWPORT_HEIGHT - (box!.y + box!.height);
    expect(gap).toBeGreaterThanOrEqual(CARD_GAP_PX - 2);
    expect(gap).toBeLessThanOrEqual(CARD_GAP_PX + 2);
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

  test('AC-4: zero-inset behaviour does not regress — the deliberate gap and tap-comfort padding survive', async ({
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
    // At zero inset, the only gap present is the fixed CARD_GAP_PX
    // (the form's own bottom padding) — there's no inset left to also
    // reserve.
    const gap = VIEWPORT_HEIGHT - (box!.y + box!.height);
    expect(gap).toBeGreaterThanOrEqual(CARD_GAP_PX - 2);
    expect(gap).toBeLessThanOrEqual(CARD_GAP_PX + 2);

    // Guards against the fallback tap-comfort padding being silently
    // dropped rather than genuinely preserved: assert the actual computed
    // values, not just that the container happens to sit the right distance
    // away (which would also be true if the padding were deleted outright).
    const controlsPaddingBottom = await controls.evaluate(
      (el) => getComputedStyle(el).paddingBottom,
    );
    expect(controlsPaddingBottom).toBe('4px'); // Tailwind pb-1 = 0.25rem (round 12, was pb-1.5; originally pb-2 pre-round-11).

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
