/**
 * Playwright e2e test for the iOS standalone-mode meta tags (FRE-1269).
 *
 * Next.js 15's `appleWebApp` metadata API stopped emitting the Apple-prefixed
 * `apple-mobile-web-app-capable` tag in favor of the generic W3C
 * `mobile-web-app-capable` tag (vercel/next.js#70272), even though the source
 * still declares `appleWebApp: { capable: true, ... }` in layout.tsx. Apple's
 * own docs state `apple-mobile-web-app-status-bar-style` has no effect at all
 * without this tag present — so the app's `black-translucent` status bar
 * setting was silently inert. This asserts the tag is restored via
 * `metadata.other`, independent of and regardless of whether it turns out to
 * be a contributing cause of the FRE-1269 bottom-gap defect.
 */

import { test, expect } from '@playwright/test';

test.describe('iOS standalone meta tags (FRE-1269)', () => {
  test('apple-mobile-web-app-capable is present, alongside the generic mobile-web-app-capable tag', async ({
    page,
  }) => {
    await page.goto('/');

    const appleCapable = page.locator('meta[name="apple-mobile-web-app-capable"]');
    await expect(appleCapable).toHaveAttribute('content', 'yes');

    // Regression guard: Next.js's own appleWebApp metadata still emits this
    // one — restoring the Apple tag must not remove it.
    const genericCapable = page.locator('meta[name="mobile-web-app-capable"]');
    await expect(genericCapable).toHaveAttribute('content', 'yes');
  });
});
