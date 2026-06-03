import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright e2e configuration for seshat-pwa (FRE-400 WS3).
 *
 * All tests mock the backend (WebSocket + REST) entirely via
 * page.routeWebSocket() and page.route() — no live Seshat server required.
 * The webServer block builds and serves a production Next.js bundle so tests
 * run against the real compiled app, not a dev proxy.
 *
 * Run locally (headless):     npm run test:e2e
 * Run with browser UI:        npm run test:e2e -- --headed
 * Run in CI:                  npx playwright install --with-deps chromium && npm run test:e2e
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,   // single worker — tests share the Next.js server
  workers: 1,
  retries: 1,             // one retry catches rare race conditions in CI
  reporter: process.env.CI ? 'github' : 'list',

  use: {
    baseURL: 'http://localhost:3100',
    headless: true,
    // No GATEWAY_TOKEN → no ticket fetch → WS URL has no query param,
    // which makes routeWebSocket matching simpler.
    ignoreHTTPSErrors: false,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    // Standalone output needs static + public co-located alongside server.js.
    command: [
      'npm run build',
      'cp -r public .next/standalone/public',
      'cp -r .next/static .next/standalone/.next/static',
      'PORT=3100 node .next/standalone/server.js',
    ].join(' && '),
    url: 'http://localhost:3100',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      NEXT_PUBLIC_SESHAT_URL: 'http://localhost:9000',
      NEXT_PUBLIC_GATEWAY_TOKEN: '',  // empty → no ticket fetch, no auth headers
    },
  },
});
