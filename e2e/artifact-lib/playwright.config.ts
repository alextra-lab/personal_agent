import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright config for the FRE-531 curated-`/lib/` render harness (ADR-0089 A7).
 *
 * Self-contained: each spec starts its own in-process CSP static server (no live
 * Seshat server, no Next.js build). Fixtures are produced beforehand by
 * `scripts/build_e2e_artifact_fixtures.py` into `.fixtures/` (see `make
 * verify-artifact-e2e`). Two engines cover the ticket's "Chromium AND WebKit/iOS"
 * bar; real-device iOS Safari remains the owner's post-merge check.
 */
export default defineConfig({
  testDir: '.',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: { headless: true },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
  ],
});
